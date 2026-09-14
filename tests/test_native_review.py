"""Budget, independent-task provenance and host lifecycle regressions (no model API)."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from test_evidence_workflow import modern_workspace

from hermes_medical_search.models import ValidationError
from medical_deep_research_plugin import native_hooks, native_review
from medical_deep_research_plugin.hermes_host import _BINDINGS, _sync, pre_tool_call
from medical_deep_research_plugin.workflow import check


def pending(w, **kwargs):
    return native_review.prepare(w, author_session_id="fixture-author", review_turns=5, **kwargs)


def response(w):
    rows = deepcopy(w.rows("reviews", fresh=False))
    for r in rows:
        r.pop("native_task_id", None)
    return {"records": rows}


def finish(w, task, **kwargs):
    return native_review.finish(
        w,
        task_id=task["task_id"],
        reviewer_session_id="fresh-reviewer",
        used_turns=2,
        result=response(w),
        completed=True,
        **kwargs,
    )


def test_author_cannot_submit_own_review(tmp_path):
    w = modern_workspace(tmp_path)
    review = w.read("reviews")
    review["records"][0].pop("native_task_id")
    with pytest.raises(ValidationError, match="separate native host"):
        w.put("reviews", review)
    task = pending(w)
    with pytest.raises(ValidationError, match="distinct"):
        native_review.finish(
            w,
            task_id=task["task_id"],
            reviewer_session_id="fixture-author",
            used_turns=2,
            result=response(w),
            completed=True,
        )


def test_whole_corpus_frozen_and_no_previous_reviews(tmp_path):
    w = modern_workspace(tmp_path)
    task = pending(w)
    packet = json.loads(Path(task["packet_path"]).read_text())
    assert "reviews" not in packet["stages"]
    assert len(packet["stages"]["records"]["records"]) == len(w.rows("records"))
    sources = json.loads(Path(packet["sources_path"]).read_text())
    assert sources == w.read("documents")
    assert "native_task_id" not in json.dumps(packet)
    coverage = w.read("coverage")
    coverage["records"][0]["reason"] += " Changed selection context."
    w.put("coverage", coverage)
    with pytest.raises(ValidationError, match="candidate changed"):
        finish(w, task)
    assert not check(w)["ready"]


def test_reservations_prevent_hidden_child_budget_and_survive_restart(tmp_path):
    w = modern_workspace(tmp_path)
    task = pending(w)
    state = w.store.read_json(native_review.STATE)
    assert native_review.budget_status(state)["reviewer_turns_or_reserved"] == 7
    with pytest.raises(ValidationError, match="already running"):
        pending(w)
    native_review.bind(
        w,
        host="hermes",
        author_session_id="resumed-author",
        total_turns=150,
        used_turns=126,
        unit="fixture turns",
    )
    with pytest.raises(ValidationError, match="original host and total"):
        native_review.bind(
            w,
            host="hermes",
            author_session_id="resumed-author",
            total_turns=200,
            used_turns=126,
            unit="fixture turns",
        )
    assert finish(w, task)["accepted"]
    # 10 + 126 author, 2 + 2 child; only 10 remain (review5 + finalization5).
    assert (
        native_review.budget_status(w.store.read_json(native_review.STATE))["remaining_turns"] == 10
    )
    with pytest.raises(ValidationError, match="insufficient"):
        native_review.prepare(w, author_session_id="resumed-author", review_turns=6)


def test_invalid_result_charges_usage_and_never_promotes(tmp_path):
    w = modern_workspace(tmp_path)
    task = pending(w)
    rows = response(w)
    rows["records"][0]["observations"][0]["verdict"] = "unsupported"
    with pytest.raises(ValidationError, match="unsupported assertions"):
        native_review.finish(
            w,
            task_id=task["task_id"],
            reviewer_session_id="fresh-reviewer",
            used_turns=3,
            result=rows,
            completed=True,
        )
    state = w.store.read_json(native_review.STATE)
    assert state["tasks"][task["task_id"]]["state"] == "failed"
    assert native_review.budget_status(state)["reviewer_turns_or_reserved"] == 5


def test_budget_overrun_is_persisted_and_blocks_export(tmp_path):
    w = modern_workspace(tmp_path)
    state = native_review.bind(
        w,
        host="hermes",
        author_session_id="fixture-author",
        total_turns=150,
        used_turns=150,
        unit="fixture turns",
    )
    assert not state["within_budget"]
    assert not check(w)["ready"]


class Counter:
    def __init__(self, used, maximum=150):
        self.used, self.max_total = used, maximum

    def refund(self):
        self.used -= 1


class Parent:
    session_id = "host-author"

    def __init__(self):
        self.iteration_budget = Counter(10)


def test_hermes_subtracts_review_usage_and_retains_cap_on_next_user_turn(tmp_path):
    w = modern_workspace(tmp_path)
    parent = Parent()
    binding = {
        "workspace": w,
        "total": 150,
        "used": 0,
        "offset": 0,
        "counter": parent.iteration_budget,
    }
    # Separate host fixture unit; do not mutate the completed fixture's budget.
    (w.path / native_review.STATE).unlink()
    first = _sync(parent, binding)
    assert first["remaining_turns"] == 140
    parent.iteration_budget.refund()
    assert parent.iteration_budget.used == 10
    task = native_review.prepare(w, author_session_id=parent.session_id, review_turns=20)
    # Simulate a stopped child using 12 native iterations.
    native_review.finish(
        w,
        task_id=task["task_id"],
        reviewer_session_id="native-child",
        used_turns=12,
        result=None,
        completed=False,
    )
    _sync(parent, binding)
    assert parent.iteration_budget.max_total == 138
    parent.iteration_budget = Counter(1)  # Hermes resets per user message.
    assert _sync(parent, binding)["remaining_turns"] == 127
    assert parent.iteration_budget.max_total == 128


def event(cwd, kind="PreToolUse", **kwargs):
    return {
        "cwd": str(cwd),
        "session_id": "author",
        "hook_event_name": kind,
        "tool_name": "Bash",
        "tool_use_id": "call-1",
        "tool_input": {},
        **kwargs,
    }


@pytest.mark.parametrize("host", ["codex", "claude-code"])
def test_native_hooks_bind_once_and_count_child_calls_in_total(tmp_path, host):
    w = modern_workspace(tmp_path / "fixture")
    # New native host/units; old reviews should become invalid until this host reviews.
    (w.path / native_review.STATE).unlink()
    start = event(
        tmp_path,
        tool_input={"command": f"medical-deep-research-plugin research host-session {w.path}"},
    )
    result = native_hooks.handle(start, host)
    assert "--hook-token" in result["hookSpecificOutput"]["updatedInput"]["command"]
    native_hooks.handle(start, host)
    assert w.store.read_json(native_review.STATE)["authors"] == {"author": 1}
    args = (
        {"fork_context": False, "message": "review"}
        if host == "codex"
        else {"subagent_type": "medical-evidence-reviewer", "prompt": "review"}
    )
    spawned = native_hooks.handle(
        event(
            tmp_path,
            tool_name="collaborationspawn_agent" if host == "codex" else "Agent",
            tool_use_id="spawn-1",
            tool_input=args,
        ),
        host,
    )
    if host == "codex":
        assert "updatedInput" not in spawned["hookSpecificOutput"]
    else:
        assert "model" not in spawned["hookSpecificOutput"]["updatedInput"]
    started = native_hooks.handle(
        event(tmp_path, "SubagentStart", agent_id="child", agent_type="reviewer"), host
    )
    assert "frozen packet" in started["hookSpecificOutput"]["additionalContext"]
    task = list(w.store.read_json(native_review.STATE)["tasks"].values())[0]
    packet_path = task["packet_path"]
    read = event(
        tmp_path,
        agent_id="child",
        tool_name="Read",
        tool_use_id="child-read",
        tool_input={"file_path": packet_path},
    )
    assert native_hooks.handle(read, host) == {}
    (Path(packet_path).parent / "result.json").write_text(json.dumps(response(w)))
    stopped = event(
        tmp_path,
        "SubagentStop",
        agent_id="child",
        agent_type="reviewer",
        last_assistant_message=json.dumps(response(w)),
    )
    assert "accepted" in native_hooks.handle(stopped, host)["systemMessage"]
    assert native_hooks.handle(stopped, host) == {}  # duplicate host delivery is idempotent
    assert check(w)["ready"]
    budget = native_review.budget_status(w.store.read_json(native_review.STATE))
    assert budget["author_turns"] == 2 and budget["reviewer_turns_or_reserved"] == 1


def test_hook_scope_and_read_only_policy(tmp_path):
    assert native_hooks.handle(event(tmp_path), "codex") == {}
    directory = tmp_path / "frozen"
    assert not native_hooks._read_only(
        event(tmp_path, tool_input={"command": f"cat {directory}/packet.json; echo bad"}), directory
    )
    assert not native_hooks._read_only(
        event(tmp_path, tool_input={"command": f"cat {tmp_path / 'outside.txt'}"}), directory
    )
    assert native_hooks._read_only(
        event(tmp_path, tool_input={"command": f"sed -n 1,20p {directory}/sources.json"}), directory
    )
    _BINDINGS.clear()
    assert pre_tool_call(tool_name="delegate_task", session_id="unrelated") is None


def test_running_or_failed_latest_review_cannot_reuse_an_older_pass(tmp_path):
    w = modern_workspace(tmp_path)
    task = pending(w)
    assert not check(w)["ready"]
    native_review.finish(
        w,
        task_id=task["task_id"],
        reviewer_session_id="failed-child",
        used_turns=2,
        result=None,
        completed=False,
    )
    assert not check(w)["ready"]


def test_native_registry_follows_session_after_cwd_changes(tmp_path):
    registry = tmp_path / "host-data"
    w = modern_workspace(tmp_path / "fixture")
    (w.path / native_review.STATE).unlink()
    native_hooks.handle(
        event(
            tmp_path,
            tool_input={
                "command": f"medical-deep-research-plugin research host-session {w.path} "
                "--total-turns 3"
            },
        ),
        "claude-code",
        registry,
    )
    for count in (2, 3):
        native_hooks.handle(event(w.path, tool_use_id=f"call-{count}"), "claude-code", registry)
    blocked = native_hooks.handle(event(w.path, tool_use_id="call-4"), "claude-code", registry)
    assert blocked["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert native_review.budget_status(w.store.read_json(native_review.STATE))["author_turns"] == 3


def test_hermes_review_tool_exposes_bounded_polling():
    from medical_deep_research_plugin.hermes_host import register

    captured = {}

    class Context:
        def register_hook(self, *args):
            pass

        def register_tool(self, **kwargs):
            captured[kwargs["name"]] = kwargs["schema"]

    register(Context())
    properties = captured["medical_research_review"]["parameters"]["properties"]
    assert properties["action"]["enum"] == ["start", "status", "abandon"]
    assert properties["wait_seconds"]["maximum"] == 300  # under Hermes's 420 s tool deadline
    assert "medical_research_review_check" in captured


def test_review_output_limit_inherits_existing_host_setting():
    from medical_deep_research_plugin.hermes_host import review_output_limit

    parent = Parent()
    parent.max_tokens = None
    assert review_output_limit(parent, {"max_tokens": 32768}) == 32768
    parent.max_tokens = 16384
    assert review_output_limit(parent, {"max_tokens": 32768}) == 16384
    parent.max_tokens = None
    assert review_output_limit(parent, {}) is None


# --- Reviewer contract, repair turns, abandonment and author visibility -------------------


def bad_citation(w, rows):
    doc = w.rows("documents")[0]
    rows["records"][0]["observations"][0]["sources"] = [
        {
            "document_id": doc["record_id"],  # a bare record id, as the Codex r5 reviewer wrote
            "locator": "stages.records.records[0].title",
            "quote": "Synthetic",
        }
    ]
    return doc


def test_check_result_reports_bare_record_id_with_hint(tmp_path):
    w = modern_workspace(tmp_path)
    task = pending(w)
    rows = response(w)
    doc = bad_citation(w, rows)
    problems = native_review.check_result(w, task, rows)
    assert [p["code"] for p in problems] == ["unknown_document"]
    assert problems[0]["finding_id"] == "f1" and problems[0]["observation_index"] == 0
    assert doc["document_id"] in problems[0]["hint"]
    with pytest.raises(native_review.ReviewContentError, match="unknown native review source"):
        native_review.finish(
            w,
            task_id=task["task_id"],
            reviewer_session_id="fresh-reviewer",
            used_turns=3,
            result=rows,
            completed=True,
        )
    stored = w.store.read_json(native_review.STATE)["tasks"][task["task_id"]]
    assert stored["state"] == "failed"
    assert "unknown native review source document" in stored["failure_reason"]
    assert stored["failure_detail"][0]["code"] == "unknown_document"
    summary = native_review.summary(w)
    assert summary["latest_task"]["failure_reason"] == stored["failure_reason"]
    assert "FAILED" in summary["guidance"]


def test_check_result_covers_review_shape_before_settlement(tmp_path):
    w = modern_workspace(tmp_path)
    task = pending(w)
    rows = response(w)
    row = rows["records"][0]
    row["status"] = "PASS"
    row["review_digest"] = "0" * 64
    codes = {p["code"] for p in native_review.check_result(w, task, rows)}
    assert {"status", "review_digest"} <= codes
    rows = response(w)
    rows["records"][0]["checks"]["scope"]["status"] = "revise"
    messages = [p["message"] for p in native_review.check_result(w, task, rows)]
    assert "a review with required revisions cannot pass" in messages
    assert w.store.read_json(native_review.STATE)["tasks"][task["task_id"]]["state"] == "running"


def test_collect_result_per_finding_files(tmp_path):
    w = modern_workspace(tmp_path)
    task = pending(w)
    directory = Path(task["packet_path"]).parent
    assert (directory / "results").is_dir()
    assert native_review.collect_result(directory) == (None, [])
    row = response(w)["records"][0]
    (directory / "results" / "f1.json").write_text(json.dumps(row))
    payload, problems = native_review.collect_result(directory)
    assert payload == {"records": [row]} and problems == []
    accepted = native_review.finish(
        w,
        task_id=task["task_id"],
        reviewer_session_id="fresh-reviewer",
        used_turns=2,
        result=payload,
        completed=True,
        problems=problems,
    )
    assert accepted["accepted"]
    scratch = tmp_path / "scratch"
    (scratch / "results").mkdir(parents=True)
    (scratch / "results" / "other.json").write_text(json.dumps(row))
    (scratch / "results" / "broken.json").write_text("{not json")
    payload, problems = native_review.collect_result(scratch)
    assert payload == {"records": []}
    assert sorted(p["code"] for p in problems) == ["filename", "json"]
    (scratch / "result.json").write_text(json.dumps({"records": [row]}))
    assert native_review.collect_result(scratch)[1][0]["code"] == "both"


def test_prepare_packet_has_citation_contract_and_valid_example(tmp_path):
    from medical_deep_research_plugin.validation import validate_location
    from medical_deep_research_plugin.workspace import digest

    w = modern_workspace(tmp_path)
    task = pending(w)
    packet = json.loads(Path(task["packet_path"]).read_text())
    assert digest(packet) == task["packet_digest"]
    contract = packet["citation_contract"]
    assert {d["document_id"] for d in contract["documents"]} == set(w.index("documents"))
    example = packet["example_observation"]["sources"][0]
    doc = validate_location(w, example, w.index("documents")[example["document_id"]]["record_id"])
    assert doc["document_id"] == example["document_id"]
    # Hermes packets name the in-process check tool with concrete arguments.
    assert packet["check_command"][0] == "medical_research_review_check"
    assert f"task_id={task['task_id']}" in packet["check_command"]
    assert "citation_contract" in task["prompt"] and "results/" in task["prompt"]
    assert packet["run_dir"] == str(w.path)


async def test_review_check_cli_is_pure(tmp_path, capsys):
    from argparse import Namespace

    from medical_deep_research_plugin.commands import dispatch

    w = modern_workspace(tmp_path)
    task = pending(w)
    directory = Path(task["packet_path"]).parent
    assert not native_review.review_check(directory)["valid"]  # nothing written yet
    rows = response(w)
    bad_citation(w, rows)
    (directory / "result.json").write_text(json.dumps(rows))
    code = await dispatch(Namespace(research_command="review-check", task_dir=directory))
    report = json.loads(capsys.readouterr().out)
    assert code == 2 and not report["valid"]
    assert report["problems"][0]["code"] == "unknown_document"
    stored = w.store.read_json(native_review.STATE)["tasks"][task["task_id"]]
    assert stored["state"] == "running" and "reviewer_session_id" not in stored
    (directory / "result.json").write_text(json.dumps(response(w)))
    assert await dispatch(Namespace(research_command="review-check", task_dir=directory)) == 0
    assert json.loads(capsys.readouterr().out)["valid"]


def bound_reviewer(tmp_path, host, registry=None):
    """Author bound through the hook with one registered reviewer child."""
    w = modern_workspace(tmp_path / "fixture")
    (w.path / native_review.STATE).unlink()
    start = event(
        tmp_path,
        tool_input={"command": f"medical-deep-research-plugin research host-session {w.path}"},
    )
    native_hooks.handle(start, host, registry)
    args = (
        {"fork_context": False, "message": "review"}
        if host == "codex"
        else {"subagent_type": "medical-evidence-reviewer", "prompt": "review"}
    )
    native_hooks.handle(
        event(tmp_path, tool_name="spawn_agent", tool_use_id="spawn-1", tool_input=args),
        host,
        registry,
    )
    native_hooks.handle(
        event(tmp_path, "SubagentStart", agent_id="child", agent_type="reviewer"), host, registry
    )
    task = list(w.store.read_json(native_review.STATE)["tasks"].values())[0]
    return w, task, Path(task["packet_path"]).parent


def stop_event(tmp_path, **kwargs):
    return event(tmp_path, "SubagentStop", agent_id="child", agent_type="reviewer", **kwargs)


@pytest.mark.parametrize("host", ["codex", "claude-code"])
def test_subagent_stop_requests_repair_then_settles(tmp_path, host):
    w, task, directory = bound_reviewer(tmp_path, host)
    assert task["check_command"] == [
        "medical-deep-research-plugin",
        "research",
        "review-check",
        str(directory),
    ]
    rows = response(w)
    doc = bad_citation(w, rows)
    (directory / "result.json").write_text(json.dumps(rows))
    blocked = native_hooks.handle(stop_event(tmp_path), host)
    assert blocked["decision"] == "block"
    assert doc["document_id"] in blocked["reason"] and "review-check" in blocked["reason"]
    stored = w.store.read_json(native_review.STATE)["tasks"][task["task_id"]]
    assert stored["state"] == "running" and stored["repairs"] == 1
    # The reviewer may read its own output back and run the check command.
    for command in (
        f"sed -n 1,20p {directory}/result.json",
        f"medical-deep-research-plugin research review-check {directory}",
    ):
        read = event(
            tmp_path,
            agent_id="child",
            tool_use_id=command[:12],
            tool_input={"command": command},
        )
        assert native_hooks.handle(read, host) == {}
    (directory / "result.json").write_text(json.dumps(response(w)))
    settled = native_hooks.handle(stop_event(tmp_path, stop_hook_active=True), host)
    assert '"accepted": true' in settled["systemMessage"]
    assert native_hooks.handle(stop_event(tmp_path), host) == {}
    assert check(w)["ready"]


def test_repair_not_offered_without_headroom_or_when_stale(tmp_path):
    w, task, directory = bound_reviewer(tmp_path, "codex")
    rows = response(w)
    bad_citation(w, rows)
    (directory / "result.json").write_text(json.dumps(rows))
    registry = tmp_path / native_hooks.REGISTRY
    state = json.loads(registry.read_text())
    state["sessions"]["child"]["calls"] = [f"c{i}" for i in range(19)]  # headroom 1
    registry.write_text(json.dumps(state))
    settled = native_hooks.handle(stop_event(tmp_path), "codex")
    assert '"accepted": false' in settled["systemMessage"]
    stored = w.store.read_json(native_review.STATE)["tasks"][task["task_id"]]
    assert stored["state"] == "failed" and stored["used_turns"] == 19
    assert stored["failure_detail"][0]["code"] == "unknown_document"
    # Stale frozen inputs are never repairable.
    w2, task2, directory2 = bound_reviewer(tmp_path / "second", "codex")
    (directory2 / "result.json").write_text(json.dumps(response(w2)))
    packet = json.loads((directory2 / "packet.json").read_text())
    packet["stages"]["synthesis"]["title"] = "edited"
    (directory2 / "packet.json").write_text(json.dumps(packet))
    settled = native_hooks.handle(stop_event(tmp_path / "second"), "codex")
    assert "frozen review inputs were modified" in settled["systemMessage"]
    stored = w2.store.read_json(native_review.STATE)["tasks"][task2["task_id"]]
    assert stored["state"] == "failed" and "modified" in stored["failure_reason"]


def test_repair_capped_at_two_then_fails(tmp_path):
    w, task, directory = bound_reviewer(tmp_path, "claude-code")
    rows = response(w)
    bad_citation(w, rows)
    (directory / "result.json").write_text(json.dumps(rows))
    assert native_hooks.handle(stop_event(tmp_path), "claude-code")["decision"] == "block"
    assert native_hooks.handle(stop_event(tmp_path), "claude-code")["decision"] == "block"
    third = native_hooks.handle(stop_event(tmp_path), "claude-code")
    assert "decision" not in third and '"accepted": false' in third["systemMessage"]
    stored = w.store.read_json(native_review.STATE)["tasks"][task["task_id"]]
    assert stored["state"] == "failed" and stored["repairs"] == 2
    assert stored["native_metadata"]["repairs"] == 2


async def test_review_abandon_guards_and_charges_reservation(tmp_path):
    from argparse import Namespace

    from medical_deep_research_plugin.commands import dispatch

    w, task, directory = bound_reviewer(tmp_path, "codex")
    command = f"medical-deep-research-plugin research review-abandon {w.path} {task['task_id']}"
    attempt = event(tmp_path, tool_use_id="abandon-1", tool_input={"command": command})
    denied = native_hooks.handle(attempt, "codex")
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "active" in denied["hookSpecificOutput"]["permissionDecisionReason"]
    registry = tmp_path / native_hooks.REGISTRY
    state = json.loads(registry.read_text())
    state["sessions"]["child"]["last_call_at"] = "2026-01-01T00:00:00+00:00"
    registry.write_text(json.dumps(state))
    allowed = native_hooks.handle(
        event(tmp_path, tool_use_id="abandon-2", tool_input={"command": command}), "codex"
    )
    updated = allowed["hookSpecificOutput"]["updatedInput"]["command"]
    assert "--hook-token" in updated
    token = updated.split("--hook-token ")[1]
    with pytest.raises(ValidationError, match="did not authorize"):
        await dispatch(
            Namespace(
                research_command="review-abandon",
                run_dir=w.path,
                task_id=task["task_id"],
                hook_token="wrong",
            )
        )
    await dispatch(
        Namespace(
            research_command="review-abandon",
            run_dir=w.path,
            task_id=task["task_id"],
            hook_token=token,
        )
    )
    stored = w.store.read_json(native_review.STATE)["tasks"][task["task_id"]]
    assert stored["state"] == "failed" and stored["used_turns"] == stored["allocated_turns"]
    assert stored["native_metadata"]["event"] == "abandoned" and "reviewer_session_id" not in stored
    assert not check(w)["ready"]
    # A late stop event from the abandoned child changes nothing; a fresh review can start.
    assert native_hooks.handle(stop_event(tmp_path), "codex") == {}
    assert (
        native_review.prepare(w, author_session_id="author", review_turns=20)["state"] == "running"
    )
    with pytest.raises(ValidationError, match="only a running"):
        native_review.abandon(w, task_id=task["task_id"], author_session_id="author")


def test_read_only_allows_check_command_and_result_files(tmp_path):
    directory = tmp_path / "frozen"
    (directory / "results").mkdir(parents=True)
    entry = {"cli_prefix": ["uv", "run", "mdr"], "packet_path": str(directory / "packet.json")}

    def bash(command):
        return event(tmp_path, tool_input={"command": command})

    check = f"uv run mdr research review-check {directory}"
    assert native_hooks._read_only(bash(check), directory, entry)
    assert not native_hooks._read_only(bash(check), directory)  # prefix unknown
    assert not native_hooks._read_only(
        bash(f"python research review-check {directory}"), directory, entry
    )
    assert not native_hooks._read_only(
        bash(f"uv run mdr research review-check {tmp_path}"), directory, entry
    )
    assert native_hooks._read_only(bash(f"cat {directory}/results/f1.json"), directory)
    # Relative paths resolve against the host-reported cwd (the Codex r6 reviewer did this).
    assert native_hooks._read_only(bash("cat frozen/packet.json"), directory)
    assert not native_hooks._read_only(bash("cat ../frozen/packet.json"), directory)
    relative = event(
        tmp_path, tool_name="Write", tool_input={"file_path": "frozen/results/f2.json"}
    )
    assert native_hooks._read_only(relative, directory)
    assert not native_hooks._read_only(bash(f"cat {directory}/../outside.json"), directory)
    write = event(
        tmp_path, tool_name="Write", tool_input={"file_path": f"{directory}/results/f1.json"}
    )
    assert native_hooks._read_only(write, directory)
    for bad in (
        f"{directory}/results/../x.json",
        f"{directory}/results/f1.txt",
        f"{directory}/packet.json",
    ):
        assert not native_hooks._read_only(
            event(tmp_path, tool_name="Edit", tool_input={"file_path": bad}), directory
        )
    patch = "\n".join(
        [
            "*** Begin Patch",
            f"*** Update File: {directory}/result.json",
            "@@",
            '-  "status": "PASS"',
            '+  "status": "pass"',
            "*** End Patch",
        ]
    )
    assert native_hooks._read_only(
        event(tmp_path, tool_name="apply_patch", tool_input={"command": patch}), directory
    )
    for bad in (
        patch.replace("Update File", "Move File"),
        patch.replace(f"{directory}/result.json", f"{directory}/packet.json"),
        patch.replace("@@", f"*** Move to: {directory}/other.json"),
    ):
        assert not native_hooks._read_only(
            event(tmp_path, tool_name="apply_patch", tool_input={"command": bad}), directory
        )


def test_author_sees_failure_until_acknowledged(tmp_path):
    w, task, directory = bound_reviewer(tmp_path, "codex")
    settled = native_hooks.handle(stop_event(tmp_path), "codex")  # no result file at all
    assert "wrote no result file" in settled["systemMessage"]
    waited = native_hooks.handle(
        event(tmp_path, "PostToolUse", tool_name="collaborationwait_agent", tool_use_id="wait-1"),
        "codex",
    )
    assert "FAILED" in waited["hookSpecificOutput"]["additionalContext"]
    before = native_hooks.handle(
        event(tmp_path, tool_use_id="echo-1", tool_input={"command": "echo hi"}), "codex"
    )
    assert "FAILED" in before["hookSpecificOutput"]["additionalContext"]
    native_hooks.handle(
        event(
            tmp_path,
            tool_use_id="check-1",
            tool_input={"command": f"medical-deep-research-plugin research check {w.path}"},
        ),
        "codex",
    )
    after = native_hooks.handle(
        event(tmp_path, tool_use_id="echo-2", tool_input={"command": "echo hi"}), "codex"
    )
    assert "FAILED" not in after["hookSpecificOutput"]["additionalContext"]
    assert "budget" in after["hookSpecificOutput"]["additionalContext"]


def test_author_stop_blocked_once_for_unacknowledged_failure(tmp_path):
    w, task, directory = bound_reviewer(tmp_path, "claude-code")
    native_hooks.handle(stop_event(tmp_path), "claude-code")
    blocked = native_hooks.handle(event(tmp_path, "Stop", tool_name=None), "claude-code")
    assert blocked["decision"] == "block" and "FAILED" in blocked["reason"]
    assert native_hooks.handle(event(tmp_path, "Stop", tool_name=None), "claude-code") == {}
    assert (
        native_hooks.handle(
            event(tmp_path, "Stop", tool_name=None, stop_hook_active=True), "claude-code"
        )
        == {}
    )


def test_summary_in_check_and_next(tmp_path):
    from medical_deep_research_plugin.packets import next_packet

    w = modern_workspace(tmp_path)
    task = pending(w)
    native_review.finish(
        w,
        task_id=task["task_id"],
        reviewer_session_id="fresh-reviewer",
        used_turns=2,
        result=None,
        completed=False,
    )
    report = check(w)
    assert not report["ready"]
    assert "wrote no result file" in report["native_review"]["latest_task"]["failure_reason"]
    packet = next_packet(w, stage="review")
    assert packet["task"].startswith(f"Latest native review {task['task_id']} is failed")


def test_hermes_reviewer_path_policy_messages(tmp_path):
    import weakref

    from medical_deep_research_plugin import hermes_host

    directory = tmp_path / "frozen"
    (directory / "results").mkdir(parents=True)
    hermes_host._REVIEWERS.clear()
    hermes_host._REVIEWERS["child"] = {
        "directory": str(directory),
        "task_id": "t1",
        "run_dir": str(tmp_path),
    }
    block = pre_tool_call(tool_name="search_files", args={"path": "."}, session_id="child")
    assert block["action"] == "block" and f"path={directory}" in block["message"]
    assert (
        pre_tool_call(tool_name="search_files", args={"path": str(directory)}, session_id="child")
        is None
    )
    assert (
        pre_tool_call(
            tool_name="read_file", args={"path": f"{directory}/results/f1.json"}, session_id="child"
        )
        is None
    )
    assert (
        pre_tool_call(
            tool_name="patch", args={"path": f"{directory}/results/f1.json"}, session_id="child"
        )
        is None
    )
    outside = pre_tool_call(
        tool_name="write_file", args={"path": f"{tmp_path}/x.json"}, session_id="child"
    )
    assert "results/<finding_id>.json" in outside["message"]
    assert (
        "medical_research_review_check"
        in pre_tool_call(tool_name="terminal", args={}, session_id="child")["message"]
    )
    assert (
        pre_tool_call(tool_name="medical_research_review_check", args={}, session_id="child")
        is None
    )
    # Hermes defers plugin tools behind tool_search/tool_describe; the reviewer may discover them.
    assert pre_tool_call(tool_name="tool_describe", args={"name": "x"}, session_id="child") is None
    assert (
        pre_tool_call(tool_name="medical_research_review", args={}, session_id="child")["action"]
        == "block"
    )
    hermes_host._REVIEWERS.clear()
    w = modern_workspace(tmp_path / "fixture")
    (w.path / native_review.STATE).unlink()
    parent = Parent()
    binding = {
        "workspace": w,
        "total": 150,
        "used": 0,
        "offset": 0,
        "counter": parent.iteration_budget,
        "parent": weakref.ref(parent),
        "reviewing": True,
    }
    _BINDINGS.clear()
    _BINDINGS[parent.session_id] = binding
    busy = pre_tool_call(tool_name="search_files", args={"path": "."}, session_id=parent.session_id)
    assert "review is running" in busy["message"] and "exhausted" not in busy["message"]
    assert (
        pre_tool_call(
            tool_name="medical_research_review",
            args={"action": "status"},
            session_id=parent.session_id,
        )
        is None
    )
    binding["reviewing"] = False
    parent.iteration_budget.used = 200
    assert (
        "exhausted"
        in pre_tool_call(tool_name="read_file", args={}, session_id=parent.session_id)["message"]
    )
    _BINDINGS.clear()


def test_hermes_progress_changes_between_polls(tmp_path):
    from medical_deep_research_plugin.hermes_host import _progress

    w = modern_workspace(tmp_path)
    task = pending(w)
    child = Parent()
    child._api_call_count = 3
    binding = {"task": task, "child": child}
    first = _progress(binding)
    assert (
        first["iterations_used"] == 10 and first["api_calls"] == 3 and first["result_files"] == []
    )
    (Path(task["packet_path"]).parent / "results" / "f1.json").write_text("{}")
    assert _progress(binding)["result_files"] == ["results/f1.json"]


@pytest.mark.parametrize(
    ("exit_reason", "expected"),
    [("max_iterations", "completed"), ("completed", "completed"), ("error", "failed")],
)
def test_hermes_settles_on_artifact_not_exit_shape(tmp_path, monkeypatch, exit_reason, expected):
    import sys
    import types
    import weakref

    from medical_deep_research_plugin import hermes_host

    w = modern_workspace(tmp_path)
    (w.path / native_review.STATE).unlink()
    parent = Parent()
    binding = {
        "workspace": w,
        "total": 150,
        "used": 0,
        "offset": 0,
        "counter": parent.iteration_budget,
        "parent": weakref.ref(parent),
        "reviewing": True,
    }
    _sync(parent, binding)
    task = native_review.prepare(w, author_session_id=parent.session_id, review_turns=20)
    directory = Path(task["packet_path"]).parent
    (directory / "results" / "f1.json").write_text(json.dumps(response(w)["records"][0]))
    child = Parent()
    child.session_id = "native-child"
    child.iteration_budget = Counter(20)
    fake = types.ModuleType("tools.delegate_tool")
    fake._run_single_child = lambda *a, **k: {
        "exit_reason": exit_reason,
        "truncated": exit_reason == "max_iterations",
        "model": "fixture",
        "api_calls": 7,
    }
    monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))
    monkeypatch.setitem(sys.modules, "tools.delegate_tool", fake)
    hermes_host._REVIEWERS["native-child"] = {"directory": str(directory)}
    outcome = hermes_host._complete_review(parent, child, binding, task)
    stored = w.store.read_json(native_review.STATE)["tasks"][task["task_id"]]
    assert stored["state"] == expected
    assert stored["native_metadata"]["exit_reason"] == exit_reason
    if expected == "completed":
        assert outcome["accepted"] and check(w)["ready"]
    else:
        assert not outcome["accepted"] and "no result" not in outcome["reason"]
    assert "native-child" not in hermes_host._REVIEWERS
    assert binding["reviewing"] is False


def test_hermes_interrupted_child_keeps_reservation_until_abandoned(tmp_path, monkeypatch):
    import sys
    import types
    import weakref
    from concurrent.futures import Future

    from medical_deep_research_plugin import hermes_host

    w = modern_workspace(tmp_path)
    (w.path / native_review.STATE).unlink()
    parent = Parent()
    binding = {
        "workspace": w,
        "total": 150,
        "used": 0,
        "offset": 0,
        "counter": parent.iteration_budget,
        "parent": weakref.ref(parent),
        "reviewing": True,
    }
    _sync(parent, binding)
    task = native_review.prepare(w, author_session_id=parent.session_id, review_turns=20)
    child = Parent()
    child.session_id = "native-child"
    fake = types.ModuleType("tools.delegate_tool")
    fake._run_single_child = lambda *a, **k: {"exit_reason": "interrupted"}
    monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))
    monkeypatch.setitem(sys.modules, "tools.delegate_tool", fake)
    outcome = hermes_host._complete_review(parent, child, binding, task)
    assert outcome["state"] == "interrupted"
    assert w.store.read_json(native_review.STATE)["tasks"][task["task_id"]]["state"] == "running"
    future = Future()
    future.set_result(outcome)
    binding.update(future=future, task=task)
    _BINDINGS.clear()
    _BINDINGS[parent.session_id] = binding
    abandoned = hermes_host.review(
        {"run_dir": str(w.path), "action": "abandon"}, parent_agent=parent
    )
    assert abandoned["state"] == "abandoned"
    stored = w.store.read_json(native_review.STATE)["tasks"][task["task_id"]]
    assert stored["state"] == "failed" and stored["used_turns"] == 20
    _BINDINGS.clear()
