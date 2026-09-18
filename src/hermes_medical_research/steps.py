"""User-invoked steps.

A step is one operator act: ``/hmr-selector`` screens what is screenable and stops.  There is no
cron, no persistent bot and no autonomous claimant, so nothing advances a Review unless someone asks
for it.  Inside a step every item is still one claim, one lease and one ``TaskEngine.submit``; only
the surface that produces the answer changes:

``call``
    One fresh tool-free Hermes session per item whose profile carries that kind's answer schema, so
    the answer arrives shape-constrained and this runner writes only the fields it owns.
``agent``
    One fresh Hermes session per item with the role's skill and tools, for work that must read
    arbitrary full text.  Corrections always take this lane: a correction re-mints the same packet,
    and a deterministic single call would repeat the rejected answer until the Cycle halts.
``none``
    No model at all.  Search replays a frozen plan and full-text acquisition is an HTTP fetch.

A Hermes quick command is killed after 30 seconds and streams nothing, so a step detaches a worker
and returns one line; progress, failures and the next command live in a status file that
``hmr step status`` reads.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import subprocess
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from . import answers, hermes
from .answers import CALL_KINDS
from .automation import AutomationEngine
from .tasks import (
    KIND_COMMANDS,
    ROLE_COMMANDS,
    ROLE_PROFILES,
    Actor,
    RunCatalog,
    TaskEngine,
    ValidationError,
    hmr_command,
)

# step -> (role, the kinds it owns)
STEPS: dict[str, tuple[str, tuple[str, ...]]] = {
    "search": ("searcher", ("search",)),
    "select": ("selector", ("screening", "coverage")),
    "extract": ("extractor", ("fulltext", "studies", "assessment")),
    "synthesize": ("synthesizer", ("synthesis",)),
    "audit": ("auditor", ("audit",)),
}
DETERMINISTIC_KINDS = ("search", "fulltext")
ROLE_BY_KIND = {kind: STEPS[step][0] for step in STEPS for kind in STEPS[step][1]}
# The slash command each step is installed as, so the operator is told what to type next.
STEP_COMMANDS = {
    "search": "/hmr-search",
    "select": "/hmr-selector",
    "extract": "/hmr-extractor",
    "synthesize": "/hmr-synthesizer",
    "audit": "/hmr-auditor",
}
# Two constrained attempts, then one session with tools, then the durable failure path.
CALL_ATTEMPTS = 2
AGENT_ATTEMPTS = 3
DEFAULT_MAX_WAIT_MINUTES = 40.0
STATUS_FAILURE_LIMIT = 20
# A transient store lock is retried; a queue that reports work but never yields it is not.
CLAIM_RETRIES = 5
CLAIM_RETRY_SECONDS = 30
IDLE_ROUNDS = 3
TERMINAL_STATES = {"drained", "limit_reached", "stopped", "paused", "blocked", "refused", "failed"}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _at(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


# -- paths ------------------------------------------------------------------------------------


def steps_root(store: Path) -> Path:
    return Path(store) / "steps"


def active_pointer(store: Path) -> Path:
    return steps_root(store) / "active.json"


def step_dir(store: Path, review: str, step: str) -> Path:
    return steps_root(store) / review / step


def status_path(store: Path, review: str, step: str) -> Path:
    return step_dir(store, review, step) / "status.json"


def stop_path(store: Path, review: str, step: str) -> Path:
    return step_dir(store, review, step) / "stop"


def current_path(store: Path, step: str) -> Path:
    """Where the tool server looks for the claim this step is working on."""
    return steps_root(store) / step / "current.json"


def lock_path(store: Path, step: str) -> Path:
    return steps_root(store) / f"{step}.lock"


def _write_json(path: Path, payload: dict[str, Any], *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    if private:
        temp.chmod(0o600)
    os.replace(temp, path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# -- the active Review ------------------------------------------------------------------------


def set_active_review(store: Path, name: str | None, *, actor: str = "operator") -> dict[str, Any]:
    """Point every argument-less step at one Review, or clear the pointer."""
    path = active_pointer(store)
    if name is None:
        path.unlink(missing_ok=True)
        return {"active_review": None}
    AutomationEngine(Path(store)).review_status(name)
    _write_json(path, {
        "schema_version": "1",
        "review": name,
        "set_at": _at(_utc_now()),
        "set_by": actor,
    })
    return {"active_review": name}


def active_review(store: Path, explicit: str | None = None) -> str:
    """``--review``, then the stored pointer, then the only Review that has a live Cycle."""
    if explicit:
        return explicit
    pointed = _read_json(active_pointer(store)).get("review")
    if pointed:
        return str(pointed)
    engine = AutomationEngine(Path(store))
    reviews = engine.list_reviews()["reviews"]
    live = [
        item["name"]
        for item in reviews
        if item.get("active_cycle") is not None and item.get("state") != "paused"
    ]
    if len(live) == 1:
        return live[0]
    names = [item["name"] for item in reviews]
    raise ValidationError(
        "no active review; choose one with `hmr step use NAME`"
        + (f" (reviews: {', '.join(names)})" if names else " after creating one")
    )


# -- lanes ------------------------------------------------------------------------------------


def lane_for(kind: str, *, packet: dict[str, Any] | None = None,
             correction: bool = False, assignment: dict[str, Any] | None = None) -> str:
    """Which surface answers this item, and why, deterministically."""
    if kind in DETERMINISTIC_KINDS:
        return "none"
    if correction:
        return "agent"
    if kind not in CALL_KINDS:
        return "agent"
    configured = ((assignment or {}).get("kinds") or {}).get(kind, {}).get("lane")
    if configured in {"call", "agent"}:
        return configured
    if kind == "synthesis" and packet is not None and packet.get("text_limit") not in (None, 600):
        # The packet was shortened to fit, so the rows it shows are summaries and the finding needs
        # source reads that a single call cannot make.
        return "agent"
    return "call"


# -- status -----------------------------------------------------------------------------------


def latest_cycle(review_state: dict[str, Any]) -> dict[str, Any]:
    """The Review's active Cycle, or its most recent one when none is active."""
    cycles = review_state.get("cycles")
    if not isinstance(cycles, list) or not cycles:
        return {}
    active = [cycle for cycle in cycles if cycle.get("status") == "active"]
    return active[-1] if active else cycles[-1]


def _stage_progress(store: Path, review: str, step: str) -> dict[str, Any]:
    engine = AutomationEngine(Path(store))
    try:
        status = engine.review_status(review)
    except ValidationError:
        return {}
    cycle = latest_cycle(status)
    run_id = cycle.get("run_id")
    if not run_id:
        return {}
    workspace = RunCatalog(Path(store)).workspace(run_id)
    manifest = workspace.manifest_view()
    datasets = manifest.get("datasets") or {}
    total = len(workspace.rows("records", fresh=False)) if "records" in datasets else 0
    decided = len(workspace.rows("screening", fresh=False)) if "screening" in datasets else 0
    return {
        "run_id": run_id,
        "cycle_id": cycle.get("cycle_id"),
        "cycle_status": cycle.get("status"),
        "records": total,
        "screened": decided,
    }


def read_status(store: Path, review: str, step: str) -> dict[str, Any]:
    status = _read_json(status_path(store, review, step))
    if status.get("state") in {"starting", "running"} and not _alive(status.get("pid")):
        status = {**status, "state": "interrupted"}
    return status


def _alive(pid: Any) -> bool:
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def status_view(store: Path, review: str | None = None) -> dict[str, Any]:
    """Everything ``hmr step status`` prints, cheap enough for a 30-second quick command."""
    store = Path(store)
    name = active_review(store, review)
    engine = AutomationEngine(store)
    review_state = engine.review_status(name)
    steps = {step: read_status(store, name, step) for step in STEPS}
    notifications = engine.notification_probe()
    next_task = None
    cycle = latest_cycle(review_state)
    if cycle.get("run_id"):
        ledger = TaskEngine(RunCatalog(store).workspace(cycle["run_id"])).status()
        engine = TaskEngine(RunCatalog(store).workspace(cycle["run_id"]))
        pending = [task for task in ledger["active"]
                   if task["state"] in {"pending", "in_progress"}]
        if pending:
            # ``status`` carries the role but not the kind, and the kind is what names the step.
            detail = engine.task_automation(pending[0]["task_id"])
            kind = detail.get("kind", "")
            next_task = {
                "kind": kind,
                "role": pending[0]["role"],
                "step": KIND_COMMANDS.get(kind, ""),
            }
    return {
        "review": name,
        "state": review_state.get("state"),
        "cycle": cycle,
        "steps": steps,
        "next": next_task,
        "notifications": notifications,
    }


# -- starting and stopping --------------------------------------------------------------------


def start(
    step: str,
    *,
    store: Path,
    review: str | None = None,
    hermes_home: Path | None = None,
    limit: int | None = None,
    max_wait: float = DEFAULT_MAX_WAIT_MINUTES,
    spawn: Callable[..., subprocess.Popen[bytes]] | None = None,
) -> dict[str, Any]:
    """Detach a worker and return at once, because a quick command has 30 seconds."""
    if step not in STEPS:
        raise ValidationError(f"unknown step: {step}; steps are {', '.join(STEPS)}")
    store = Path(store)
    name = active_review(store, review)
    current = read_status(store, name, step)
    if current.get("state") in {"starting", "running"}:
        return {
            "step": step,
            "review": name,
            "state": current["state"],
            "already_running": True,
            "job_id": current.get("job_id"),
            "processed": current.get("processed", 0),
            "next": "watch it with `hmr step status`",
        }
    job_id = "job-" + uuid4().hex[:12]
    logs = step_dir(store, name, step) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log_file = logs / f"{job_id}.log"
    stop_path(store, name, step).unlink(missing_ok=True)
    _write_json(status_path(store, name, step), {
        "step": step,
        "review": name,
        "state": "starting",
        "job_id": job_id,
        "started_at": _at(_utc_now()),
        "processed": 0,
        "failed": 0,
        "log": str(log_file),
        "progress": _stage_progress(store, name, step),
    })
    argv = [
        hmr_command().strip("'"),
        "--store", str(store),
        "step", "run", step,
        "--review", name,
        "--job", job_id,
        "--max-wait", str(max_wait),
    ]
    if hermes_home is not None:
        argv += ["--hermes-home", str(hermes_home)]
    if limit is not None:
        argv += ["--limit", str(limit)]
    launcher = spawn or _spawn
    with open(log_file, "ab", buffering=0) as handle:
        launcher(argv, stdout=handle, stderr=handle)
    return {
        "step": step,
        "review": name,
        "state": "starting",
        "job_id": job_id,
        "log": str(log_file),
        "next": "watch it with `hmr step status`",
    }


def _spawn(argv: list[str], *, stdout: Any, stderr: Any) -> subprocess.Popen[bytes]:
    """Start the worker so neither the 30-second timeout nor its process group can reach it.

    ``start_new_session`` puts the worker in its own session, so killing the quick command's
    process group leaves it running, and binding both streams to the log file matters as much: an
    inherited pipe would keep the Hermes CLI blocked in ``communicate()`` after its timeout.
    """
    return subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
        close_fds=True,
    )


def request_stop(store: Path, *, review: str | None = None, step: str | None = None
                 ) -> dict[str, Any]:
    """Ask running steps to finish the item in flight and stop."""
    store = Path(store)
    name = active_review(store, review)
    asked = []
    for candidate in ([step] if step else list(STEPS)):
        status = read_status(store, name, candidate)
        if status.get("state") in {"starting", "running"}:
            path = stop_path(store, name, candidate)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_at(_utc_now()) + "\n")
            asked.append(candidate)
    return {"review": name, "stop_requested": asked}


# -- the loop ---------------------------------------------------------------------------------


async def run_step(
    step: str,
    *,
    store: Path,
    review: str | None = None,
    hermes_home: Path | None = None,
    hermes_executable: Path | None = None,
    job_id: str | None = None,
    limit: int | None = None,
    max_wait: float = DEFAULT_MAX_WAIT_MINUTES,
    invoke: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    call: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], datetime] = _utc_now,
) -> dict[str, Any]:
    """Work one step's queue until it is empty, interrupted, capped, or blocked."""
    if step not in STEPS:
        raise ValidationError(f"unknown step: {step}; steps are {', '.join(STEPS)}")
    role, _kinds = STEPS[step]
    store = Path(store)
    name = active_review(store, review)
    home = hermes.profile_home(hermes_home)
    executable = str(hermes_executable or hermes.hermes_executable())
    assignment = hermes.step_assignment(home)
    lock = FileLock(str(lock_path(store, step)), timeout=0)
    steps_root(store).mkdir(parents=True, exist_ok=True)
    state = {
        "step": step,
        "review": name,
        "state": "running",
        "job_id": job_id,
        "pid": os.getpid(),
        "started_at": _at(clock()),
        "processed": 0,
        "failed": 0,
        "lanes": {},
        "failures": [],
    }
    try:
        lock.acquire()
    except FileLockTimeout:
        refused = {**state, "state": "refused",
                   "message": f"another {step} runner holds {lock_path(store, step).name}"}
        _write_json(status_path(store, name, step), refused)
        return refused

    automation = AutomationEngine(store, clock=clock)
    claim_errors = 0
    idle_rounds = 0
    try:
        _recover(automation, store, name, step, role)
        await automation.tick()
        state["progress"] = _stage_progress(store, name, step)
        _write_json(status_path(store, name, step), state)
        while True:
            if stop_path(store, name, step).is_file():
                state["state"] = "stopped"
                break
            if limit is not None and state["processed"] + state["failed"] >= limit:
                state["state"] = "limit_reached"
                break
            actor = Actor(ROLE_PROFILES[role], f"step-{step}-{uuid4().hex}", role)
            try:
                claim = automation.claim(ROLE_COMMANDS[role], actor, review=name)
            except (FileLockTimeout, ValidationError) as exc:
                # A busy store lock is transient; only a persistent one ends the step.
                claim_errors += 1
                if claim_errors > CLAIM_RETRIES:
                    raise
                state["last_claim_error"] = str(exc)[:200]
                await sleep(CLAIM_RETRY_SECONDS)
                continue
            claim_errors = 0
            if claim.get("state") == "idle":
                waited = await _wait_for_backoff(
                    automation, role, name, max_wait=max_wait, sleep=sleep, clock=clock,
                    stop=stop_path(store, name, step),
                )
                idle_rounds = idle_rounds + 1 if waited else 0
                if waited and idle_rounds <= IDLE_ROUNDS:
                    continue
                state["state"] = "drained"
                break
            idle_rounds = 0
            started = clock()
            item = await _work_one(
                automation=automation, claim=claim, actor=actor, role=role, step=step,
                store=store, review=name, executable=executable, home=home,
                assignment=assignment, invoke=invoke, call=call, clock=clock,
            )
            seconds = round((clock() - started).total_seconds(), 2)
            state["lanes"][item["lane"]] = state["lanes"].get(item["lane"], 0) + 1
            if item["accepted"]:
                state["processed"] += 1
            else:
                state["failed"] += 1
                state["failures"] = (state["failures"] + [{
                    "task_id": claim["task_id"], "kind": claim.get("kind"),
                    "target_ids": claim.get("target_ids"), "lane": item["lane"],
                    "code": item.get("code"), "message": (item.get("message") or "")[:400],
                    "at": _at(clock()),
                }])[-STATUS_FAILURE_LIMIT:]
            state["last_seconds"] = seconds
            state["progress"] = _stage_progress(store, name, step)
            _write_json(status_path(store, name, step), state)
            if item.get("blocked"):
                state["state"] = "blocked"
                break
        await automation.tick()
    except Exception as exc:  # the worker is detached, so the status file is the only report
        state.update(state="failed", message=f"{type(exc).__name__}: {exc}")
        _write_json(status_path(store, name, step), state)
        raise
    finally:
        current_path(store, step).unlink(missing_ok=True)
        lock.release()
    state["finished_at"] = _at(clock())
    state["progress"] = _stage_progress(store, name, step)
    state.pop("pid", None)
    _write_json(status_path(store, name, step), state)
    return state


def _recover(automation: AutomationEngine, store: Path, review: str, step: str, role: str) -> None:
    """Return a claim a killed runner left leased. Holding the step lock proves nobody owns it."""
    stale = _read_json(current_path(store, step))
    claim_id = stale.get("claim_id")
    if not claim_id:
        return
    actor = Actor(stale.get("actor_profile") or ROLE_PROFILES[role],
                  stale.get("session_id", ""), role)
    with contextlib.suppress(ValidationError):
        # A claim the queue already expired or failed needs no release.
        automation.release(claim_id, actor, reason="step_interrupted")
    current_path(store, step).unlink(missing_ok=True)


async def _wait_for_backoff(
    automation: AutomationEngine, role: str, review: str, *, max_wait: float,
    sleep: Callable[[float], Awaitable[None]], clock: Callable[[], datetime], stop: Path,
) -> bool:
    """Sleep out a bounded retry backoff, so one bad item does not end a 170-item step."""
    from .automation import _parse

    available = automation.next_available_at(role, review)
    if not available:
        return False
    target = _parse(available)
    delay = (target - clock()).total_seconds()
    if delay <= 0:
        return True
    if delay > max_wait * 60:
        return False
    while clock() < target:
        if stop.is_file():
            return False
        await sleep(min(5.0, (target - clock()).total_seconds()))
        if clock() >= target:
            break
        if not _advanced(clock, target):
            # The clock did not move while sleeping, so waiting again would spin.
            return False
    return True


def _advanced(clock: Callable[[], datetime], target: datetime) -> bool:
    """Whether time is passing at all, so a bounded wait cannot become a busy loop."""
    first = clock()
    return clock() >= first and first < target


async def _work_one(
    *,
    automation: AutomationEngine,
    claim: dict[str, Any],
    actor: Actor,
    role: str,
    step: str,
    store: Path,
    review: str,
    executable: str,
    home: Path,
    assignment: dict[str, Any],
    invoke: Callable[..., subprocess.CompletedProcess[str]] | None,
    call: Callable[..., subprocess.CompletedProcess[str]] | None,
    clock: Callable[[], datetime],
) -> dict[str, Any]:
    """One claimed Task: pick the lane, run it, and let the ledger decide the outcome."""
    engine = TaskEngine(RunCatalog(store).workspace(claim["run_id"]))
    kind = claim.get("kind", "")
    task = engine.task_automation(claim["task_id"])
    packet = _read_json(Path(claim["packet_path"])) if claim.get("packet_path") else {}
    lane = lane_for(kind, packet=packet, correction=bool(task.get("correction_for")),
                    assignment=assignment)
    last_error = ""
    if lane == "none":
        try:
            await _submit_unchanged(engine, claim, actor, kind)
            return {"lane": lane, "accepted": True}
        except ValidationError as exc:
            last_error = str(exc)
    else:
        _write_json(current_path(store, step), {
            "claim_id": claim["claim_id"],
            "claim_token": claim["claim_token"],
            "run_id": claim["run_id"],
            "task_id": claim["task_id"],
            "role": role,
            "kind": kind,
            "actor_profile": actor.profile,
            "session_id": actor.session_id,
            "packet_path": claim.get("packet_path"),
            "proposal_path": claim["proposal_path"],
            "expires_at": claim["expires_at"],
        }, private=True)
        attempts = [lane] * (CALL_ATTEMPTS if lane == "call" else AGENT_ATTEMPTS)
        if lane == "call":
            attempts.append("agent")  # escalate once to a session that can read and explain
        hint: str | None = None
        for attempt_lane in attempts:
            try:
                completed = await hermes.run_with_renewal(
                    automation, claim, actor,
                    runner=_runner(attempt_lane, kind, packet, executable, home, assignment,
                                   invoke=invoke, call=call, hint=hint),
                )
                last_error = (completed.stderr or completed.stdout or "").strip()[-500:]
            except subprocess.TimeoutExpired:
                last_error = f"{attempt_lane} session exceeded its timeout"
                continue
            except ValidationError as exc:
                # A lost lease or a busy lock ends this attempt, not the step.
                last_error = str(exc)[-500:]
                continue
            if attempt_lane == "call":
                try:
                    await _record_answer(engine, claim, actor, kind, packet,
                                         completed.stdout or "")
                except ValidationError as exc:
                    # The next attempt carries the reason, which is the only thing a stateless
                    # call can act on; the cached prompt prefix is unchanged.
                    hint = str(exc)
                    last_error = hint[-500:]
            if engine.task_automation(claim["task_id"])["state"] != "in_progress":
                break
        if engine.task_automation(claim["task_id"])["state"] == "accepted":
            current_path(store, step).unlink(missing_ok=True)
            return {"lane": lane, "accepted": True}
    current_path(store, step).unlink(missing_ok=True)
    state = engine.task_automation(claim["task_id"])["state"]
    if state == "accepted":
        return {"lane": lane, "accepted": True}
    code = f"{kind or role}_not_recorded"
    result: dict[str, Any] = {"lane": lane, "accepted": False, "code": code,
                              "message": last_error or f"task stayed {state}"}
    try:
        failure = automation.fail(claim["claim_id"], actor, code=code,
                                  message=result["message"] or code)
        result["blocked"] = failure.get("blocked", False)
    except ValidationError as exc:
        # The session already failed the claim itself, or the Task was superseded.
        result["fail_not_recorded"] = str(exc)
    return result


def _runner(
    lane: str, kind: str, packet: dict[str, Any], executable: str, home: Path,
    assignment: dict[str, Any], *,
    invoke: Callable[..., subprocess.CompletedProcess[str]] | None,
    call: Callable[..., subprocess.CompletedProcess[str]] | None,
    hint: str | None = None,
) -> Callable[[dict[str, Any], Actor], subprocess.CompletedProcess[str]]:
    if lane == "call":
        runner = call or hermes.invoke_call_session
        profile = hermes.profile_for_kind(kind, assignment)
        prefix, tail = answers.build_prompt(kind, packet, hint=hint)
        prompt = f"{prefix}\n\n{tail}"

        def run_call(claim: dict[str, Any], actor: Actor) -> subprocess.CompletedProcess[str]:
            return runner(executable, home, profile, prompt, kind)

        return run_call
    session = invoke or hermes.invoke_task_session
    role = ROLE_BY_KIND.get(kind, "")

    def run_agent(claim: dict[str, Any], actor: Actor) -> subprocess.CompletedProcess[str]:
        return session(executable, home, claim, actor, role)

    return run_agent


async def _record_answer(
    engine: TaskEngine,
    claim: dict[str, Any],
    actor: Actor,
    kind: str,
    packet: dict[str, Any],
    text: str,
) -> None:
    """Map a constrained answer into the task's proposal and submit it, unchanged path.

    The model never edits the proposal and never runs a command: it answers, and this writes only
    the fields its schema owns. A rejection is raised with the validator's own message so the next
    attempt can carry it.
    """
    answer = answers.parse_answer(kind, text)
    proposal_path = Path(claim["proposal_path"])
    updated = answers.apply_result(kind, _read_json(proposal_path), answer, packet)
    proposal_path.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n")
    outcome = await engine.submit(
        KIND_COMMANDS[kind], claim["task_id"], proposal_path, actor,
        claim_token=claim["claim_token"],
    )
    if outcome.get("accepted") is False:
        raise ValidationError(
            "; ".join(str(error) for error in (outcome.get("errors") or [])[:6])
        )


async def _submit_unchanged(
    engine: TaskEngine, claim: dict[str, Any], actor: Actor, kind: str
) -> None:
    """Search and full-text acquisition need no model: the pre-filled proposal is the answer."""
    proposal = Path(claim["proposal_path"])
    if kind == "search":
        await engine.execute_search_plan(
            claim["task_id"], actor,
            proposal_path=None if claim.get("resume") else proposal,
            claim_token=claim["claim_token"],
        )
        return
    outcome = await engine.submit(
        KIND_COMMANDS[kind], claim["task_id"], proposal, actor,
        claim_token=claim["claim_token"],
    )
    if outcome.get("accepted") is False:
        raise ValidationError("; ".join(str(error) for error in (outcome.get("errors") or [])))




def quick_command_specs(store: Path, *, review: str | None = None) -> dict[str, dict[str, str]]:
    """The managed Hermes quick commands, one per step plus the operator's read-only views."""
    executable = hmr_command().strip("'")
    base = f"{shlex.quote(executable)} --store {shlex.quote(str(Path(store)))}"
    scope = f" --review {shlex.quote(review)}" if review else ""
    specs = {
        "hmr-status": f"{base} step status{scope}",
        "hmr-next": f"{base} step next{scope}",
        "hmr-stop": f"{base} step stop{scope}",
        "hmr-finalize": f"{base} step finalize{scope}",
        "hmr-retry": f"{base} step retry{scope}",
    }
    for step, label in STEP_COMMANDS.items():
        specs[label.lstrip("/")] = f"{base} step {step}{scope}"
    return {name: {"type": "exec", "command": command} for name, command in sorted(specs.items())}

# -- operator-facing text ---------------------------------------------------------------------


def render_started(result: dict[str, Any]) -> str:
    if result.get("already_running"):
        return (
            f"{result['step']} is already {result['state']} on {result['review']} "
            f"({result.get('processed', 0)} done); watch it with /hmr-status"
        )
    return (
        f"started {result['step']} on {result['review']} as {result['job_id']}; "
        f"watch it with /hmr-status"
    )


def render_next(view: dict[str, Any]) -> str:
    following = view.get("next")
    if not following:
        cycle = view.get("cycle") or {}
        return (
            f"review {view['review']} has nothing routed "
            f"(cycle {cycle.get('number', '?')} is {cycle.get('status', 'unknown')})"
        )
    label = STEP_COMMANDS.get(following["step"], following["step"] or "hmr step status")
    return f"next: {following['kind']} ({following['role']}) — run {label}"


def render_status(view: dict[str, Any]) -> str:
    cycle = view.get("cycle") or {}
    state = view.get("state")
    # A paused Review refuses every claim, so it is the first thing an operator needs to see.
    paused = " · paused" if state and state != "active" else ""
    lines = [
        f"review {view['review']}{paused} · cycle {cycle.get('number', '?')} · "
        f"{cycle.get('status', 'unknown')} · {cycle.get('run_id', 'no run')}"
    ]
    if paused:
        lines.append(f"the review is {state}; run `hmr review resume {view['review']}` to work it")
    for step, status in view["steps"].items():
        if not status:
            continue
        progress = status.get("progress") or {}
        counted = ""
        if progress.get("records"):
            counted = f"{progress.get('screened', 0)}/{progress['records']} screened · "
        seconds = status.get("last_seconds")
        pace = f"{seconds} s/item · " if seconds else ""
        lines.append(
            f"{step:<11}{status.get('state', '?'):<9} {counted}{pace}"
            f"{status.get('processed', 0)} done · {status.get('failed', 0)} failed"
            + (f" · job {status['job_id']}" if status.get("job_id") else "")
        )
        for failure in (status.get("failures") or [])[-3:]:
            lines.append(f"  ! {failure.get('code')}: {failure.get('message', '')[:120]}")
    lines.append(render_next(view))
    pending = (view.get("notifications") or {}).get("count")
    if pending:
        lines.append(f"notifications: {pending} pending (hmr review acknowledge EVENT_ID)")
    return "\n".join(lines)


def render_prompt(store: Path, step: str, *, review: str | None = None) -> str:
    """Show the prompt a constrained call would send for whatever is routed now."""
    name = active_review(Path(store), review)
    state = AutomationEngine(Path(store)).review_status(name)
    cycle = latest_cycle(state)
    if not cycle.get("run_id"):
        return f"review {name} has no active run"
    engine = TaskEngine(RunCatalog(Path(store)).workspace(cycle["run_id"]))
    role = STEPS[step][0]
    pending = [
        task for task in engine.status()["active"]
        if task["role"] == role and task["state"] in {"pending", "in_progress"}
    ]
    if not pending:
        return f"nothing is routed for {step} in review {name}"
    task = engine.task_automation(pending[0]["task_id"])
    kind = task.get("kind", "")
    if kind not in CALL_KINDS:
        return f"{kind} is answered by a session, so it has no constrained prompt"
    packet = _read_json(Path(task["packet_path"])) if task.get("packet_path") else {}
    if not packet:
        packet = _read_json(
            Path(RunCatalog(Path(store)).workspace(cycle["run_id"]).path)
            / "tasks" / pending[0]["task_id"] / "packet.json"
        )
    prefix, tail = answers.build_prompt(kind, packet)
    return f"{prefix}\n\n{tail}"


async def finalize(store: Path, *, review: str | None = None) -> dict[str, Any]:
    """Finalize the active Cycle's Run once every stage is accepted."""
    name = active_review(Path(store), review)
    state = AutomationEngine(Path(store)).review_status(name)
    cycle = latest_cycle(state)
    if not cycle.get("run_id"):
        raise ValidationError(f"review {name} has no run to finalize")
    engine = TaskEngine(RunCatalog(Path(store)).workspace(cycle["run_id"]))
    actor = Actor(ROLE_PROFILES["coordinator"], f"step-finalize-{os.getpid()}", "coordinator")
    return await engine.finalize(actor, offline=False)
