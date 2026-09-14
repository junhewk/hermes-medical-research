"""Codex/Claude lifecycle bridge. Count each local tool invocation conservatively.

No transcript inference, credentials, or model client. Host-generated lifecycle IDs
bind the reviewer receipt. Hook coverage is a host contract, not a security boundary.
Codex shows `systemMessage` only to the user, so every model-facing notice travels as
`additionalContext` on PreToolUse/PostToolUse or as a `decision` on stop events.
"""

from __future__ import annotations

import json
import re
import shlex
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from filelock import FileLock

from hermes_medical_search.artifacts import RunStore
from hermes_medical_search.models import ValidationError

from . import native_review
from .workspace import Workspace, now

REGISTRY = ".medical-research-native.json"
UNIT = "local tool invocations (conservative tool-turn accounting)"
REVIEWER = "medical-evidence-reviewer"
SPAWN_TOOLS = {"Agent", "spawn_agent", "Task"}
WAIT_TOOLS = {"wait_agent", "Agent", "Task"}
ACKNOWLEDGING = {"status", "check", "next", "finalize"}
REVIEWER_INACTIVE_SECONDS = 60
NO_REVIEWER_SECONDS = 120
SHELL_META = re.compile(r"[;&|<>`$\n]")
PATCH_HEADER = re.compile(r"\*\*\* (Add File|Update File|Delete File): (.+)")


def deny(message):
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": message,
        }
    }


def _context(kind, text):
    return {"hookSpecificOutput": {"hookEventName": kind, "additionalContext": text}}


def _words(event):
    if event.get("tool_name") != "Bash":
        return None
    command = event.get("tool_input", {}).get("command", "")
    if not isinstance(command, str) or SHELL_META.search(command):
        return None
    try:
        return shlex.split(command)
    except ValueError:
        return None


def _cli_call(event, action):
    """Return (cli_prefix, args) for `<cli...> research <action> ARGS...`, else None."""
    words = _words(event)
    if not words:
        return None
    for i in range(len(words) - 1):
        if words[i : i + 2] == ["research", action]:
            return words[:i], words[i + 2 :]
    return None


def _bootstrap(event):
    call = _cli_call(event, "host-session")
    if not call or not call[1]:
        return None
    prefix, args = call
    total = int(args[args.index("--total-turns") + 1]) if "--total-turns" in args else 150
    path = Path(args[0]).expanduser()
    if not path.is_absolute():
        path = Path(event["cwd"]) / path
    return path.resolve(), total, prefix


def _patch_ok(patch, directory, cwd=None):
    lines = patch.splitlines() if isinstance(patch, str) else []
    if len(lines) < 3 or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        return False
    files = 0
    for line in lines[1:-1]:
        if not line.startswith("*** "):
            continue
        header = PATCH_HEADER.fullmatch(line)
        if header:
            if not native_review.is_result_path(header.group(2).strip(), directory, cwd):
                return False
            files += 1
        elif line != "*** End of File":
            return False
    return files >= 1


def _read_only(event, directory, entry=None):
    """Reviewer policy: read frozen task files, write result files, run the check command.

    Relative paths resolve against the host-reported cwd.
    """
    name = event.get("tool_name")
    args = event.get("tool_input", {})
    cwd = event.get("cwd")
    if name in {"Write", "Edit"}:
        return native_review.is_result_path(args.get("file_path", ""), directory, cwd)
    if name == "apply_patch":
        return _patch_ok(args.get("command", ""), directory, cwd)
    if name == "Read":
        return native_review.is_task_file(args.get("file_path", ""), directory, cwd)
    if name != "Bash":
        return False
    words = _words(event)
    if not words:
        return False
    if len(words) == 2 and words[0] == "cat":
        return native_review.is_task_file(words[1], directory, cwd)
    if (
        len(words) == 4
        and words[:2] == ["sed", "-n"]
        and re.fullmatch(r"[0-9]+(?:,[0-9]+)?p", words[2])
    ):
        return native_review.is_task_file(words[3], directory, cwd)
    prefix = (entry or {}).get("cli_prefix")
    if prefix is not None:
        call = _cli_call(event, "review-check")
        if call and call[0] == list(prefix) and len(call[1]) == 1:
            try:
                target = native_review._absolute(call[1][0], cwd)
            except (TypeError, ValueError, OSError):
                return False
            return target is not None and target == Path(directory).resolve()
    return False


def _reviewer_denial(entry):
    directory = Path(entry["packet_path"]).parent
    check = native_review._command_text(entry.get("check_command"))
    return (
        f"Reviewer may only read files under {directory}, write {directory}/result.json or "
        f"{directory}/results/<finding_id>.json" + (f", and run `{check}`." if check else ".")
    )


def _seconds_since(stamp):
    if not stamp:
        return None
    try:
        return (datetime.now(UTC) - datetime.fromisoformat(stamp)).total_seconds()
    except (TypeError, ValueError):
        return None


def _notice(workspace, entry):
    """Model-facing review state for the author until it inspects the run."""
    summary = native_review.summary(workspace)
    latest = summary.get("latest_task")
    if not latest:
        return None
    acknowledged = latest["task_id"] in entry.get("acknowledged", [])
    if latest["state"] == "running" or not acknowledged:
        return f"Native review {latest['task_id']} is {latest['state']}. {summary['guidance']}"
    return None


def _acknowledge(event, entry, workspace):
    """The author inspected the run (status/check/next/finalize): stop repeating the notice."""
    for action in ACKNOWLEDGING:
        call = _cli_call(event, action)
        if not call or not call[1]:
            continue
        if Path(call[1][0]).expanduser().resolve() != workspace.path.resolve():
            continue
        latest = (native_review.summary(workspace).get("latest_task") or {}).get("task_id")
        if latest:
            acknowledged = entry.setdefault("acknowledged", [])
            if latest not in acknowledged:
                acknowledged.append(latest)
        return


def handle(event, host, registry_dir=None):
    """Process one native lifecycle event, scoped to explicitly bound research sessions."""
    event = dict(event)
    name = event.get("tool_name") or ""
    for prefix in ("functions.collaboration.", "collaboration.", "functions."):
        if name.startswith(prefix):
            event["tool_name"] = name[len(prefix) :]
            break
    aliases = {
        "collaboration" + action: action
        for action in (
            "spawn_agent",
            "followup_task",
            "send_message",
            "wait_agent",
            "interrupt_agent",
        )
    }
    event["tool_name"] = aliases.get(event.get("tool_name"), event.get("tool_name"))
    cwd = Path(registry_dir or event["cwd"]).resolve()
    store = RunStore(cwd)
    kind = event.get("hook_event_name")
    bootstrap = _bootstrap(event) if kind == "PreToolUse" else None
    if not (cwd / REGISTRY).exists() and not bootstrap:
        return {}
    cwd.mkdir(parents=True, exist_ok=True)
    with FileLock(str(cwd / (REGISTRY + ".lock")), timeout=5):
        state = store.read_json(REGISTRY, default={"sessions": {}, "pending": {}})
        sid = event.get("agent_id") or event.get("session_id")
        parent_id = event.get("session_id")
        if bootstrap:
            run, total, cli_prefix = bootstrap
            run.mkdir(parents=True, exist_ok=True)
            if sid in state["sessions"] and state["sessions"][sid]["run_dir"] != str(run):
                return deny("One medical report per native session; start a fresh author session")
            if sid in state["sessions"] and state["sessions"][sid]["total"] != total:
                return deny("A resumed report retains its original total budget")
            state["sessions"].setdefault(
                sid,
                {"role": "author", "run_dir": str(run), "calls": [], "total": total, "host": host},
            )
            state["sessions"][sid].setdefault("cli_prefix", cli_prefix)
        if sid in state["sessions"] or parent_id in state["sessions"]:
            state.setdefault("events", []).append(
                {
                    k: event.get(k)
                    for k in (
                        "hook_event_name",
                        "tool_name",
                        "session_id",
                        "agent_id",
                        "tool_use_id",
                    )
                }
            )
            store.write_json(REGISTRY, state)
        if kind == "SubagentStart" and parent_id in state["pending"]:
            pending = state["pending"][parent_id]
            if not sid or sid == parent_id or sid in state["sessions"]:
                raise ValidationError("native host did not provide a fresh reviewer identity")
            state["sessions"][sid] = {**pending, "role": "reviewer", "calls": []}
            state["pending"][parent_id]["reviewer_session_id"] = sid
            store.write_json(REGISTRY, state)
            return _context(
                "SubagentStart",
                native_review.reviewer_prompt(
                    pending["packet_path"],
                    pending["allocated_turns"],
                    host,
                    pending.get("check_command"),
                ),
            )
        entry = state["sessions"].get(sid)
        if not entry:
            return {}
        workspace = Workspace(Path(entry["run_dir"]))
        if kind == "PreToolUse":
            return _pre_tool_use(event, host, state, store, sid, entry, workspace, bootstrap)
        if kind == "SubagentStop" and entry["role"] == "reviewer":
            return _subagent_stop(event, host, state, store, sid, entry, workspace)
        if kind == "PostToolUse":
            if entry["role"] == "reviewer":
                exhausted = len(entry["calls"]) >= entry["allocated_turns"]
            else:
                exhausted = (
                    native_review.budget_status(workspace.store.read_json(native_review.STATE))[
                        "remaining_turns"
                    ]
                    <= 0
                )
            if exhausted:
                return {
                    "continue": False,
                    "stopReason": "Shared medical report allocation exhausted",
                }
            if entry["role"] == "author" and event.get("tool_name") in WAIT_TOOLS:
                notice = _notice(workspace, entry)
                if notice:
                    return _context("PostToolUse", notice)
        if kind == "Stop" and entry["role"] == "author" and not event.get("stop_hook_active"):
            latest = native_review.summary(workspace).get("latest_task")
            if (
                latest
                and latest["state"] == "failed"
                and latest["task_id"] not in entry.get("acknowledged", [])
                and latest["task_id"] not in entry.get("stop_blocked", [])
            ):
                entry.setdefault("stop_blocked", []).append(latest["task_id"])
                store.write_json(REGISTRY, state)
                return {
                    "decision": "block",
                    "reason": _notice(workspace, entry)
                    + " Inspect `research check` before ending the turn.",
                }
        return {}


def _pre_tool_use(event, host, state, store, sid, entry, workspace, bootstrap):
    call_id = event.get("tool_use_id")
    if not call_id:
        return deny("The native host omitted the tool identity needed for shared accounting")
    if call_id not in entry["calls"]:
        if entry["role"] == "reviewer":
            if len(entry["calls"]) >= entry["allocated_turns"]:
                return deny("Reviewer allocation exhausted; return the review now")
        elif not bootstrap:
            budget = native_review.budget_status(workspace.store.read_json(native_review.STATE))
            if budget["remaining_turns"] <= 0:
                return deny("Shared report budget exhausted; preserve the checkpoint")
        entry["calls"].append(call_id)
    if entry["role"] == "reviewer":
        entry["last_call_at"] = now()
        store.write_json(REGISTRY, state)
        directory = Path(entry["packet_path"]).parent
        return {} if _read_only(event, directory, entry) else deny(_reviewer_denial(entry))
    _acknowledge(event, entry, workspace)
    store.write_json(REGISTRY, state)
    budget = native_review.bind(
        workspace,
        host=host,
        author_session_id=sid,
        total_turns=entry["total"],
        used_turns=len(entry["calls"]),
        unit=UNIT,
    )
    if bootstrap:
        token = uuid4().hex
        workspace.store.write_json("native-binding.json", {"token": token, "session_id": sid})
        updated = dict(event["tool_input"])
        updated["command"] += " --hook-token " + token
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": updated,
            }
        }
    abandon = _cli_call(event, "review-abandon")
    if abandon:
        return _abandon(event, state, store, sid, entry, workspace, abandon[1])
    name = event.get("tool_name", "")
    args = event.get("tool_input", {})
    if name in SPAWN_TOOLS:
        requested = args.get("subagent_type", "").split(":")[-1]
        if (host == "claude-code" and requested != REVIEWER) or sid in state["pending"]:
            return deny("Use one fresh medical-evidence-reviewer task at a time")
        if host == "codex" and not (
            args.get("fork_turns") == "none" or args.get("fork_context") is False
        ):
            return deny("Set the native spawn tool's fresh-context option explicitly")
        if args.get("model") or args.get("resume"):
            return deny("Use a fresh reviewer that inherits the host model")
        task = native_review.prepare(
            workspace,
            author_session_id=sid,
            review_turns=20,
            check_command=entry.get("cli_prefix"),
        )
        state["pending"][sid] = {**entry, **task, "parent_id": sid}
        store.write_json(REGISTRY, state)
        if host == "codex":
            # The collaboration transport is not the ordinary local-tool
            # argument decoder. Deliver its packet through SubagentStart.
            return _context("PreToolUse", "Review reserved; packet follows.")
        updated = dict(args)
        updated.update(prompt=task["prompt"], run_in_background=False)
        updated.pop("resume", None)
        updated.pop("model", None)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": updated,
            }
        }
    if name in {"followup_task", "send_message", "delegate_task"}:
        return deny(
            "Do not message, fork or re-task the reviewer; wait for it to stop, then read the "
            "recorded verdict with research check"
        )
    text = (
        f"Medical shared budget: {budget['remaining_turns']} local tool calls remain, "
        "including review."
    )
    notice = _notice(workspace, entry)
    return _context("PreToolUse", text + (" " + notice if notice else ""))


def _abandon(event, state, store, sid, entry, workspace, args):
    if len(args) < 2 or Path(args[0]).expanduser().resolve() != workspace.path:
        return deny("Usage: research review-abandon RUN TASK_ID for this report's run directory")
    task_id = args[1]
    review_state = workspace.store.read_json(native_review.STATE, default={"tasks": {}})
    task = review_state["tasks"].get(task_id)
    if not task or task["state"] != "running":
        return deny("Only a running native review can be abandoned")
    reviewers = [
        s
        for s in state["sessions"].values()
        if s.get("role") == "reviewer" and s.get("task_id") == task_id
    ]
    if reviewers:
        idle = [_seconds_since(r.get("last_call_at") or task["created_at"]) for r in reviewers]
        if any(i is None or i < REVIEWER_INACTIVE_SECONDS for i in idle):
            return deny(
                f"The reviewer was active within the last {REVIEWER_INACTIVE_SECONDS} seconds; "
                "wait for it to stop"
            )
    else:
        since = _seconds_since(task["created_at"])
        if since is None or since < NO_REVIEWER_SECONDS:
            return deny(
                f"Wait at least {NO_REVIEWER_SECONDS} seconds for the host to start the reviewer"
            )
    binding = workspace.store.read_json("native-binding.json", default={})
    if binding.get("session_id") != sid or not binding.get("token"):
        return deny("Only the bound author session can abandon its review")
    for r in reviewers:
        r["settled"] = True
    state["pending"].pop(sid, None)
    store.write_json(REGISTRY, state)
    updated = dict(event["tool_input"])
    updated["command"] += " --hook-token " + binding["token"]
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": updated,
        }
    }


def _subagent_stop(event, host, state, store, sid, entry, workspace):
    if entry.get("settled"):
        return {}
    directory = Path(entry["packet_path"]).parent
    payload, problems = native_review.collect_result(directory)
    review_state = workspace.store.read_json(native_review.STATE)
    task = review_state["tasks"].get(entry["task_id"])
    repairable = False
    if task and task["state"] == "running" and (payload is not None or problems):
        try:
            native_review.assert_fresh(workspace, task)
            problems = list(problems) + native_review.check_result(workspace, task, payload)
            repairable = bool(problems)
        except ValidationError:
            repairable = False
    headroom = entry["allocated_turns"] - len(entry["calls"])
    if repairable and headroom >= 2 and task.get("repairs", 0) < native_review.MAX_REPAIRS:
        repairs = native_review.note_repair(workspace, task_id=entry["task_id"], problems=problems)
        entry.update(repairs=repairs, stop_blocked_at=now(), calls_at_block=len(entry["calls"]))
        store.write_json(REGISTRY, state)
        return {
            "decision": "block",
            "reason": native_review.repair_text(
                problems, directory, entry.get("check_command"), host
            ),
        }
    try:
        result = native_review.finish(
            workspace,
            task_id=entry["task_id"],
            reviewer_session_id=sid,
            used_turns=len(entry["calls"]),
            result=payload,
            completed=payload is not None or bool(problems),
            problems=problems,
            native_metadata={
                "event": "SubagentStop",
                "agent_type": event.get("agent_type"),
                "stop_hook_active": event.get("stop_hook_active"),
                "repairs": entry.get("repairs", 0),
            },
        )
    except ValidationError as exc:
        result = {"accepted": False, "reason": str(exc)}
    task_state = workspace.store.read_json(native_review.STATE)["tasks"][entry["task_id"]]
    if task_state["state"] != "running":
        state["pending"].pop(entry["parent_id"], None)
        entry["settled"] = True
        entry["state"] = task_state["state"]
        store.write_json(REGISTRY, state)
    return {"systemMessage": "Medical review recorded: " + json.dumps(result)}
