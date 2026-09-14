"""Codex/Claude lifecycle bridge. Count each local tool invocation conservatively.

No transcript inference, credentials, or model client. Host-generated lifecycle IDs
bind the reviewer receipt. Hook coverage is a host contract, not a security boundary.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from uuid import uuid4

from filelock import FileLock

from hermes_medical_search.artifacts import RunStore
from hermes_medical_search.models import ValidationError

from . import native_review
from .workspace import Workspace

REGISTRY = ".medical-research-native.json"
UNIT = "local tool invocations (conservative tool-turn accounting)"
REVIEWER = "medical-evidence-reviewer"


def deny(message):
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": message,
        }
    }


def _bootstrap(event):
    if event.get("tool_name") != "Bash":
        return None
    command = event.get("tool_input", {}).get("command", "")
    if not isinstance(command, str) or re.search(r"[;&|<>`\n]", command):
        return None
    words = shlex.split(command)
    for i in range(len(words) - 2):
        if words[i : i + 2] == ["research", "host-session"]:
            total = (
                int(words[words.index("--total-turns") + 1]) if "--total-turns" in words else 150
            )
            path = Path(words[i + 2]).expanduser()
            if not path.is_absolute():
                path = Path(event["cwd"]) / path
            return path.resolve(), total
    return None


def _read_only(event, directory):
    if event.get("tool_name") == "Write":
        return (
            Path(event.get("tool_input", {}).get("file_path", "")).resolve()
            == directory / "result.json"
        )
    if event.get("tool_name") == "apply_patch":
        patch = event.get("tool_input", {}).get("command", "")
        lines = patch.splitlines()
        return (
            len(lines) >= 4
            and lines[0] == "*** Begin Patch"
            and lines[1] == f"*** Add File: {directory / 'result.json'}"
            and lines[-1] == "*** End Patch"
            and all(line.startswith("+") for line in lines[2:-1])
        )
    args = event.get("tool_input", {})
    if event.get("tool_name") == "Read":
        path = args.get("file_path", "")
    elif event.get("tool_name") == "Bash":
        command = args.get("command", "")
        if re.search(r"[;&|<>`$\n]", command):
            return False
        words = shlex.split(command)
        if len(words) == 2 and words[0] == "cat":
            path = words[1]
        elif (
            len(words) == 4
            and words[:2] == ["sed", "-n"]
            and re.fullmatch(r"[0-9]+(?:,[0-9]+)?p", words[2])
        ):
            path = words[3]
        else:
            return False
    else:
        return False
    return Path(path).is_absolute() and Path(path).resolve() in {
        directory / "packet.json",
        directory / "sources.json",
    }


def handle(event, host, registry_dir=None):
    """Process one native lifecycle event, scoped to explicitly bound research sessions."""
    event = dict(event)
    name = event.get("tool_name", "")
    for prefix in ("functions.collaboration.", "collaboration.", "functions."):
        if name.startswith(prefix):
            event["tool_name"] = name[len(prefix) :]
            break
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
            run, total = bootstrap
            run.mkdir(parents=True, exist_ok=True)
            if sid in state["sessions"] and state["sessions"][sid]["run_dir"] != str(run):
                return deny("One medical report per native session; start a fresh author session")
            if sid in state["sessions"] and state["sessions"][sid]["total"] != total:
                return deny("A resumed report retains its original total budget")
            state["sessions"].setdefault(
                sid,
                {"role": "author", "run_dir": str(run), "calls": [], "total": total, "host": host},
            )
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
            store.write_json(REGISTRY, state)
            return {}
        entry = state["sessions"].get(sid)
        if not entry:
            return {}
        workspace = Workspace(Path(entry["run_dir"]))
        if kind == "PreToolUse":
            call_id = event.get("tool_use_id")
            if not call_id:
                return deny(
                    "The native host omitted the tool identity needed for shared accounting"
                )
            if call_id not in entry["calls"]:
                if entry["role"] == "reviewer":
                    if len(entry["calls"]) >= entry["allocated_turns"]:
                        return deny("Reviewer allocation exhausted; return the review now")
                elif not bootstrap:
                    budget = native_review.budget_status(
                        workspace.store.read_json(native_review.STATE)
                    )
                    if budget["remaining_turns"] <= 0:
                        return deny("Shared report budget exhausted; preserve the checkpoint")
                entry["calls"].append(call_id)
            store.write_json(REGISTRY, state)
            if entry["role"] == "reviewer":
                directory = Path(entry["packet_path"]).parent
                return (
                    {}
                    if _read_only(event, directory)
                    else deny("Reviewer can only read frozen sources and write its assigned result")
                )
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
                workspace.store.write_json(
                    "native-binding.json", {"token": token, "session_id": sid}
                )
                updated = dict(event["tool_input"])
                updated["command"] += " --hook-token " + token
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "allow",
                        "updatedInput": updated,
                    }
                }
            name = event.get("tool_name", "")
            args = event.get("tool_input", {})
            if name in {"Agent", "spawn_agent", "Task"}:
                requested = args.get("subagent_type", "").split(":")[-1]
                if (host == "claude-code" and requested != REVIEWER) or sid in state["pending"]:
                    return deny("Use one fresh medical-evidence-reviewer task at a time")
                if host == "codex" and not ({"fork_turns", "fork_context"} & args.keys()):
                    return deny("Set the native spawn tool's fresh-context option explicitly")
                task = native_review.prepare(workspace, author_session_id=sid, review_turns=20)
                state["pending"][sid] = {**entry, **task, "parent_id": sid}
                store.write_json(REGISTRY, state)
                updated = dict(args)
                if host == "codex":
                    updated["message"] = task["prompt"]
                    if "fork_turns" in updated:
                        updated["fork_turns"] = "none"
                    if "fork_context" in updated:
                        updated["fork_context"] = False
                    updated.pop("model", None)
                    updated.pop("reasoning_effort", None)
                else:
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
                return deny("Medical reviewers must be fresh, metered native tasks")
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": f"Medical shared budget: {budget['remaining_turns']} "
                    "local tool calls remain, including review.",
                }
            }
        if kind == "SubagentStop" and entry["role"] == "reviewer":
            if entry.get("settled"):
                return {}
            try:
                payload = json.loads(
                    (Path(entry["packet_path"]).parent / "result.json").read_text()
                )
            except (ValueError, OSError):
                payload = None
            try:
                result = native_review.finish(
                    workspace,
                    task_id=entry["task_id"],
                    reviewer_session_id=sid,
                    used_turns=len(entry["calls"]),
                    result=payload,
                    completed=payload is not None,
                    native_metadata={
                        "event": "SubagentStop",
                        "agent_type": event.get("agent_type"),
                    },
                )
            except ValidationError as exc:
                result = {"accepted": False, "reason": str(exc)}
            task_state = workspace.store.read_json(native_review.STATE)["tasks"][entry["task_id"]]
            if task_state["state"] != "running":
                state["pending"].pop(entry["parent_id"], None)
                entry["settled"] = True
                store.write_json(REGISTRY, state)
            return {"systemMessage": "Medical review recorded: " + json.dumps(result)}
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
        return {}
