"""Hermes 0.21 native delegation adapter; no provider client or global config edits.

The author is paused while one child runs. The child's measured iterations are
charged to the report ledger, then subtracted from the author's remaining cap.
Hermes's normal final toolless grace response is outside its iteration counter.
"""

from __future__ import annotations

import contextvars
import json
import threading
import weakref
from concurrent.futures import TimeoutError
from pathlib import Path

from hermes_medical_search.models import ValidationError

from . import native_review
from .workspace import Workspace

_BINDINGS = {}
_REVIEWERS = {}


def _parent(kwargs):
    parent = kwargs.get("parent_agent")
    if parent is None:
        from agent.subagent_lifecycle import get_active_subagent_parent

        parent = get_active_subagent_parent()
    if parent is None or not getattr(parent, "session_id", None):
        raise ValidationError("Hermes did not expose its native parent session")
    return parent


def _sync(parent, binding):
    with binding.setdefault("lock", threading.RLock()):
        return _sync_locked(parent, binding)


def _sync_locked(parent, binding):
    counter = parent.iteration_budget
    # Native counters reset between user turns. Persist prior usage across that reset.
    if binding.get("counter") is not counter:
        binding["offset"] = binding["used"]
        binding["counter"] = counter
    used = binding["offset"] + counter.used
    state = native_review.bind(
        binding["workspace"],
        host="hermes",
        author_session_id=parent.session_id,
        total_turns=binding["total"],
        used_turns=used,
        unit="host iterations (execute_code refunds disabled)",
    )
    binding["used"] = used
    # Per-object, per-report policy: code execution consumes turns like every other tool.
    counter.refund = lambda: None
    counter.max_total = counter.used + state["remaining_turns"]
    return state


def session(args, **kwargs):
    parent = _parent(kwargs)
    if getattr(parent, "_delegate_depth", 0):
        raise ValidationError("start the medical report in its author session")
    workspace = Workspace(Path(args["run_dir"]))
    workspace.path.mkdir(parents=True, exist_ok=True)
    if (workspace.path / "research.json").exists():
        workspace.load()
    sid = parent.session_id
    existing = _BINDINGS.get(sid)
    if existing and existing["workspace"].path != workspace.path:
        raise ValidationError("one active research budget per native author session")
    if not existing:
        state = workspace.store.read_json(native_review.STATE, default={})
        prior = state.get("authors", {}).get(sid, 0)
        existing = {
            "workspace": workspace,
            "total": args.get("total_turns", 150),
            "used": prior,
            "offset": prior,
            "counter": parent.iteration_budget,
            "parent": weakref.ref(parent),
            "reviewing": False,
        }
        _BINDINGS[sid] = existing
    return {
        "host": "hermes",
        "author_session_id": sid,
        "budget": _sync(parent, existing),
        "review_tool": "medical_research_review",
    }


def review(args, **kwargs):
    parent = _parent(kwargs)
    binding = _BINDINGS.get(parent.session_id)
    if not binding or Workspace(Path(args["run_dir"])).path != binding["workspace"].path:
        raise ValidationError("call medical_research_session before research work")
    _sync(parent, binding)
    workspace = binding["workspace"]
    if args.get("action", "start") == "status":
        future = binding.get("future")
        if future is None:
            raise ValidationError("there is no active native reviewer in this session")
        try:
            return future.result(timeout=min(45, max(0, args.get("wait_seconds", 45))))
        except TimeoutError:
            return {
                "state": "running",
                "task_id": binding["task"]["task_id"],
                "next": "call medical_research_review action=status wait_seconds=45",
            }
    task = native_review.prepare(
        workspace, author_session_id=parent.session_id, review_turns=args.get("review_turns", 20)
    )
    binding["reviewing"] = True
    binding["task"] = task
    # Construction follows the native delegate's main-thread requirement; the host's
    # daemon executor handles execution, so the tool never waits beyond the 420s guard.
    from tools.daemon_pool import DaemonThreadPoolExecutor
    from tools.delegate_tool import _build_child_agent

    child = _build_child_agent(
        0, task["prompt"], None, ["file"], None, task["allocated_turns"], 1, parent
    )
    child.prefill_messages = []
    _REVIEWERS[child.session_id] = str(Path(task["packet_path"]).parent)
    old_step = getattr(child, "step_callback", None)

    def step(*values):
        child.iteration_budget.refund = lambda: None
        if old_step:
            old_step(*values)

    child.step_callback = step
    executor = DaemonThreadPoolExecutor(max_workers=1, thread_name_prefix="medical-review")
    binding["executor"] = executor
    binding["future"] = executor.submit(
        contextvars.copy_context().run, _complete_review, parent, child, binding, task
    )
    return {
        "state": "running",
        "task_id": task["task_id"],
        "budget": task["budget"],
        "next": "call medical_research_review action=status wait_seconds=45",
    }


def _complete_review(parent, child, binding, task):
    from tools.delegate_tool import _run_single_child

    workspace = binding["workspace"]
    terminated = False
    try:
        result = _run_single_child(0, task["prompt"], child, parent)
        # A timed-out native worker may still be stopping; retain its reservation and
        # path restrictions rather than assume that returned text proves termination.
        terminated = result.get("exit_reason") in {"completed", "max_iterations"}
        if not terminated:
            return {
                "state": "interrupted",
                "accepted": False,
                "reason": "Native reviewer termination is unconfirmed; budget remains reserved",
            }
        output = Path(task["packet_path"]).parent / "result.json"
        completed = result.get("exit_reason") == "completed" and not result.get("truncated")
        try:
            payload = json.loads(output.read_text())
        except (ValueError, OSError):
            payload, completed = {}, False
        accepted = native_review.finish(
            workspace,
            task_id=task["task_id"],
            reviewer_session_id=child.session_id,
            used_turns=child.iteration_budget.used,
            result=payload,
            completed=completed,
            native_metadata={
                "subagent_id": getattr(child, "_subagent_id", None),
                "model": result.get("model"),
                "api_calls": result.get("api_calls"),
                "exit_reason": result.get("exit_reason"),
            },
        )
        return {"state": "completed", **accepted, "review_path": str(output)}
    except Exception as exc:
        return {"state": "failed", "accepted": False, "reason": str(exc)}
    finally:
        binding["reviewing"] = False
        _sync(parent, binding)
        if terminated:
            _REVIEWERS.pop(child.session_id, None)


def pre_tool_call(tool_name=None, args=None, session_id=None, **kwargs):
    """Block unmetered delegation and give reviewers only frozen-source read access."""
    if session_id in _REVIEWERS:
        if tool_name not in {"read_file", "write_file"}:
            return {
                "action": "block",
                "message": "Reviewer may only read frozen sources or write its result",
            }
        supplied = (args or {}).get("path", "")
        try:
            directory = Path(_REVIEWERS[session_id])
            allowed = (
                {directory / "packet.json", directory / "sources.json"}
                if tool_name == "read_file"
                else {directory / "result.json"}
            )
            if Path(supplied).resolve() not in allowed:
                return {
                    "action": "block",
                    "message": "Use only the assigned review input/output paths",
                }
        except (TypeError, ValueError):
            return {"action": "block", "message": "Invalid review source path"}
    binding = _BINDINGS.get(session_id)
    if binding:
        parent = binding["parent"]()
        if parent is None:
            return {"action": "block", "message": "Research author session is unavailable"}
        budget = _sync(parent, binding)
        waiting = tool_name == "medical_research_review" and (args or {}).get("action") == "status"
        if not budget["within_budget"] or (binding["reviewing"] and not waiting):
            return {"action": "block", "message": "Research budget exhausted or review in progress"}
        if tool_name == "delegate_task":
            return {
                "action": "block",
                "message": "Use medical_research_review for metered delegation",
            }
    return None


def register(ctx):
    ctx.register_hook("pre_tool_call", pre_tool_call)
    for name, handler, properties in (
        ("medical_research_session", session, {"total_turns": {"type": "integer", "minimum": 1}}),
        ("medical_research_review", review, {"review_turns": {"type": "integer", "minimum": 1}}),
    ):

        def handle(args, _handler=handler, **kwargs):
            try:
                return json.dumps(_handler(args, **kwargs))
            except (ValidationError, ValueError, OSError, KeyError, TypeError) as exc:
                return json.dumps({"error": str(exc)})

        schema = {
            "name": name,
            "description": (
                "Bind/resume a medical report's shared host turn budget"
                if handler is session
                else "Run a fresh native evidence reviewer within the report's shared turn budget"
            ),
            "parameters": {
                "type": "object",
                "properties": {"run_dir": {"type": "string"}, **properties},
                "required": ["run_dir"],
            },
        }
        ctx.register_tool(name=name, toolset="medical_research", schema=schema, handler=handle)
