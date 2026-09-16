"""Cron automation keeps human Review state separate from bounded Task state."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_research import completed_search

from hermes_medical_research.automation import AutomationEngine
from hermes_medical_research.hermes import _routine_specs, routines
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
    first_probe = automation.probe("searcher")
    assert first_probe["state"] == "ready"
    assert automation.probe("searcher") == first_probe

    actor = Actor("mdr-searcher", "cron-session", "searcher")
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
            Actor("mdr-searcher", "replacement", "searcher"),
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

    actor = Actor("mdr-selector", "continuous-session", "selector")
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
    searcher = Actor("mdr-searcher", "cancel-session", "searcher")
    claim = automation.claim("search", searcher)

    result = automation.cancel_review(
        "cancelled-review",
        Actor("mdr-coordinator", "operator-session", "coordinator"),
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
    actor = Actor("mdr-searcher", "retry-session", "searcher")

    first = automation.claim("search", actor)
    failed = automation.fail(first["claim_id"], actor, code="fixture", message="one")
    assert failed["attempt"] == 1 and not failed["blocked"]
    assert automation.probe("searcher")["state"] == "idle"
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


def test_routines_are_dry_run_first_and_plan_six_base_jobs(tmp_path: Path):
    result = routines(hermes_home=tmp_path / "hermes", store=tmp_path / "store")
    assert not result["applied"]
    assert len(result["jobs"]) == 6
    assert {job["name"] for job in result["jobs"]} == {
        "mdr-work-tick",
        "mdr-work-searcher",
        "mdr-work-selector",
        "mdr-work-extractor",
        "mdr-work-synthesizer",
        "mdr-work-auditor",
    }
    for job in result["jobs"]:
        if not job["no_agent"]:
            assert f"--actor {job['profile']}" in job["prompt"]
    selector = next(job for job in result["jobs"] if job["profile"] == "mdr-selector")
    assert "up to 10 accepted tasks" in selector["prompt"]
    assert not (tmp_path / "hermes").exists()


def test_routine_scripts_pin_the_mdr_executable(tmp_path: Path, monkeypatch):
    executable = tmp_path / "bin" / "mdr"
    executable.parent.mkdir()
    executable.touch()
    monkeypatch.setattr(
        "hermes_medical_research.hermes.shutil.which",
        lambda name: str(executable) if name == "mdr" else None,
    )

    _, scripts = _routine_specs(tmp_path / "store", tmp_path / "hermes")

    assert scripts
    assert all(f"exec {executable} ".encode() in content for content in scripts.values())


def test_living_review_adds_real_cadence_routine(tmp_path: Path):
    store = tmp_path / "store"
    coordinator = tmp_path / "hermes" / "profiles" / "mdr-coordinator"
    coordinator.mkdir(parents=True)
    (coordinator / "config.yaml").write_text("timezone: Asia/Seoul\n")
    AutomationEngine(store).create_review(
        "living-exercise",
        protocol(),
        schedule="0 3 * * 1",
        timezone="Asia/Seoul",
    )
    result = routines(hermes_home=tmp_path / "hermes", store=store)
    scheduled = next(job for job in result["jobs"] if job["name"] == "mdr-review-living-exercise")
    assert scheduled["schedule"] == "0 3 * * 1"
    assert scheduled["no_agent"]
