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
        {"fork_context": True, "message": "review", "model": "unwanted"}
        if host == "codex"
        else {"subagent_type": "medical-evidence-reviewer", "prompt": "review", "model": "unwanted"}
    )
    spawned = native_hooks.handle(
        event(tmp_path, tool_name="Agent", tool_use_id="spawn-1", tool_input=args), host
    )
    updated = spawned["hookSpecificOutput"]["updatedInput"]
    assert "model" not in updated
    if host == "codex":
        assert updated["fork_context"] is False
    native_hooks.handle(
        event(tmp_path, "SubagentStart", agent_id="child", agent_type="reviewer"), host
    )
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
    assert properties["action"]["enum"] == ["start", "status"]
    assert properties["wait_seconds"]["maximum"] == 45
