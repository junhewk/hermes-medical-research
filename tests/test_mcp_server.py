"""The typed task tools a Hermes session calls, and the claim they resolve against."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from test_research import completed_search

from hermes_medical_research import answers, mcp_server, steps
from hermes_medical_research.automation import AutomationEngine
from hermes_medical_research.search.models import ValidationError
from hermes_medical_research.tasks import Actor, TaskEngine


def protocol() -> dict:
    return {
        "schema_version": "3",
        "framework": "PICO",
        "question": "Does exercise improve function in adults?",
        "components": {
            "population": {"groups": [{"label": "population", "text": "adults"}]},
            "intervention": {"groups": [{"label": "intervention", "text": "exercise"}]},
        },
        "sources": ["europe-pmc"],
        "eligibility": {"include": ["Adults"], "exclude": ["Animal-only"]},
        "outcomes": ["Function"],
        "search_rationale": "Bounded synthetic automation test.",
    }


async def claimed_screening(tmp_path: Path, count: int = 1):
    """A store whose selector queue holds one claimed screening task, as a step would leave it."""
    store = tmp_path / "store"
    automation = AutomationEngine(store)
    created = automation.create_review("tools", protocol(), schedule="once",
                                       timezone="Asia/Seoul")
    run_id = automation.review_status(created["name"])["cycles"][0]["run_id"]
    workspace = automation.catalog.workspace(run_id)
    search, _ = completed_search(workspace, tmp_path / "search", count=count)
    workspace.attach(search.path)
    await automation.tick()
    actor = Actor("hmr-selector", "step-select-test", "selector")
    claim = automation.claim("select", actor, review=created["name"])
    steps._write_json(steps.current_path(store, "select"), {
        "claim_id": claim["claim_id"],
        "claim_token": claim["claim_token"],
        "run_id": claim["run_id"],
        "task_id": claim["task_id"],
        "role": "selector",
        "kind": claim["kind"],
        "actor_profile": actor.profile,
        "session_id": actor.session_id,
        "packet_path": claim["packet_path"],
        "proposal_path": claim["proposal_path"],
        "expires_at": claim["expires_at"],
    }, private=True)
    return store, automation, workspace, claim


def test_a_constrained_profile_sees_exactly_one_tool(tmp_path):
    server = mcp_server.ToolServer(tmp_path, "selector", ("screening",))

    tools = mcp_server.tool_definitions(server.kinds)

    assert [tool["name"] for tool in tools] == ["submit_screening"]
    schema = tools[0]["inputSchema"]
    assert schema["required"] == ["result"]
    # The payload must be an object: llama.cpp gives a string argument no grammar at all.
    assert schema["properties"]["result"]["type"] == "object"
    answers.check_keywords(schema["properties"]["result"])


def test_a_role_without_a_kind_filter_gets_its_own_kinds(tmp_path):
    assert mcp_server.ToolServer(tmp_path, "selector").kinds == ("screening", "coverage")
    assert mcp_server.ToolServer(tmp_path, "synthesizer").kinds == ("synthesis",)


def test_the_handshake_and_tool_list_answer_an_mcp_client(tmp_path):
    server = mcp_server.ToolServer(tmp_path, "selector", ("screening",))

    initialized = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": "2025-03-26"}})
    assert initialized["result"]["protocolVersion"] == "2025-03-26"
    assert initialized["result"]["capabilities"]["tools"] == {"listChanged": False}
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    listed = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert [tool["name"] for tool in listed["result"]["tools"]] == ["submit_screening"]


def test_serve_reads_newline_delimited_json_until_stdin_closes(tmp_path):
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    out = io.StringIO()

    mcp_server.serve(tmp_path, "selector", kinds=("screening",),
                     stdin=io.StringIO(request + "\n"), stdout=out)

    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [tool["name"] for tool in replies[0]["result"]["tools"]] == ["submit_screening"]


@pytest.mark.asyncio
async def test_a_submitted_decision_is_accepted_through_the_normal_submit_path(tmp_path):
    store, automation, workspace, claim = await claimed_screening(tmp_path)
    server = mcp_server.ToolServer(store, "selector", ("screening",))

    reply = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
        "name": "submit_screening",
        "arguments": {"result": {"decision": "exclude", "reason": "No exercise intervention."}},
    }})

    assert reply["result"].get("isError") is not True
    assert json.loads(reply["result"]["content"][0]["text"])["accepted"] is True
    engine = TaskEngine(workspace)
    assert engine.task_automation(claim["task_id"])["state"] == "accepted"
    row = workspace.rows("screening")[0]
    assert (row["decision"], row["basis"]) == ("exclude", "title-abstract")
    assert row["record_id"] == workspace.rows("records")[0]["record_id"]


@pytest.mark.asyncio
async def test_an_off_schema_answer_is_returned_to_the_model_not_written(tmp_path):
    store, _automation, workspace, claim = await claimed_screening(tmp_path)
    server = mcp_server.ToolServer(store, "selector", ("screening",))

    reply = server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
        "name": "submit_screening",
        "arguments": {"result": {"decision": "probably", "reason": "Unsure."}},
    }})

    assert reply["result"]["isError"] is True
    assert "must be one of" in reply["result"]["content"][0]["text"]
    assert TaskEngine(workspace).task_automation(claim["task_id"])["state"] == "in_progress"
    assert "screening" not in workspace.manifest_view()["datasets"]


@pytest.mark.asyncio
async def test_a_validation_rejection_comes_back_as_the_validators_own_message(tmp_path):
    store, _automation, workspace, claim = await claimed_screening(tmp_path)
    server = mcp_server.ToolServer(store, "selector", ("screening",))

    # The shape is fine, so only the stage validator can catch it: full-text screening needs a
    # stored full text, and this record has none.
    reply = server.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {
        "name": "submit_screening",
        "arguments": {"result": {"decision": "include", "reason": "Reads as eligible.",
                                 "basis": "fulltext"}},
    }})

    assert reply["result"]["isError"] is True
    text = reply["result"]["content"][0]["text"]
    assert "full-text screening requires a stored full text" in text
    assert "call the tool again" in text
    assert TaskEngine(workspace).task_automation(claim["task_id"])["state"] == "in_progress"


def test_with_no_claimed_task_every_call_is_refused(tmp_path):
    server = mcp_server.ToolServer(tmp_path, "selector", ("screening",))

    reply = server.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {
        "name": "submit_screening",
        "arguments": {"result": {"decision": "exclude", "reason": "Out of scope."}},
    }})

    assert reply["result"]["isError"] is True
    assert "no task is claimed" in reply["result"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_a_tool_that_does_not_match_the_claimed_kind_is_refused(tmp_path):
    store, _automation, _workspace, _claim = await claimed_screening(tmp_path)
    server = mcp_server.ToolServer(store, "selector", ("screening", "coverage"))

    reply = server.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
        "name": "submit_coverage",
        "arguments": {"result": {"selection": "selected", "reason": "Why not.",
                                 "protocol_outcomes": []}},
    }})

    assert reply["result"]["isError"] is True
    assert "does not apply" in reply["result"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_the_claim_token_is_never_written_into_a_config_or_a_prompt(tmp_path):
    store, _automation, _workspace, claim = await claimed_screening(tmp_path)

    current = steps.current_path(store, "select")
    assert claim["claim_token"] in current.read_text()
    assert oct(current.stat().st_mode)[-3:] == "600"
    packet = json.loads(Path(claim["packet_path"]).read_text())
    assert claim["claim_token"] not in json.dumps(packet)
    prefix, tail = answers.build_prompt("screening", packet)
    assert claim["claim_token"] not in prefix + tail
    assert claim["task_id"] not in prefix


def test_an_unknown_method_is_a_json_rpc_error(tmp_path):
    server = mcp_server.ToolServer(tmp_path, "selector", ("screening",))

    reply = server.handle({"jsonrpc": "2.0", "id": 8, "method": "tools/whatever"})

    assert reply["error"]["code"] == -32601


def test_a_server_cannot_be_built_for_a_kind_without_a_shape(tmp_path):
    with pytest.raises(ValidationError):
        mcp_server.ToolServer(tmp_path, "extractor", ("assessment",))


@pytest.mark.asyncio
async def test_a_tool_call_works_even_when_the_caller_already_has_a_loop(tmp_path):
    # The stdio server is synchronous, but an in-process harness may already be running a loop.
    store, _automation, workspace, claim = await claimed_screening(tmp_path)
    server = mcp_server.ToolServer(store, "selector", ("screening",))

    text = server.call("submit_screening",
                       {"result": {"decision": "uncertain", "reason": "No abstract."}})

    assert json.loads(text)["accepted"] is True
    assert workspace.rows("screening")[0]["decision"] == "uncertain"
