"""The task tools a Hermes session calls, served over MCP stdio.

A session no longer reads an instruction file, hand-edits ``proposal.json`` and shells out to a
submit command.  It calls one typed tool whose single ``result`` argument is grammar-constrained by
the model server, and this process maps that payload into the task's pre-filled proposal and puts it
through ``TaskEngine.submit`` unchanged.  Model output still has no authority: the submission is
validated exactly as before and the ledger decides whether the task is accepted.

The server is started by a managed profile's ``mcp_servers`` entry, which is static, so it resolves
its own work: the step runner records the claim it is currently working on in
``<store>/steps/<step>/current.json`` (owner-readable, because it carries the claim token), and
this process reads it.  No task id, path or token is ever written into a config file or a prompt.
With no current item every tool call is refused, so a stray session cannot touch the store.

Protocol: JSON-RPC 2.0 over newline-delimited stdio, the subset an MCP 1.x client needs
(``initialize``, ``tools/list``, ``tools/call``, ``ping``, and empty ``resources``/``prompts``
listings).  Nothing here imports an MCP SDK, so the package keeps its dependency set.
"""

from __future__ import annotations

import asyncio
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, TextIO

from . import answers
from .answers import RESULT_SCHEMAS, apply_result
from .tasks import (
    KIND_COMMANDS,
    KIND_ROLES,
    ROLE_PROFILES,
    Actor,
    RunCatalog,
    TaskEngine,
    ValidationError,
)

PROTOCOL_VERSION = "2025-03-26"
SERVER_NAME = "hermes-medical-research"

# One submit tool per constrained kind.  ``result`` is an object on purpose: only a non-string tool
# argument gets a real grammar from llama.cpp, so the enums inside it are what actually bind.
TOOL_KINDS = {
    "submit_screening": "screening",
    "submit_coverage": "coverage",
    "submit_study_link": "studies",
    "submit_finding": "synthesis",
}
KIND_TOOLS = {kind: name for name, kind in TOOL_KINDS.items()}

# Assessment is decided one protocol outcome at a time, so it has three tools rather than one, and
# the submission happens when the last outcome is decided.
ASSESSMENT_TOOLS = {
    "record_study_appraisal": "study_appraisal",
    "record_outcome_extracted": "outcome_extracted",
    "record_outcome_missing": "outcome_missing",
}

TOOL_DESCRIPTIONS = {
    "submit_screening": "Record the eligibility decision for the record in this task.",
    "submit_coverage": "Record whether this record goes on to detailed assessment.",
    "submit_study_link": "Record this record's study kind and whether it joins a linked study.",
    "submit_finding": "Record the finding for this task's protocol outcome.",
    "record_study_appraisal": (
        "Record this study's risk-of-bias appraisal once, before the outcomes that copy it."
    ),
    "record_outcome_extracted": (
        "Record one protocol outcome this record reports, with its estimate and the quote that "
        "supports it."
    ),
    "record_outcome_missing": (
        "Record one protocol outcome this record does not report, with the locations you inspected."
    ),
}


def _definition(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "description": TOOL_DESCRIPTIONS[name],
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["result"],
            "properties": {"result": schema},
        },
    }


def tool_definitions(kinds: tuple[str, ...]) -> list[dict[str, Any]]:
    tools = [
        _definition(KIND_TOOLS[kind], RESULT_SCHEMAS[kind])
        for kind in kinds
        if kind in KIND_TOOLS
    ]
    if "assessment" in kinds:
        tools += [
            _definition(name, answers.ASSESSMENT_SCHEMAS[shape])
            for name, shape in ASSESSMENT_TOOLS.items()
        ]
    return tools


class NoCurrentItem(ValidationError):
    """Raised when no step runner has handed this role a claim to work on."""


class ToolServer:
    """Serves one role's submit tools against one store."""

    def __init__(self, store: Path, role: str, kinds: tuple[str, ...] | None = None) -> None:
        self.store = store.resolve()
        self.role = role
        served = [*answers.CALL_KINDS, "assessment"]
        self.kinds = tuple(kinds or [k for k in served if KIND_ROLES[k] == role])
        unknown = [kind for kind in self.kinds if kind not in served]
        if unknown:
            raise ValidationError(f"no constrained answer shape for {unknown[0]}")

    # -- current item -------------------------------------------------------------------------
    def current_path(self, kind: str) -> Path:
        return self.store / "steps" / KIND_COMMANDS[kind] / "current.json"

    def current(self, kind: str) -> dict[str, Any]:
        path = self.current_path(kind)
        try:
            item = json.loads(path.read_text())
        except FileNotFoundError as exc:
            raise NoCurrentItem(
                "no task is claimed for this role right now; stop and report that you had no work"
            ) from exc
        if item.get("kind") != kind:
            raise NoCurrentItem(
                f"the claimed task is a {item.get('kind')} task, so {KIND_TOOLS[kind]} does not "
                f"apply to it"
            )
        return item

    # -- tool calls ---------------------------------------------------------------------------
    def call(self, name: str, arguments: dict[str, Any]) -> str:
        if "result" not in (arguments or {}):
            raise ValidationError("call this tool with a single `result` object")
        if name in ASSESSMENT_TOOLS:
            if "assessment" not in self.kinds:
                raise ValidationError(f"unknown tool: {name}")
            return self._record_assessment(name, arguments["result"])
        kind = TOOL_KINDS.get(name)
        if kind is None or kind not in self.kinds:
            raise ValidationError(f"unknown tool: {name}")
        item = self.current(kind)
        engine = TaskEngine(RunCatalog(self.store).workspace(item["run_id"]))
        proposal_path = Path(item["proposal_path"])
        packet_path = item.get("packet_path")
        packet = json.loads(Path(packet_path).read_text()) if packet_path else {}
        updated = apply_result(kind, json.loads(proposal_path.read_text()),
                               arguments["result"], packet)
        proposal_path.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n")
        actor = Actor(
            profile=item.get("actor_profile") or ROLE_PROFILES[self.role],
            session_id=item["session_id"],
            role=self.role,
        )
        outcome = _blocking(
            engine.submit(
                KIND_COMMANDS[kind],
                item["task_id"],
                proposal_path,
                actor,
                claim_token=item["claim_token"],
            )
        )
        if outcome.get("accepted") is False:
            errors = outcome.get("errors") or []
            raise ValidationError(
                "the submission was rejected: "
                + "; ".join(str(error) for error in errors[:6])
                + ". Correct those fields and call the tool again."
            )
        return json.dumps({"accepted": True, "recorded": kind}, sort_keys=True)

    def _record_assessment(self, name: str, result: Any) -> str:
        """Stage one outcome of an assessment, and submit when the last one is decided."""
        from . import assessment

        item = self.current("assessment")
        proposal_path = Path(item["proposal_path"])
        packet = json.loads(Path(item["packet_path"]).read_text())
        staged, remaining = assessment.record(
            ASSESSMENT_TOOLS[name], result, json.loads(proposal_path.read_text()), packet
        )
        proposal_path.write_text(json.dumps(staged, ensure_ascii=False, indent=2) + "\n")
        if remaining:
            return json.dumps(
                {"recorded": ASSESSMENT_TOOLS[name], "outcomes_remaining": remaining},
                sort_keys=True,
            )
        engine = TaskEngine(RunCatalog(self.store).workspace(item["run_id"]))
        actor = Actor(
            profile=item.get("actor_profile") or ROLE_PROFILES[self.role],
            session_id=item["session_id"],
            role=self.role,
        )
        outcome = _blocking(
            engine.submit(
                KIND_COMMANDS["assessment"],
                item["task_id"],
                proposal_path,
                actor,
                claim_token=item["claim_token"],
            )
        )
        if outcome.get("accepted") is False:
            errors = outcome.get("errors") or []
            raise ValidationError(
                "every outcome is decided but the submission was rejected: "
                + "; ".join(str(error) for error in errors[:6])
                + ". Record the affected outcome again with the correction."
            )
        return json.dumps({"accepted": True, "recorded": "assessment"}, sort_keys=True)

    # -- JSON-RPC -----------------------------------------------------------------------------
    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            requested = (message.get("params") or {}).get("protocolVersion")
            return _result(request_id, {
                "protocolVersion": requested if isinstance(requested, str) else PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": _version()},
            })
        if method in {"notifications/initialized", "notifications/cancelled"}:
            return None
        if request_id is None:
            return None
        if method == "ping":
            return _result(request_id, {})
        if method in {"tools/list", "server/discover"}:
            return _result(request_id, {"tools": tool_definitions(self.kinds)})
        if method == "resources/list":
            return _result(request_id, {"resources": []})
        if method == "prompts/list":
            return _result(request_id, {"prompts": []})
        if method == "tools/call":
            params = message.get("params") or {}
            try:
                text = self.call(params.get("name", ""), params.get("arguments") or {})
            except ValidationError as exc:
                # A rejection is the one thing the model can act on, so it comes back as tool
                # output rather than a transport error.
                return _result(request_id, {
                    "content": [{"type": "text", "text": str(exc)}],
                    "isError": True,
                })
            return _result(request_id, {"content": [{"type": "text", "text": text}]})
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }


def _blocking(coro: Any) -> Any:
    """Await a coroutine from synchronous tool-call code.

    The stdio server is synchronous, so ``asyncio.run`` is the normal path.  A caller that already
    has a loop running, such as a test or an in-process harness, gets a worker thread instead of a
    RuntimeError.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _result(request_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _version() -> str:
    from . import __version__

    return __version__


def serve(
    store: Path,
    role: str,
    *,
    kinds: tuple[str, ...] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    """Read newline-delimited JSON-RPC from ``stdin`` until it closes."""
    server = ToolServer(store, role, kinds)
    source = stdin if stdin is not None else sys.stdin
    sink = stdout if stdout is not None else sys.stdout
    for line in source:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        response = server.handle(message)
        if response is not None:
            sink.write(json.dumps(response, ensure_ascii=False) + "\n")
            sink.flush()
    return 0
