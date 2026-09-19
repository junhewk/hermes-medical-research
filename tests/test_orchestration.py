"""Step orchestration: continuation, pause, retry, host failures, and audit reuse."""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_automation import protocol
from test_evidence_workflow import modern_workspace, record_reviews
from test_research import completed_search

from hermes_medical_research import audit, steps
from hermes_medical_research.automation import AutomationEngine
from hermes_medical_research.hermes import (
    routine_status,
    routines,
    set_routines_paused,
    write_assignment,
)
from hermes_medical_research.search.models import ValidationError
from hermes_medical_research.tasks import MAX_CORRECTIONS_PER_GROUP, Actor, TaskEngine
from hermes_medical_research.workflow import check

COORDINATOR = Actor("hmr-coordinator", "operator", "coordinator")


def review_with_records(tmp_path: Path, name: str, count: int, clock=None):
    automation = AutomationEngine(tmp_path / "store", **({"clock": clock} if clock else {}))
    created = automation.create_review(name, protocol(), schedule="once", timezone="Asia/Seoul")
    run_id = automation.review_status(created["name"])["cycles"][0]["run_id"]
    workspace = automation.catalog.workspace(run_id)
    search, _ = completed_search(workspace, tmp_path / "search", count=count)
    workspace.attach(search.path)
    return automation, workspace


def exclude(claim: dict) -> Path:
    path = Path(claim["proposal_path"])
    proposal = json.loads(path.read_text())
    proposal["stages"]["screening"]["records"][0].update(
        decision="exclude", reason="Fails the synthetic eligibility criteria."
    )
    path.write_text(json.dumps(proposal))
    return path


@pytest.mark.asyncio
async def test_acceptance_continues_into_the_next_role_without_a_tick(tmp_path):
    automation, workspace = review_with_records(tmp_path, "handoff", 1)
    await automation.tick()
    actor = Actor("hmr-selector", "handoff-session", "selector")
    claim = automation.claim("select", actor)

    receipt = await TaskEngine(workspace).submit(
        "select", claim["task_id"], exclude(claim), actor, claim_token=claim["claim_token"]
    )

    assert receipt["continuation"]["role"] == "synthesizer"
    synthesizer = Actor("hmr-synthesizer", "next-session", "synthesizer")
    assert automation.claim("synthesize", synthesizer)["kind"] == "synthesis"
    stored = json.loads((automation.claims / f"{claim['claim_id']}.json").read_text())
    assert stored["state"] == "completed"


@pytest.mark.asyncio
async def test_paused_review_is_unclaimable_and_a_long_wait_never_blocks_the_cycle(tmp_path):
    current = [datetime.now(UTC) + timedelta(seconds=2)]
    automation, workspace = review_with_records(
        tmp_path, "paused", 1, clock=lambda: current[0]
    )
    await automation.tick()
    automation.set_paused("paused", True)
    actor = Actor("hmr-selector", "paused-session", "selector")
    assert automation.claim("select", actor)["state"] == "idle"
    assert automation.next_available_at("selector", "paused") is None

    # Nothing claims work on a timer any more, so a Task nobody has started is not abandoned
    # however long it waits: only a lease that expires or three failures can block the Cycle.
    current[0] += timedelta(hours=3)
    await automation.tick()
    assert automation.review_status("paused")["latest_cycle_state"] == "active"

    automation.set_paused("paused", False)
    current[0] += timedelta(minutes=30)
    await automation.tick()
    assert automation.review_status("paused")["latest_cycle_state"] == "active"
    assert automation.claim("select", actor)["kind"] == "screening"


@pytest.mark.asyncio
async def test_blocked_cycle_retries_in_place_and_keeps_partial_proposal(tmp_path):
    current = [datetime.now(UTC) + timedelta(seconds=2)]
    automation, workspace = review_with_records(
        tmp_path, "retry-in-place", 2, clock=lambda: current[0]
    )
    await automation.tick()
    actor = Actor("hmr-selector", "first-session", "selector")
    first = automation.claim("select", actor)
    await TaskEngine(workspace).submit(
        "select", first["task_id"], exclude(first), actor, claim_token=first["claim_token"]
    )
    for delay in (5, 30, 0):
        claim = automation.claim("select", actor)
        path = Path(claim["proposal_path"])
        partial = json.loads(path.read_text())
        partial["stages"]["screening"]["records"][0]["reason"] = "Partly reviewed."
        path.write_text(json.dumps(partial))
        automation.fail(claim["claim_id"], actor, code="fixture", message="host died")
        current[0] += timedelta(minutes=delay)
    assert automation.review_status("retry-in-place")["latest_cycle_state"] == "blocked"

    with pytest.raises(ValidationError, match="coordinator role required"):
        automation.retry_review("retry-in-place", actor, reason="Operator restarted workers.")
    retried = automation.retry_review(
        "retry-in-place", COORDINATOR, reason="Operator restarted workers."
    )

    assert retried["cycle_state"] == "active"
    assert retried["retried_task_ids"] == [claim["task_id"]]
    reopened = automation.claim("select", Actor("hmr-selector", "second-session", "selector"))
    assert reopened["task_id"] == claim["task_id"]
    assert json.loads(Path(reopened["proposal_path"]).read_text()) == partial
    assert len(workspace.rows("screening")) == 1
    with pytest.raises(ValidationError, match="already has an active Cycle"):
        automation.retry_review("retry-in-place", COORDINATOR, reason="Again.")


def _hermes(monkeypatch):
    monkeypatch.setattr(
        "hermes_medical_research.hermes.shutil.which",
        lambda name: "/opt/hermes" if name == "hermes" else None,
    )


@pytest.mark.asyncio
async def test_unfinished_host_session_is_failed_without_stopping_the_runner(
    tmp_path, monkeypatch
):
    automation, workspace = review_with_records(tmp_path, "host-failure", 2)
    await automation.tick()
    write_assignment(tmp_path / "h", lanes={"screening": "agent"})
    calls: list[str] = []

    def unfinished(_executable, _home, claim, _actor, role):
        calls.append(claim["task_id"])
        assert role == "selector"
        return subprocess.CompletedProcess([], 0, "", "turn limit reached")

    _hermes(monkeypatch)
    result = await steps.run_step(
        "select",
        store=tmp_path / "store",
        review="host-failure",
        hermes_home=tmp_path / "h",
        invoke=unfinished,
        # The failed Task backs off for five minutes; this step reports instead of waiting.
        max_wait=0,
    )

    assert result["state"] == "drained"
    assert result["processed"] == 0 and result["failed"] == 1
    failure = result["failures"][0]
    assert failure["task_id"] == calls[0]
    assert failure["kind"] == "screening"
    assert failure["lane"] == "agent"
    assert failure["code"] == "screening_not_recorded"
    assert failure["message"] == "turn limit reached"
    # Three fresh sessions for the one Task, then the durable failure path.
    assert len(calls) == 3
    task = workspace.load()["task_engine"]["tasks"][calls[0]]
    assert task["state"] == "pending" and task["automation_failures"][0]["message"] == (
        "turn limit reached"
    )


@pytest.mark.asyncio
async def test_fulltext_tasks_are_submitted_without_a_model_session(tmp_path, monkeypatch):
    automation, workspace = review_with_records(tmp_path, "fulltext-runner", 1)
    await automation.tick()
    actor = Actor("hmr-selector", "include-session", "selector")
    claim = automation.claim("select", actor)
    path = Path(claim["proposal_path"])
    proposal = json.loads(path.read_text())
    record_id = proposal["stages"]["screening"]["records"][0]["record_id"]
    proposal["stages"]["screening"]["records"][0].update(
        decision="include", reason="Matches the synthetic criteria."
    )
    path.write_text(json.dumps(proposal))
    await TaskEngine(workspace).submit(
        "select", claim["task_id"], path, actor, claim_token=claim["claim_token"]
    )
    claim = automation.claim("select", actor)
    path = Path(claim["proposal_path"])
    proposal = json.loads(path.read_text())
    proposal["stages"]["coverage"]["records"][0].update(
        selection="selected", reason="Direct evidence.", protocol_outcomes=["Function"]
    )
    path.write_text(json.dumps(proposal))
    await TaskEngine(workspace).submit(
        "select", claim["task_id"], path, actor, claim_token=claim["claim_token"]
    )

    async def unavailable(workspace, ids, **_kwargs):
        manifest = workspace.load()
        manifest["fulltext_attempts"][ids[0]] = {"status": "unavailable", "error": "fixture"}
        workspace.save(manifest)
        return {"fulltexts": {ids[0]: "unavailable"}}

    sessions: list[str] = []

    def no_session(_executable, _home, claim, _actor, _role):
        sessions.append(claim["kind"])
        return subprocess.CompletedProcess([], 0, "", "fixture session left the task open")

    monkeypatch.setattr("hermes_medical_research.tasks.fetch_fulltexts", unavailable)
    _hermes(monkeypatch)
    write_assignment(tmp_path / "h", lanes={"studies": "agent"})
    result = await steps.run_step(
        "extract",
        store=tmp_path / "store",
        review="fulltext-runner",
        hermes_home=tmp_path / "h",
        invoke=no_session,
        max_wait=0,
    )
    assert result["processed"] == 1
    assert result["lanes"] == {"none": 1, "agent": 1}
    assert workspace.load()["fulltext_attempts"][record_id]["status"] == "unavailable"
    # The runner continued straight into the study-link task, which does need a session.
    assert sessions == ["studies", "studies", "studies"]
    assert [item["kind"] for item in result["failures"]] == ["studies"]


def test_run_step_rejects_unknown_steps_and_reports_concurrent_runners(tmp_path):
    with pytest.raises(ValidationError, match="unknown step"):
        asyncio.run(steps.run_step("coordinate", store=tmp_path / "store", review="busy"))
    from filelock import FileLock

    review_with_records(tmp_path, "busy", 1)
    steps.steps_root(tmp_path / "store").mkdir(parents=True, exist_ok=True)
    with FileLock(str(steps.lock_path(tmp_path / "store", "audit"))):
        result = asyncio.run(
            steps.run_step("audit", store=tmp_path / "store", review="busy")
        )
    assert result["state"] == "refused"
    assert "audit.lock" in result["message"]
    assert steps.read_status(tmp_path / "store", "busy", "audit")["state"] == "refused"


def test_routine_pause_controls_use_public_cron_commands(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    for profile, job_id, enabled in (
        ("hmr-coordinator", "tick1", True),
        ("hmr-extractor", "ext1", False),
    ):
        cron = home / "profiles" / profile / "cron"
        cron.mkdir(parents=True)
        (cron / "jobs.json").write_text(
            json.dumps(
                {"jobs": [{"id": job_id, "name": f"job-{profile}", "enabled": enabled}]}
            )
        )
    (home / "hmr-routines.json").write_text(
        json.dumps(
            {
                "scripts": {},
                "jobs": [
                    {"profile": "hmr-coordinator", "name": "job-hmr-coordinator"},
                    {"profile": "hmr-extractor", "name": "job-hmr-extractor"},
                ],
            }
        )
    )
    assert routine_status(home)["paused"] == ["hmr-extractor/job-hmr-extractor"]
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    _hermes(monkeypatch)
    monkeypatch.setattr("hermes_medical_research.hermes.subprocess.run", fake_run)
    result = set_routines_paused(False, hermes_home=home)
    assert calls == [["/opt/hermes", "-p", "hmr-extractor", "cron", "resume", "ext1"]]
    assert result["changed"] == ["hmr-extractor/job-hmr-extractor"]


def test_report_audits_are_grouped_per_record_and_bounded(tmp_path):
    workspace = modern_workspace(tmp_path)
    _, groups = audit.audit_groups(workspace)
    report_groups = [group for group in groups if group["kind"] == "report"]
    records = {row["record_id"] for row in workspace.rows("records")}
    # One narrative group plus one group per selected record, instead of one per target.
    assert len(report_groups) == 1 + len(records)
    for group in report_groups[1:]:
        described = {t["record_id"] for t in group["targets"] if t["kind"] != "studies"}
        assert len(described) == 1 and described <= records
        assert {"screening", "coverage", "studies", "extractions", "appraisals"} == {
            target["kind"] for target in group["targets"]
        }
    manifest = workspace.load()
    audits = [t for t in manifest["task_engine"]["tasks"].values() if t["kind"] == "audit"]
    assert len(audits) == len(groups)
    assert all(
        len(workspace.store.read_json(t["packet_file"]).__repr__()) < 32 * 1024 for t in audits
    )


def test_screening_only_records_are_batched(tmp_path):
    workspace = modern_workspace(tmp_path)
    frozen = audit.candidate(workspace)
    report_targets, _, _ = audit.build_review_targets(frozen)
    screening = next(t for t in report_targets if t["kind"] == "screening")
    extra = []
    for index in range(audit.SCREENING_BATCH + 3):
        extra.append(
            {
                **screening,
                "target_id": f"report-extra{index}",
                "entity_id": f"r-extra{index}",
                "record_id": f"r-extra{index}",
            }
        )
    groups = audit._pack_report_groups([*report_targets, *extra])
    batches = [g for g in groups if all(t["target_id"].startswith("report-extra") for t in g)]
    assert [len(batch) for batch in batches] == [audit.SCREENING_BATCH, 3]


def test_changed_record_reaudits_only_its_group(tmp_path):
    workspace = modern_workspace(tmp_path)
    _, before = audit.audit_groups(workspace)
    appraisals = workspace.read("appraisals")
    changed = appraisals["records"][0]
    changed["rationale"] = "Revised after an audit finding."
    workspace.put("appraisals", appraisals)
    workspace.put("synthesis", workspace.read("synthesis", fresh=False))
    _, after = audit.audit_groups(workspace)
    old = {group["group_id"]: group["group_digest"] for group in before}
    new = {group["group_id"]: group["group_digest"] for group in after}
    changed_groups = {group_id for group_id in new if new[group_id] != old.get(group_id)}
    extraction = workspace.index("extractions")[changed["extraction_id"]]
    finding_groups = {g for g in changed_groups if g.startswith("finding:")}
    record_groups = changed_groups - finding_groups - {
        group["group_id"] for group in after if group["targets"][0]["kind"] == "protocol"
    }
    assert len(record_groups) == 1
    (group,) = [g for g in after if g["group_id"] in record_groups]
    assert {t["record_id"] for t in group["targets"] if t["kind"] != "studies"} == {
        extraction["record_id"]
    }

    engine = TaskEngine(workspace)
    route = engine.route_next(COORDINATOR)
    task = workspace.load()["task_engine"]["tasks"][route["task_id"]]
    assert task["group_id"] in changed_groups


def test_unresolved_audit_group_halts_the_run_until_retried(tmp_path):
    workspace = modern_workspace(tmp_path)
    engine = TaskEngine(workspace)
    manifest = workspace.load()
    ledger = manifest["task_engine"]
    task = next(
        t for t in ledger["tasks"].values() if t["kind"] == "audit" and t["state"] == "accepted"
    )
    result = workspace.store.read_json(task["result_file"])
    for row in [*result["records"], *result["report_reviews"]]:
        row["status"] = "revise"
    from hermes_medical_research.workspace import digest

    task["result_file"] = f"tasks/{task['task_id']}/result-{digest(result)}.json"
    task["result_digest"] = digest(result)
    workspace.store.write_json(task["result_file"], result)
    ledger["correction_counts"] = {task["group_id"]: MAX_CORRECTIONS_PER_GROUP}
    manifest["datasets"].pop("reviews")
    workspace.save(manifest)

    route = engine.route_next(COORDINATOR)

    assert route == {"run_id": route["run_id"], "task_id": None, "state": "blocked"}
    halted = workspace.load()["task_engine"]["halt"]
    assert halted["code"] == "audit_unresolved" and halted["group_id"] == task["group_id"]
    cleared = engine.retry_blocked(reason="Human reviewed the audit.", at="2026-09-17T00:00Z")
    assert cleared["halt_cleared"]["code"] == "audit_unresolved"
    assert "halt" not in workspace.load()["task_engine"]


def test_grouped_audits_still_finalize_multi_record_reports(tmp_path):
    workspace = modern_workspace(tmp_path)
    assert check(workspace)["ready"]
    record_reviews(workspace)
    assert asyncio.run(_finalized(workspace))


async def _finalized(workspace) -> bool:
    from hermes_medical_research.workflow import finalize

    return (await finalize(workspace, offline=True))["completed"]


@pytest.mark.asyncio
async def test_runner_renews_the_claim_while_a_slow_session_works(tmp_path, monkeypatch):
    current = [datetime.now(UTC) + timedelta(seconds=2)]
    automation, workspace = review_with_records(
        tmp_path, "slow-session", 1, clock=lambda: current[0]
    )
    await automation.tick()
    actor = Actor("hmr-selector", "slow-session", "selector")
    claim = automation.claim("select", actor)
    first_expiry = claim["expires_at"]
    current[0] += timedelta(minutes=50)
    renewed = automation.renew(claim["claim_id"], actor)
    assert renewed["expires_at"] > first_expiry
    task = workspace.load()["task_engine"]["tasks"][claim["task_id"]]
    assert task["lease"]["expires_at"] == renewed["expires_at"]
    assert task["lease"]["renewals"] == 1
    with pytest.raises(ValidationError, match="different Hermes session"):
        automation.renew(claim["claim_id"], Actor("hmr-selector", "other", "selector"))
    current[0] += timedelta(minutes=61)
    with pytest.raises(ValidationError, match="already expired"):
        automation.renew(claim["claim_id"], actor)


@pytest.mark.asyncio
async def test_run_with_renewal_extends_the_lease_until_the_session_exits(monkeypatch):
    import threading

    from hermes_medical_research import hermes

    renewals: list[str] = []
    release = threading.Event()

    class FakeAutomation:
        def renew(self, claim_id, _actor):
            renewals.append(claim_id)
            if len(renewals) == 2:
                release.set()

    def slow_session(_claim, _actor):
        release.wait(5)
        return subprocess.CompletedProcess([], 0, "done", "")

    monkeypatch.setattr(hermes, "LEASE_RENEW_SECONDS", 0.01)
    completed = await hermes.run_with_renewal(
        FakeAutomation(), {"claim_id": "claim-x"}, None, runner=slow_session
    )
    assert completed.stdout == "done"
    assert renewals[:2] == ["claim-x", "claim-x"]


def test_screening_reuse_ignores_citation_counts_and_source_rank():
    from hermes_medical_research.automation import _source_digest

    record = {
        "title": "Trial",
        "abstract": "Text",
        "citation_count": 3,
        "source_rank": 7,
        "source_records": [{"source": "openalex", "source_id": "W1", "source_rank": 7}],
    }
    later = {
        **record,
        "citation_count": 9,
        "source_rank": 2,
        "source_records": [{"source": "openalex", "source_id": "W1", "source_rank": 2}],
    }
    assert _source_digest(record) == _source_digest(later)
    assert _source_digest(record) != _source_digest({**record, "abstract": "Changed text"})


def test_routines_remove_deletes_only_the_jobs_its_manifest_records(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    cron = home / "profiles" / "hmr-selector" / "cron"
    cron.mkdir(parents=True)
    (cron / "jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {"id": "managed-1", "name": "hmr-work-selector", "enabled": True},
                    {"id": "operator-1", "name": "my-own-job", "enabled": True},
                ]
            }
        )
    )
    script = home / "profiles" / "hmr-selector" / "scripts" / "hmr-work-selector.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\nexec hmr hermes drain --role selector\n")
    unmanaged_script = script.with_name("operator.sh")
    unmanaged_script.write_text("#!/bin/sh\necho mine\n")
    (home / "hmr-routines.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "hmr-selector/scripts/hmr-work-selector.sh": hashlib.sha256(
                        script.read_bytes()
                    ).hexdigest()
                },
                "jobs": [
                    {"profile": "hmr-selector", "name": "hmr-work-selector", "id": "managed-1"}
                ],
            }
        )
    )

    plan = routines(hermes_home=home)
    assert not plan["applied"]
    assert plan["workflow"] == "user-invoked-steps"
    assert plan["legacy_jobs"] == [
        {"profile": "hmr-selector", "name": "hmr-work-selector", "id": "managed-1",
         "present": True}
    ]
    assert plan["scripts"] == ["hmr-selector/scripts/hmr-work-selector.sh"]
    assert "--remove --apply" in plan["next"]
    with pytest.raises(ValidationError, match="retired"):
        routines(apply=True, hermes_home=home)

    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    _hermes(monkeypatch)
    monkeypatch.setattr("hermes_medical_research.hermes.subprocess.run", fake_run)
    result = routines(apply=True, remove=True, hermes_home=home)

    assert result["applied"]
    assert result["removed"] == ["hmr-selector/hmr-work-selector"]
    assert result["remaining"] == []
    # The fleet is paused before anything is deleted, and only the manifest's own job is touched.
    assert calls == [
        ["/opt/hermes", "-p", "hmr-selector", "cron", "pause", "managed-1"],
        ["/opt/hermes", "-p", "hmr-selector", "cron", "delete", "managed-1"],
    ]
    assert not script.exists()
    assert unmanaged_script.read_text() == "#!/bin/sh\necho mine\n"
    assert not (home / "hmr-routines.json").exists()
    assert [job["id"] for job in json.loads((cron / "jobs.json").read_text())["jobs"]] == [
        "managed-1",
        "operator-1",
    ]


def test_a_findings_evidence_is_shown_once_not_inside_two_assertions(tmp_path):
    """One finding's evidence list used to sit inside both its harms and certainty assertions.

    An auditor has to echo an assertion back verbatim, so the list was paid for twice in the packet
    and twice again in the answer. On the first real review that put one group at 34105 bytes
    against the 32768-byte bound, and no split could have helped: a finding audit's record must
    answer every review check at once.
    """
    workspace = modern_workspace(tmp_path)
    _, groups = audit.audit_groups(workspace)
    finding_groups = [group for group in groups if group["kind"] == "finding"]
    assert finding_groups, "the fixture should synthesize at least one finding"

    for group in finding_groups:
        assertions = {target["check"]: target["assertion"] for target in group["targets"]}
        assert "evidence" not in assertions["harms"]
        assert "evidence" not in assertions["certainty"]
        # Shown once for both checks, so the auditor can still judge them against it.
        assert group["evidence"] == next(
            row["evidence"]
            for row in workspace.read("synthesis")["findings"]
            if row["finding_id"] == group["entity_id"]
        )


def test_an_oversized_packet_names_the_field_that_overflowed(tmp_path):
    workspace = modern_workspace(tmp_path)
    engine = TaskEngine(workspace)
    manifest = workspace.load()
    spec = {
        "kind": "audit",
        "target_ids": ["t-1"],
        "instructions": "Check it.",
        "proposal": {"schema_version": "2"},
        "packet_data": {"audit_group": {"filler": "x" * 40 * 1024}, "small": "y"},
        "allowed_source_ids": [],
    }
    with pytest.raises(ValidationError) as error:
        engine._create_task(manifest, engine._ledger(manifest), spec)

    message = str(error.value)
    assert "audit task packet is" in message
    assert "audit_group is the largest field" in message


def _assertions(workspace):
    report, findings, memberships = audit.build_review_targets(audit.candidate(workspace))
    return [*report, *findings, *memberships]


def _quotes_in(value) -> list[str]:
    """Every `quote` string anywhere inside an assertion."""
    found = []
    if isinstance(value, dict):
        for key, member in value.items():
            if key == "quote" and isinstance(member, str):
                found.append(member)
            else:
                found.extend(_quotes_in(member))
    elif isinstance(value, list):
        for member in value:
            found.extend(_quotes_in(member))
    return found


def test_no_assertion_carries_the_quote_that_would_answer_it(tmp_path):
    """The quote is the answer, so it must not sit inside the question.

    An `extractions` target requires a citation, and its own row's `source_location.quote` used to
    be part of the assertion: already in `allowed_document_ids`, already verbatim-checked at submit
    time. An auditor could satisfy the requirement by copying it out of the question without ever
    opening a document, so independent verification was not enforced at all.
    """
    workspace = modern_workspace(tmp_path)

    targets = _assertions(workspace)
    cited = [t for t in targets if t.get("requires_sources")]
    assert cited, "the fixture must contain at least one target that requires a citation"

    for target in targets:
        leaked = _quotes_in(json.loads(target["assertion"]))
        assert not leaked, (
            f"{target.get('kind')}/{target.get('check')} assertion still carries "
            f"{leaked[0][:40]!r}"
        )


def test_an_assertion_still_says_where_to_look(tmp_path):
    """Stripping the quote must not strip the locator: the auditor needs somewhere to read."""
    workspace = modern_workspace(tmp_path)

    extraction = next(t for t in _assertions(workspace) if t.get("kind") == "extractions")
    location = json.loads(extraction["assertion"])["source_location"]

    assert location["document_id"] and location["locator"]
    assert "quote" not in location
