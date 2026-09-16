"""Cron automation keeps human Review state separate from bounded Task state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hermes_medical_research.automation import AutomationEngine
from hermes_medical_research.hermes import routines
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
    assert not (tmp_path / "hermes").exists()


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
