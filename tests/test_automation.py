"""Durable automation keeps human Review state separate from bounded Task state."""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_research import completed_search

from hermes_medical_research import steps
from hermes_medical_research.automation import AutomationEngine
from hermes_medical_research.hermes import write_assignment
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


@pytest.mark.asyncio
async def test_review_tick_claim_and_capability_bound_submission(tmp_path: Path):
    automation = AutomationEngine(tmp_path)
    created = automation.create_review(
        "exercise-review",
        protocol(),
        schedule="once",
        timezone="Asia/Seoul",
    )
    assert created["active_cycle"] == 1
    assert (await automation.tick())["routed"] == 1

    actor = Actor("hmr-searcher", "cron-session", "searcher")
    claim = automation.claim("search", actor)
    assert claim["attempt"] == 1
    assert "--claim-token" in claim["submit"]
    assert "search run" in claim["run"]
    assert "search execute" in claim["execute"]
    assert f"--from {claim['proposal_path']}" in claim["execute"]
    assert "approve" not in claim
    engine = TaskEngine(automation.catalog.workspace(claim["run_id"]))
    with pytest.raises(ValidationError, match="claim token"):
        await engine.submit(
            "search",
            claim["task_id"],
            Path(claim["proposal_path"]),
            actor,
            claim_token="wrong",
        )
    with pytest.raises(ValidationError, match="different Hermes session"):
        await engine.submit(
            "search",
            claim["task_id"],
            Path(claim["proposal_path"]),
            Actor("hmr-searcher", "replacement", "searcher"),
            claim_token=claim["claim_token"],
        )
    accepted = await engine.submit(
        "search",
        claim["task_id"],
        Path(claim["proposal_path"]),
        actor,
        claim_token=claim["claim_token"],
    )
    assert accepted["state"] == "plan_recorded"


@pytest.mark.asyncio
async def test_selector_acceptance_immediately_routes_the_next_record(tmp_path: Path):
    automation = AutomationEngine(tmp_path)
    created = automation.create_review(
        "continuous-selection",
        protocol(),
        schedule="once",
        timezone="Asia/Seoul",
    )
    run_id = automation.review_status(created["name"])["cycles"][0]["run_id"]
    workspace = automation.catalog.workspace(run_id)
    search, _ = completed_search(workspace, tmp_path / "search", count=2)
    workspace.attach(search.path)
    assert (await automation.tick())["routed"] == 1

    actor = Actor("hmr-selector", "continuous-session", "selector")
    claim = automation.claim("select", actor)
    proposal_path = Path(claim["proposal_path"])
    proposal = json.loads(proposal_path.read_text())
    proposal["stages"]["screening"]["records"][0].update(
        decision="exclude",
        reason="Fails the synthetic eligibility criteria.",
    )
    proposal_path.write_text(json.dumps(proposal))
    receipt = await TaskEngine(workspace).submit(
        "select",
        claim["task_id"],
        proposal_path,
        actor,
        claim_token=claim["claim_token"],
    )

    assert receipt["continuation"]["state"] == "pending"
    next_claim = automation.claim("select", actor)
    assert next_claim["role"] == "selector"
    assert next_claim["task_id"] == receipt["continuation"]["task_id"]


@pytest.mark.asyncio
async def test_coordinator_cancels_an_active_cycle_with_audit_trail(tmp_path: Path):
    automation = AutomationEngine(tmp_path)
    automation.create_review(
        "cancelled-review",
        protocol(),
        schedule="once",
        timezone="Asia/Seoul",
    )
    await automation.tick()
    searcher = Actor("hmr-searcher", "cancel-session", "searcher")
    claim = automation.claim("search", searcher)

    result = automation.cancel_review(
        "cancelled-review",
        Actor("hmr-coordinator", "operator-session", "coordinator"),
        reason="Replace the run affected by the lost search reservation defect.",
    )
    assert result["state"] == "paused"
    assert result["cycle_state"] == "blocked"
    assert result["code"] == "operator_cancelled"
    status = automation.review_status("cancelled-review")
    assert status["latest_cycle_state"] == "blocked"
    assert status["active_cycle"] is None
    engine = TaskEngine(automation.catalog.workspace(claim["run_id"]))
    task = engine.workspace.load()["task_engine"]["tasks"][claim["task_id"]]
    assert task["state"] == "blocked"
    assert task["blocked_code"] == "operator_cancelled"
    stored_claim = automation.claims.joinpath(f"{claim['claim_id']}.json")
    assert json.loads(stored_claim.read_text())["state"] == "cancelled"
    assert automation.notification_probe()["state"] == "ready"
    with pytest.raises(ValidationError, match="active claim"):
        await engine.submit(
            "search",
            claim["task_id"],
            Path(claim["proposal_path"]),
            searcher,
            claim_token=claim["claim_token"],
        )


@pytest.mark.asyncio
async def test_three_attempts_use_bounded_backoff_then_block(tmp_path: Path):
    current = [datetime.now(UTC) + timedelta(seconds=2)]
    automation = AutomationEngine(tmp_path, clock=lambda: current[0])
    automation.create_review(
        "retry-review",
        protocol(),
        schedule="0 3 * * 1",
        timezone="Asia/Seoul",
    )
    await automation.tick()
    actor = Actor("hmr-searcher", "retry-session", "searcher")

    first = automation.claim("search", actor)
    failed = automation.fail(first["claim_id"], actor, code="fixture", message="one")
    assert failed["attempt"] == 1 and not failed["blocked"]
    assert automation.claim("search", actor)["state"] == "idle"
    assert automation.next_available_at("searcher") is not None
    current[0] += timedelta(minutes=5)

    second = automation.claim("search", actor)
    failed = automation.fail(second["claim_id"], actor, code="fixture", message="two")
    assert failed["attempt"] == 2 and not failed["blocked"]
    current[0] += timedelta(minutes=30)

    third = automation.claim("search", actor)
    failed = automation.fail(third["claim_id"], actor, code="fixture", message="three")
    assert failed["attempt"] == 3 and failed["blocked"]
    status = automation.review_status("retry-review")
    assert status["latest_cycle_state"] == "blocked"
    assert automation.notification_probe()["state"] == "ready"


def test_serial_selector_uses_one_fresh_host_session_per_article(tmp_path: Path):
    automation = AutomationEngine(tmp_path / "store")
    created = automation.create_review(
        "serial-selection",
        protocol(),
        schedule="once",
        timezone="Asia/Seoul",
    )
    run_id = automation.review_status(created["name"])["cycles"][0]["run_id"]
    workspace = automation.catalog.workspace(run_id)
    search, _ = completed_search(workspace, tmp_path / "search", count=12)
    workspace.attach(search.path)
    asyncio.run(automation.tick())
    # Screening is answered by a constrained call by default; this is the session lane, so the
    # operator's per-kind assignment pins it.
    write_assignment(tmp_path / "hermes", lanes={"screening": "agent"})
    sessions: list[str] = []

    def accept_one(_executable, _home, claim, actor, role):
        assert role == "selector"
        packet = json.loads(Path(claim["packet_path"]).read_text())
        assert len(packet["target_ids"]) == 1
        proposal_path = Path(claim["proposal_path"])
        proposal = json.loads(proposal_path.read_text())
        proposal["stages"]["screening"]["records"][0].update(
            decision="exclude",
            reason="Fails the synthetic eligibility criteria.",
        )
        proposal_path.write_text(json.dumps(proposal))
        asyncio.run(
            TaskEngine(workspace).submit(
                "select",
                claim["task_id"],
                proposal_path,
                actor,
                claim_token=claim["claim_token"],
            )
        )
        sessions.append(actor.session_id)
        return subprocess.CompletedProcess([], 0, "accepted", "")

    result = asyncio.run(
        steps.run_step(
            "select",
            store=tmp_path / "store",
            review="serial-selection",
            hermes_home=tmp_path / "hermes",
            invoke=accept_one,
        )
    )

    assert result["state"] == "drained"
    assert result["processed"] == 12 and result["failed"] == 0
    assert result["lanes"] == {"agent": 12}
    assert len(sessions) == len(set(sessions)) == 12
    assert len(workspace.rows("screening")) == 12


def test_serial_selector_honors_a_failure_recorded_by_the_host(tmp_path: Path):
    automation = AutomationEngine(tmp_path / "store")
    created = automation.create_review(
        "failed-selection",
        protocol(),
        schedule="once",
        timezone="Asia/Seoul",
    )
    run_id = automation.review_status(created["name"])["cycles"][0]["run_id"]
    workspace = automation.catalog.workspace(run_id)
    search, _ = completed_search(workspace, tmp_path / "search", count=1)
    workspace.attach(search.path)
    asyncio.run(automation.tick())
    write_assignment(tmp_path / "hermes", lanes={"screening": "agent"})

    def fail_one(_executable, _home, claim, actor, _role):
        automation.fail(
            claim["claim_id"],
            actor,
            code="fixture_failure",
            message="The host recorded its exact fail command.",
        )
        return subprocess.CompletedProcess([], 0, "failed", "")

    result = asyncio.run(
        steps.run_step(
            "select",
            store=tmp_path / "store",
            review="failed-selection",
            hermes_home=tmp_path / "hermes",
            invoke=fail_one,
            # The failed Task backs off for five minutes; this step reports instead of waiting.
            max_wait=0,
        )
    )
    assert result["state"] == "drained"
    assert result["processed"] == 0 and result["failed"] == 1
    assert [item["code"] for item in result["failures"]] == ["screening_not_recorded"]

    task = next(
        task
        for task in workspace.load()["task_engine"]["tasks"].values()
        if task["role"] == "selector"
    )
    assert task["state"] == "pending"
    # The host already failed its own claim, so the runner's fail was refused rather than
    # spending a second of the three bounded attempts.
    assert task["automation_attempts"] == 1
    assert [failure["code"] for failure in task["automation_failures"]] == ["fixture_failure"]


@pytest.mark.asyncio
async def test_claim_returns_token_bound_find_and_read_commands(tmp_path: Path):
    automation = AutomationEngine(tmp_path)
    created = automation.create_review(
        "source-commands",
        protocol(),
        schedule="once",
        timezone="Asia/Seoul",
    )
    run_id = automation.review_status(created["name"])["cycles"][0]["run_id"]
    workspace = automation.catalog.workspace(run_id)
    search, _ = completed_search(workspace, tmp_path / "search", count=1)
    workspace.attach(search.path)
    await automation.tick()

    claim = automation.claim("select", Actor("hmr-selector", "find-session", "selector"))
    for key in ("source_list", "source_show", "source_find", "source_read"):
        assert f"--claim-token {claim['claim_token']}" in claim[key]
    assert claim["source_find"].endswith("source find " f"{run_id} {claim['task_id']} SEARCH WORDS")
    assert claim["source_read"].endswith("DOCUMENT_ID LOCATOR")
