"""User-invoked steps: the claim loop, the lanes, the failure ladder, and detaching."""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_mcp_server import claimed_screening, protocol
from test_research import completed_search

from hermes_medical_research import hermes, steps
from hermes_medical_research.automation import AutomationEngine
from hermes_medical_research.search.models import ValidationError
from hermes_medical_research.tasks import Actor, TaskEngine

DONE = subprocess.CompletedProcess([], 0, "", "")


def review_with_records(tmp_path: Path, name: str, count: int, clock=None):
    automation = AutomationEngine(tmp_path / "store", **({"clock": clock} if clock else {}))
    created = automation.create_review(name, protocol(), schedule="once", timezone="Asia/Seoul")
    run_id = automation.review_status(created["name"])["cycles"][0]["run_id"]
    workspace = automation.catalog.workspace(run_id)
    search, _ = completed_search(workspace, tmp_path / "search", count=count)
    workspace.attach(search.path)
    return automation, workspace, created["name"]


PAYLOADS = {
    "screening": {"decision": "exclude", "reason": "Fails the synthetic eligibility criteria."},
    "coverage": {"selection": "selected", "reason": "Reports the protocol outcome.",
                 "protocol_outcomes": ["Function"]},
    "studies": {"kind": "primary", "basis": "No companion report named.", "same_study_id": ""},
}


def answering(store: Path, decision: str = "exclude", *, record: list[str] | None = None):
    """A constrained-call stub: the session answers with schema JSON and nothing else."""
    def call(_executable, _home, _profile, prompt, kind):
        payload = dict(PAYLOADS[kind])
        if kind == "screening":
            payload["decision"] = decision
        if record is not None:
            record.append(prompt)
        return subprocess.CompletedProcess([], 0, json.dumps(payload), "")
    del store
    return call


def refusing(reply: str = "I cannot answer that."):
    """A session that answers in prose, which is what the schema exists to prevent."""
    def call(_executable, _home, _profile, _prompt, _kind):
        return subprocess.CompletedProcess([], 0, reply, "")
    return call


def session_excluding(workspace):
    """A session-lane stub that edits the proposal and submits, as the skill tells a bot to."""
    def invoke(_executable, _home, claim, actor, _role):
        path = Path(claim["proposal_path"])
        proposal = json.loads(path.read_text())
        proposal["stages"]["screening"]["records"][0].update(
            decision="exclude", reason="Fails the synthetic eligibility criteria."
        )
        path.write_text(json.dumps(proposal))
        import asyncio

        asyncio.run(TaskEngine(workspace).submit(
            "select", claim["task_id"], path, actor, claim_token=claim["claim_token"]
        ))
        return DONE
    return invoke


@pytest.mark.asyncio
async def test_one_constrained_call_per_record_and_one_decision_each(tmp_path):
    automation, workspace, name = review_with_records(tmp_path, "one-call", 3)
    await automation.tick()
    prompts: list[str] = []

    result = await steps.run_step(
        "select", store=tmp_path / "store", review=name, hermes_home=tmp_path / "h",
        call=answering(tmp_path / "store", record=prompts),
        invoke=lambda *a, **k: pytest.fail("the session lane must not be used"),
    )

    assert (result["state"], result["processed"], result["failed"]) == ("drained", 3, 0)
    assert result["lanes"] == {"call": 3}
    assert len(prompts) == 3
    assert len({prompt for prompt in prompts}) == 3
    decided = {row["record_id"] for row in workspace.rows("screening")}
    assert decided == {row["record_id"] for row in workspace.rows("records")}


@pytest.mark.asyncio
async def test_an_unanswered_call_retries_then_escalates_to_the_session_lane(tmp_path):
    automation, workspace, name = review_with_records(tmp_path, "escalate", 1)
    await automation.tick()
    calls: list[str] = []

    def call(executable, home, profile, prompt, kind):
        calls.append(kind)
        return refusing()(executable, home, profile, prompt, kind)

    sessions: list[str] = []

    def invoke(executable, home, claim, actor, role):
        sessions.append(role)
        return session_excluding(workspace)(executable, home, claim, actor, role)

    result = await steps.run_step(
        "select", store=tmp_path / "store", review=name, hermes_home=tmp_path / "h",
        call=call, invoke=invoke,
    )

    assert len(calls) == steps.CALL_ATTEMPTS
    assert sessions == ["selector"]  # one session with tools, after the constrained attempts
    assert (result["processed"], result["failed"]) == (1, 0)
    assert workspace.rows("screening")[0]["decision"] == "exclude"


@pytest.mark.asyncio
async def test_an_exhausted_ladder_fails_the_claim_and_records_the_reason(tmp_path):
    automation, workspace, name = review_with_records(tmp_path, "exhausted", 1)
    await automation.tick()

    result = await steps.run_step(
        "select", store=tmp_path / "store", review=name, hermes_home=tmp_path / "h",
        call=refusing("no tool call"),
        invoke=lambda *_a, **_k: subprocess.CompletedProcess([], 1, "", "session gave up"),
        max_wait=0,
    )

    assert (result["state"], result["processed"], result["failed"]) == ("drained", 0, 1)
    failure = result["failures"][0]
    assert failure["code"] == "screening_not_recorded"
    assert "session gave up" in failure["message"]
    task = TaskEngine(workspace).status()["active"][0]
    assert task["state"] == "pending"
    assert TaskEngine(workspace).task_automation(task["task_id"])["automation_attempts"] == 1


@pytest.mark.asyncio
async def test_a_single_failure_does_not_end_the_step(tmp_path):
    """The first failure backs a Task off for five minutes, which is not an empty queue."""
    clock = {"now": datetime(2026, 9, 18, 9, 0, tzinfo=UTC)}
    automation, workspace, name = review_with_records(
        tmp_path, "backoff", 2, clock=lambda: clock["now"]
    )
    await automation.tick()
    store = tmp_path / "store"
    failed_once: set[str] = set()
    slept: list[float] = []

    def current_task() -> str:
        return json.loads(steps.current_path(store, "select").read_text())["task_id"]

    def call(executable, home, profile, prompt, kind):
        if current_task() in failed_once:
            return answering(store)(executable, home, profile, prompt, kind)
        return refusing()(executable, home, profile, prompt, kind)

    def invoke(*_args, **_kwargs):
        failed_once.add(current_task())
        return subprocess.CompletedProcess([], 1, "", "session gave up")

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["now"] += timedelta(seconds=seconds)

    result = await steps.run_step(
        "select", store=store, review=name, hermes_home=tmp_path / "h",
        call=call, invoke=invoke, sleep=sleep, max_wait=40.0,
        clock=lambda: clock["now"],
    )

    assert slept, "the step must wait out the bounded backoff instead of reporting an empty queue"
    # Each record failed once and was answered on its retry, which is the point: one bad item no
    # longer ends a step that still has work.
    assert (result["processed"], result["failed"]) == (2, 2)
    assert len(workspace.rows("screening")) == 2


@pytest.mark.asyncio
async def test_search_and_fulltext_need_no_model_at_all(tmp_path):
    automation, workspace, name = review_with_records(tmp_path, "no-model", 1)
    await automation.tick()

    def forbidden(*_args, **_kwargs):
        pytest.fail("a deterministic kind must not start a session")

    result = await steps.run_step(
        "select", store=tmp_path / "store", review=name, hermes_home=tmp_path / "h",
        call=answering(tmp_path / "store", "include"), invoke=forbidden,
    )
    assert result["processed"] == 2  # the screening decision and its coverage decision

    extract = await steps.run_step(
        "extract", store=tmp_path / "store", review=name, hermes_home=tmp_path / "h",
        call=forbidden, invoke=forbidden, limit=1,
    )
    assert extract["lanes"] == {"none": 1}
    assert workspace.rows("documents")


def test_the_lane_of_each_kind_is_deterministic():
    assert steps.lane_for("screening") == "call"
    assert steps.lane_for("coverage") == "call"
    assert steps.lane_for("studies") == "call"
    assert steps.lane_for("synthesis", packet={"text_limit": 600}) == "call"
    # A shortened synthesis packet shows summaries, so the finding needs source reads.
    assert steps.lane_for("synthesis", packet={"text_limit": 240}) == "agent"
    assert steps.lane_for("assessment") == "agent"
    assert steps.lane_for("audit") == "agent"
    assert steps.lane_for("search") == "none"
    assert steps.lane_for("fulltext") == "none"
    # A correction carries the audit's objection, which only a session can act on.
    assert steps.lane_for("screening", correction=True) == "agent"
    assert steps.lane_for("screening", assignment={"kinds": {"screening": {"lane": "agent"}}}) \
        == "agent"


@pytest.mark.asyncio
async def test_a_step_claims_only_its_own_review(tmp_path):
    first_automation, first_workspace, first = review_with_records(tmp_path, "first", 1)
    await first_automation.tick()
    second = first_automation.create_review(
        "second", protocol(), schedule="once", timezone="Asia/Seoul"
    )
    run_id = first_automation.review_status(second["name"])["cycles"][0]["run_id"]
    other = first_automation.catalog.workspace(run_id)
    search, _ = completed_search(other, tmp_path / "search-2", count=1)
    other.attach(search.path)
    await first_automation.tick()

    result = await steps.run_step(
        "select", store=tmp_path / "store", review=first, hermes_home=tmp_path / "h",
        call=answering(tmp_path / "store"),
    )

    assert result["processed"] == 1
    assert len(first_workspace.rows("screening")) == 1
    assert "screening" not in other.manifest_view()["datasets"]


@pytest.mark.asyncio
async def test_the_active_review_pointer_resolves_and_explains_itself(tmp_path):
    store = tmp_path / "store"
    automation, _workspace, name = review_with_records(tmp_path, "pointed", 1)
    automation.create_review("another", protocol(), schedule="once", timezone="Asia/Seoul")
    await automation.tick()

    with pytest.raises(ValidationError) as error:
        steps.active_review(store)
    assert "hmr step use NAME" in str(error.value)

    steps.set_active_review(store, name)
    assert steps.active_review(store) == name
    assert steps.active_review(store, "another") == "another"
    steps.set_active_review(store, None)
    with pytest.raises(ValidationError):
        steps.active_review(store)


@pytest.mark.asyncio
async def test_a_paused_review_reports_instead_of_working(tmp_path):
    automation, workspace, name = review_with_records(tmp_path, "paused", 1)
    await automation.tick()
    automation.set_paused(name, True)

    result = await steps.run_step(
        "select", store=tmp_path / "store", review=name, hermes_home=tmp_path / "h",
        call=lambda *_a, **_k: pytest.fail("a paused review must not be worked"),
    )

    assert result["state"] == "drained"
    assert result["processed"] == 0
    assert "screening" not in workspace.manifest_view()["datasets"]


@pytest.mark.asyncio
async def test_a_stop_request_ends_the_step_between_items(tmp_path):
    automation, workspace, name = review_with_records(tmp_path, "stopping", 3)
    await automation.tick()
    store = tmp_path / "store"

    def call(executable, home, profile, prompt, kind):
        completed = answering(store)(executable, home, profile, prompt, kind)
        steps.request_stop(store, review=name)
        return completed

    result = await steps.run_step(
        "select", store=store, review=name, hermes_home=tmp_path / "h", call=call
    )

    assert result["state"] == "stopped"
    assert result["processed"] == 1
    assert len(workspace.rows("screening")) == 1


@pytest.mark.asyncio
async def test_a_limit_stops_the_step_and_leaves_the_rest_claimable(tmp_path):
    automation, workspace, name = review_with_records(tmp_path, "limited", 3)
    await automation.tick()

    result = await steps.run_step(
        "select", store=tmp_path / "store", review=name, hermes_home=tmp_path / "h",
        call=answering(tmp_path / "store"), limit=2,
    )

    assert (result["state"], result["processed"]) == ("limit_reached", 2)
    assert len(workspace.rows("screening")) == 2


@pytest.mark.asyncio
async def test_an_interrupted_claim_is_released_without_spending_an_attempt(tmp_path):
    store, automation, workspace, claim = await claimed_screening(tmp_path)
    task = TaskEngine(workspace).task_automation(claim["task_id"])
    assert task["automation_attempts"] == 1 and task.get("lease")

    result = await steps.run_step(
        "select", store=store, review="tools", hermes_home=tmp_path / "h",
        call=answering(store),
    )

    assert result["processed"] == 1
    detail = TaskEngine(workspace).task_automation(claim["task_id"])
    assert detail["state"] == "accepted"
    released = [event for event in workspace.manifest_view()["task_engine"]["events"]
                if event["event"] == "task.released"]
    assert released and released[0]["reason"] == "step_interrupted"


@pytest.mark.asyncio
async def test_a_second_runner_is_refused_while_the_step_lock_is_held(tmp_path):
    from filelock import FileLock

    automation, _workspace, name = review_with_records(tmp_path, "locked", 1)
    await automation.tick()
    store = tmp_path / "store"
    steps.steps_root(store).mkdir(parents=True, exist_ok=True)
    held = FileLock(str(steps.lock_path(store, "select")), timeout=0)
    held.acquire()
    try:
        result = await steps.run_step(
            "select", store=store, review=name, hermes_home=tmp_path / "h",
            call=lambda *_a, **_k: pytest.fail("a second runner must not work the queue"),
        )
    finally:
        held.release()

    assert result["state"] == "refused"
    assert "select.lock" in result["message"]


@pytest.mark.asyncio
async def test_status_reports_progress_failures_and_the_next_command(tmp_path):
    automation, _workspace, name = review_with_records(tmp_path, "reported", 2)
    await automation.tick()
    store = tmp_path / "store"
    steps.set_active_review(store, name)

    await steps.run_step("select", store=store, review=name, hermes_home=tmp_path / "h",
                         call=answering(store), limit=1)

    view = steps.status_view(store)
    assert view["review"] == name
    assert view["steps"]["select"]["processed"] == 1
    assert view["steps"]["select"]["progress"]["screened"] == 1
    text = steps.render_status(view)
    assert "1/2 screened" in text
    assert "/hmr-selector" in text

    automation.set_paused(name, True)
    paused = steps.render_status(steps.status_view(store))
    assert "paused" in paused.splitlines()[0]
    assert "hmr review resume" in paused


def test_start_detaches_the_worker_and_never_inherits_the_quick_commands_pipes(tmp_path):
    """A quick command is killed at 30 s; an inherited pipe would hang the Hermes CLI."""
    store = tmp_path / "store"
    automation = AutomationEngine(store)
    automation.create_review("detached", protocol(), schedule="once", timezone="Asia/Seoul")
    recorded: dict = {}

    def spawn(argv, *, stdout, stderr):
        recorded.update(argv=argv, stdout=stdout, stderr=stderr)

        class Fake:
            pid = 4242

        return Fake()

    result = steps.start("select", store=store, review="detached", spawn=spawn)

    assert result["state"] == "starting" and result["job_id"].startswith("job-")
    assert recorded["argv"][1:6] == ["--store", str(store), "step", "run", "select"]
    assert "--job" in recorded["argv"] and "--review" in recorded["argv"]
    assert recorded["stdout"] is recorded["stderr"]
    assert recorded["stdout"].name.endswith(".log")
    status = json.loads(steps.status_path(store, "detached", "select").read_text())
    assert status["state"] == "starting"


def test_starting_a_step_twice_reports_the_running_job_instead_of_racing(tmp_path):
    store = tmp_path / "store"
    automation = AutomationEngine(store)
    automation.create_review("twice", protocol(), schedule="once", timezone="Asia/Seoul")
    steps._write_json(steps.status_path(store, "twice", "select"), {
        "step": "select", "review": "twice", "state": "running", "pid": os.getpid(),
        "job_id": "job-abc", "processed": 4,
    })

    result = steps.start("select", store=store, review="twice",
                         spawn=lambda *a, **k: pytest.fail("must not spawn a second runner"))

    assert result["already_running"] is True
    assert result["job_id"] == "job-abc"


def test_an_unknown_step_is_refused_by_name(tmp_path):
    with pytest.raises(ValidationError) as error:
        steps.start("screen", store=tmp_path)
    assert "unknown step" in str(error.value)


def test_the_quick_commands_carry_no_arguments_and_one_store(tmp_path):
    specs = steps.quick_command_specs(tmp_path / "store")

    assert set(specs) == {
        "hmr-search", "hmr-selector", "hmr-extractor", "hmr-synthesizer", "hmr-auditor",
        "hmr-status", "hmr-next", "hmr-stop", "hmr-retry", "hmr-finalize",
    }
    for entry in specs.values():
        assert entry["type"] == "exec"
        assert str(tmp_path / "store") in entry["command"]


@pytest.mark.asyncio
async def test_run_with_renewal_extends_the_lease_while_a_session_works(tmp_path, monkeypatch):
    store, automation, _workspace, claim = await claimed_screening(tmp_path)
    monkeypatch.setattr(hermes, "LEASE_RENEW_SECONDS", 0.01)
    renewed: list[str] = []
    monkeypatch.setattr(
        automation, "renew", lambda claim_id, actor: renewed.append(claim_id)
    )
    actor = Actor("hmr-selector", "renewal", "selector")

    def slow(_claim, _actor):
        import time

        time.sleep(0.15)
        return DONE

    await hermes.run_with_renewal(automation, claim, actor, runner=slow)

    assert renewed and renewed[0] == claim["claim_id"]
    del store


@pytest.mark.asyncio
async def test_waiting_never_blocks_a_cycle(tmp_path):
    """Abandonment blocking is gone: an unclaimed Task between two steps is normal."""
    clock = {"now": datetime(2026, 9, 18, 9, 0, tzinfo=UTC)}
    automation, workspace, name = review_with_records(
        tmp_path, "unattended", 1, clock=lambda: clock["now"]
    )
    await automation.tick()

    clock["now"] += timedelta(hours=6)
    await automation.tick()

    assert automation.review_status(name)["cycles"][0]["status"] == "active"
    assert TaskEngine(workspace).status()["active"][0]["state"] == "pending"


@pytest.mark.asyncio
async def test_a_session_is_told_its_submit_tool_only_when_one_exists(tmp_path, monkeypatch):
    """The skills prefer the typed tool, so the instruction file must not promise a missing one."""
    store, _automation, _workspace, claim = await claimed_screening(tmp_path)
    written: list[dict] = []

    def record(argv, **kwargs):
        # The prompt names the instruction file; the session never gets it as an argument.
        match = re.search(r"(/\S+\.json)", argv[-1])
        assert match, argv[-1]
        written.append(json.loads(Path(match.group(1)).read_text()))
        return DONE

    monkeypatch.setattr(subprocess, "run", record)
    actor = Actor("hmr-selector", "instruction", "selector")

    hermes.invoke_task_session("hermes", tmp_path / "h", claim, actor, "selector")
    assert written[0]["submit_tool"] == "submit_screening"

    hermes.invoke_task_session(
        "hermes", tmp_path / "h", {**claim, "kind": "assessment"}, actor, "extractor"
    )
    assert "submit_tool" not in written[1]
    assert written[1]["submit_tools"] == [
        "record_study_appraisal", "record_outcome_extracted", "record_outcome_missing"
    ]
    del store
