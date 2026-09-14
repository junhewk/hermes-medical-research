"""Hermes 0.21 native delegation adapter; no provider client or global config edits.

The author is paused while one child runs. The child's measured iterations are
charged to the report ledger, then subtracted from the author's remaining cap.
Hermes's normal final toolless grace response is outside its iteration counter.
"""

from __future__ import annotations

import contextvars
import json
import threading
import time
import weakref
from concurrent.futures import TimeoutError
from pathlib import Path

from hermes_medical_search.models import ValidationError

from . import native_review
from .workspace import Workspace

_BINDINGS = {}
_REVIEWERS = {}
WAIT_MAX = 300  # Hermes's sequential tool deadline is 420 s by default.
CHECK_TOOL = "medical_research_review_check"


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
        "check_tool": CHECK_TOOL,
        "native_review": native_review.summary(workspace),
    }


def _progress(binding):
    """Fields that change between polls so the host's identical-call guard stays quiet."""
    child = binding.get("child")
    task = binding.get("task") or {}
    directory = Path(task["packet_path"]).parent if task else None
    files = []
    if directory:
        combined, per_finding = native_review.result_paths(directory)
        files = [combined.name] if combined.exists() else []
        if per_finding.is_dir():
            files += sorted(f"results/{p.name}" for p in per_finding.glob("*.json"))
    budget = getattr(child, "iteration_budget", None)
    return {
        "iterations_used": getattr(budget, "used", None),
        "iterations_allocated": task.get("allocated_turns"),
        "api_calls": getattr(child, "_api_call_count", None),
        "result_files": files,
        "elapsed_seconds": int(time.monotonic() - binding.get("started", time.monotonic())),
    }


def review(args, **kwargs):
    parent = _parent(kwargs)
    binding = _BINDINGS.get(parent.session_id)
    if not binding or Workspace(Path(args["run_dir"])).path != binding["workspace"].path:
        raise ValidationError("call medical_research_session before research work")
    _sync(parent, binding)
    workspace = binding["workspace"]
    action = args.get("action", "start")
    if action == "status":
        future = binding.get("future")
        if future is None:
            raise ValidationError("there is no active native reviewer in this session")
        try:
            return future.result(timeout=min(WAIT_MAX, max(0, args.get("wait_seconds", WAIT_MAX))))
        except TimeoutError:
            return {
                "state": "running",
                "task_id": binding["task"]["task_id"],
                "progress": _progress(binding),
                "next": f"call medical_research_review action=status wait_seconds={WAIT_MAX}",
            }
    if action == "abandon":
        future = binding.get("future")
        if future is not None and not future.done():
            raise ValidationError("the native reviewer is still running; keep polling status")
        task = binding.get("task") or {}
        result = native_review.abandon(
            workspace,
            task_id=args.get("task_id") or task.get("task_id"),
            author_session_id=parent.session_id,
        )
        binding["reviewing"] = False
        _sync(parent, binding)
        return {"state": "abandoned", **result, "native_review": native_review.summary(workspace)}
    task = native_review.prepare(
        workspace,
        author_session_id=parent.session_id,
        review_turns=args.get("review_turns", 20),
    )
    binding["reviewing"] = True
    binding["task"] = task
    binding["started"] = time.monotonic()
    # Construction follows the native delegate's main-thread requirement; the host's
    # daemon executor handles execution, so the tool never waits beyond the 420s guard.
    from tools.daemon_pool import DaemonThreadPoolExecutor
    from tools.delegate_tool import _build_child_agent

    child = _build_child_agent(
        0,
        task["prompt"],
        None,
        ["file", "medical_research"],
        None,
        task["allocated_turns"],
        1,
        parent,
    )
    child.prefill_messages = []
    from hermes_cli.config import load_config

    limit = review_output_limit(parent, (load_config() or {}).get("model", {}))
    if limit is not None:
        # Inherit only an existing host setting; never replace the child's default with None.
        child.max_tokens = limit
    binding["child"] = child
    _REVIEWERS[child.session_id] = {
        "directory": str(Path(task["packet_path"]).parent),
        "task_id": task["task_id"],
        "run_dir": str(workspace.path),
    }
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
        "check_tool": CHECK_TOOL,
        "next": f"call medical_research_review action=status wait_seconds={WAIT_MAX}",
    }


def review_output_limit(parent, model_config):
    limit = getattr(parent, "max_tokens", None)
    if limit is None and isinstance(model_config, dict):
        limit = model_config.get("max_tokens")
    return limit if type(limit) is int and limit > 0 else None


def review_check(args, **kwargs):
    """Validate a reviewer's result files without recording anything. Pure by design."""
    workspace = Workspace(Path(args["run_dir"]))
    state = workspace.store.read_json(native_review.STATE, default=None)
    if not state or not state.get("tasks"):
        raise ValidationError("this run has no native review task")
    task_id = args.get("task_id")
    if not task_id:
        task_id = max(state["tasks"].values(), key=lambda t: t["created_at"])["task_id"]
    task = state["tasks"].get(task_id)
    if not task:
        raise ValidationError("unknown native review task")
    return native_review.review_check(Path(task["packet_path"]).parent)


def _complete_review(parent, child, binding, task):
    from tools.delegate_tool import _run_single_child

    workspace = binding["workspace"]
    terminated = False
    try:
        result = _run_single_child(0, task["prompt"], child, parent)
        exit_reason = result.get("exit_reason")
        # A timed-out native worker may still be stopping; retain its reservation and
        # path restrictions rather than assume that returned text proves termination.
        terminated = exit_reason in {"completed", "max_iterations", "error"}
        if not terminated:
            return {
                "state": "interrupted",
                "accepted": False,
                "reason": "Native reviewer termination is unconfirmed; budget remains reserved",
                "next": "poll status; if the host never confirms, abandon the review",
            }
        directory = Path(task["packet_path"]).parent
        payload, problems = native_review.collect_result(directory)
        # The artifact decides. Loop exhaustion after a complete, digest-verified verdict is
        # acceptable because the child's cap equals its reservation; a missing or invalid file
        # still fails.
        completed = exit_reason in {"completed", "max_iterations"} and (
            payload is not None or bool(problems)
        )
        accepted = native_review.finish(
            workspace,
            task_id=task["task_id"],
            reviewer_session_id=child.session_id,
            used_turns=child.iteration_budget.used,
            result=payload,
            completed=completed,
            problems=problems,
            native_metadata={
                "subagent_id": getattr(child, "_subagent_id", None),
                "model": result.get("model"),
                "max_tokens": getattr(child, "max_tokens", None),
                "api_calls": result.get("api_calls"),
                "exit_reason": exit_reason,
                "truncated": result.get("truncated"),
            },
        )
        return {
            "state": "completed",
            **accepted,
            "review_path": str(directory),
            "native_review": native_review.summary(workspace),
        }
    except Exception as exc:
        return {
            "state": "failed",
            "accepted": False,
            "reason": str(exc),
            "native_review": native_review.summary(workspace),
        }
    finally:
        binding["reviewing"] = False
        _sync(parent, binding)
        if terminated:
            _REVIEWERS.pop(child.session_id, None)


def _reviewer_policy(tool_name, args, reviewer):
    """Path-based allowlist; every refusal states what the reviewer may do instead."""
    directory = Path(reviewer["directory"])
    combined, per_finding = native_review.result_paths(directory)
    allowed = (
        f"Reviewer for {directory}: read_file/search_files inside that directory (pass "
        f"path=...), write_file/patch on {combined} or {per_finding}/<finding_id>.json, and "
        f"{CHECK_TOOL}(run_dir, task_id)."
    )
    supplied = (args or {}).get("path", "")
    try:
        if tool_name in {"read_file", "search_files"}:
            if tool_name == "search_files" and supplied in {"", "."}:
                return {"action": "block", "message": f"Pass path={directory}. {allowed}"}
            if not native_review.is_task_file(supplied, directory) and not (
                Path(supplied).is_absolute() and Path(supplied).resolve() == directory.resolve()
            ):
                return {"action": "block", "message": f"Path outside the task. {allowed}"}
            return None
        if tool_name in {"write_file", "patch"}:
            if not native_review.is_result_path(supplied, directory):
                return {"action": "block", "message": f"Not a result path. {allowed}"}
            return None
        if tool_name in {CHECK_TOOL, "tool_search", "tool_describe"}:
            # The host defers plugin tools behind its read-only tool discovery helpers.
            return None
    except (TypeError, ValueError, OSError):
        return {"action": "block", "message": f"Invalid path. {allowed}"}
    return {"action": "block", "message": f"Tool {tool_name} is not available here. {allowed}"}


def pre_tool_call(tool_name=None, args=None, session_id=None, **kwargs):
    """Block unmetered delegation and give reviewers only frozen-source read access."""
    reviewer = _REVIEWERS.get(session_id)
    if reviewer:
        return _reviewer_policy(tool_name, args, reviewer)
    binding = _BINDINGS.get(session_id)
    if binding:
        parent = binding["parent"]()
        if parent is None:
            return {"action": "block", "message": "Research author session is unavailable"}
        budget = _sync(parent, binding)
        if not budget["within_budget"]:
            return {
                "action": "block",
                "message": "Research budget exhausted; preserve the checkpoint and stop",
            }
        waiting = tool_name == "medical_research_review" and (args or {}).get("action") in {
            "status",
            "abandon",
        }
        if binding["reviewing"] and not waiting:
            return {
                "action": "block",
                "message": (
                    "A native review is running. Only medical_research_review action=status "
                    f"(wait_seconds<={WAIT_MAX}) is available until it settles."
                ),
            }
        if tool_name == "delegate_task":
            return {
                "action": "block",
                "message": "Use medical_research_review for metered delegation",
            }
    return None


def register(ctx):
    ctx.register_hook("pre_tool_call", pre_tool_call)
    for name, handler, properties, description in (
        (
            "medical_research_session",
            session,
            {"total_turns": {"type": "integer", "minimum": 1}},
            "Bind/resume a medical report's shared host turn budget",
        ),
        (
            "medical_research_review",
            review,
            {
                "review_turns": {"type": "integer", "minimum": 1},
                "action": {"type": "string", "enum": ["start", "status", "abandon"]},
                "wait_seconds": {"type": "integer", "minimum": 0, "maximum": WAIT_MAX},
                "task_id": {"type": "string"},
            },
            "Run a fresh native evidence reviewer within the report's shared turn budget; "
            "poll with action=status; abandon only a reviewer the host never settled",
        ),
        (
            CHECK_TOOL,
            review_check,
            {"task_id": {"type": "string"}},
            "Validate a native reviewer's result files against its frozen packet without "
            "recording anything; lists every problem with hints",
        ),
    ):

        def handle(args, _handler=handler, **kwargs):
            try:
                return json.dumps(_handler(args, **kwargs))
            except (ValidationError, ValueError, OSError, KeyError, TypeError) as exc:
                return json.dumps({"error": str(exc)})

        schema = {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {"run_dir": {"type": "string"}, **properties},
                "required": ["run_dir"],
            },
        }
        ctx.register_tool(name=name, toolset="medical_research", schema=schema, handler=handle)
