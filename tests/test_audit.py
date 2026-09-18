"""Task routing and immutable audit receipts replace host-native delegation tests."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_evidence_workflow import modern_workspace
from test_research import completed_search, protocol

from hermes_medical_research.cli import main
from hermes_medical_research.search.artifacts import preflight_digest, strategy_digest
from hermes_medical_research.search.models import ValidationError
from hermes_medical_research.tasks import Actor, RunCatalog, TaskEngine
from hermes_medical_research.workspace import Workspace

COORDINATOR = Actor("hmr-coordinator", "coordinator-session", "coordinator")
SELECTOR = Actor("hmr-selector", "selector-session", "selector")
SEARCHER = Actor("hmr-searcher", "searcher-session", "searcher")


def selector_workspace(tmp_path: Path) -> tuple[TaskEngine, dict]:
    catalog = RunCatalog(tmp_path / "store")
    created = catalog.create(protocol(), records=1, fulltexts=1)
    workspace = catalog.workspace(created["run_id"])
    search, _ = completed_search(workspace, tmp_path / "search", count=1)
    workspace.attach(search.path)
    return TaskEngine(workspace), created


def _search_fixture(tmp_path: Path, *, optional_source: bool = False):
    request = protocol()
    if optional_source:
        request["sources"] = ["europe-pmc", "openalex"]
    catalog = RunCatalog(tmp_path / "store")
    created = catalog.create(request, records=10, fulltexts=1)
    engine = TaskEngine(catalog.workspace(created["run_id"]))
    route = engine.route_next(COORDINATOR)
    opened = engine.role_next("search", route["task_id"], SEARCHER)
    proposal_path = Path(opened["proposal_path"])
    proposal = json.loads(proposal_path.read_text())
    proposal["mesh"] = False
    proposal_path.write_text(json.dumps(proposal))
    return engine, route["task_id"], proposal_path


def _search_preflight(strategy, *, core_count: int) -> dict:
    sources = {
        source: (
            {"status": "available", "count": core_count, "error": None}
            if source == "europe-pmc"
            else {"status": "unavailable", "count": None, "error": "optional fixture outage"}
        )
        for source in strategy.strategies
    }
    result = {
        "schema_version": "2",
        "created_at": datetime.now(UTC).isoformat(),
        "mode": strategy.mode,
        "limit_per_source": strategy.limit_per_source,
        "strategy_digest": strategy_digest(strategy),
        "sources": sources,
        "ready": all(item["status"] == "available" for item in sources.values()),
    }
    result["preflight_digest"] = preflight_digest(result)
    return result


@pytest.mark.asyncio
async def test_selector_records_one_decision_and_replays_from_fresh_session(tmp_path):
    engine, created = selector_workspace(tmp_path)
    route = engine.route_next(COORDINATOR)
    assert route == {
        "run_id": created["run_id"],
        "task_id": route["task_id"],
        "role": "selector",
        "profile": "hmr-selector",
        "state": "pending",
    }
    opened = engine.role_next("select", route["task_id"], SELECTOR)
    packet = json.loads(Path(opened["packet_path"]).read_text())
    proposal_path = Path(opened["proposal_path"])
    proposal = json.loads(proposal_path.read_text())
    assigned = packet["target_ids"][0]
    row = proposal["stages"]["screening"]["records"][0]
    row.update(decision="include", reason="Matches every synthetic eligibility criterion.")
    proposal_path.write_text(json.dumps(proposal))

    accepted = await engine.submit("select", route["task_id"], proposal_path, SELECTOR)
    assert accepted["state"] == "accepted"
    assert engine.workspace.index("screening")[assigned]["decision"] == "include"

    fresh = Actor("hmr-selector", "fresh-selector-session", "selector")
    replay = await engine.submit("select", route["task_id"], proposal_path, fresh)
    assert replay == accepted


@pytest.mark.asyncio
async def test_selector_cannot_submit_extra_or_stale_records(tmp_path):
    engine, _ = selector_workspace(tmp_path)
    route = engine.route_next(COORDINATOR)
    opened = engine.role_next("select", route["task_id"], SELECTOR)
    path = Path(opened["proposal_path"])
    proposal = json.loads(path.read_text())
    row = proposal["stages"]["screening"]["records"][0]
    row.update(decision="include", reason="Synthetic match.")
    proposal["stages"]["screening"]["records"].append(
        {**deepcopy(row), "record_id": "r-outside"}
    )
    path.write_text(json.dumps(proposal))
    with pytest.raises(ValidationError, match="exactly the assigned record"):
        await engine.submit("select", route["task_id"], path, SELECTOR)

    proposal["stages"]["screening"]["records"] = [row]
    path.write_text(json.dumps(proposal))
    records = engine.workspace.read("records")
    records["records"][0]["title"] += " revised"
    engine.workspace.put("records", records, validate=False)
    with pytest.raises(ValidationError, match="stale submission"):
        await engine.submit("select", route["task_id"], path, SELECTOR)


def test_in_progress_proposal_resumes_under_a_fresh_profile_session(tmp_path):
    engine, _ = selector_workspace(tmp_path)
    route = engine.route_next(COORDINATOR)
    opened = engine.role_next("select", route["task_id"], SELECTOR)
    path = Path(opened["proposal_path"])
    proposal = json.loads(path.read_text())
    proposal["stages"]["screening"]["records"][0]["reason"] = "Work in progress."
    path.write_text(json.dumps(proposal))
    resumed = engine.role_next(
        "select",
        route["task_id"],
        Actor("hmr-selector", "replacement-session", "selector"),
    )
    assert resumed["proposal_path"] == str(path)
    assert json.loads(path.read_text()) == proposal


@pytest.mark.asyncio
async def test_excluding_every_record_materializes_empty_evidence_stages(tmp_path):
    engine, _ = selector_workspace(tmp_path)
    route = engine.route_next(COORDINATOR)
    opened = engine.role_next("select", route["task_id"], SELECTOR)
    path = Path(opened["proposal_path"])
    proposal = json.loads(path.read_text())
    proposal["stages"]["screening"]["records"][0].update(
        decision="exclude", reason="Fails the synthetic eligibility criteria."
    )
    path.write_text(json.dumps(proposal))
    await engine.submit("select", route["task_id"], path, SELECTOR)
    next_route = engine.route_next(COORDINATOR)
    assert next_route["role"] == "synthesizer"
    for stage in ("coverage", "studies", "extractions", "appraisals"):
        assert engine.workspace.read(stage)["records"] == []


def test_study_link_task_can_add_only_its_assigned_report(tmp_path):
    workspace = modern_workspace(tmp_path)
    engine = TaskEngine(workspace)
    records = workspace.rows("records")
    existing = workspace.rows("studies")[0]
    assigned = records[1]["record_id"]
    proposal = engine._studies_spec(assigned, existing["study_id"])["proposal"]
    row = proposal["stages"]["studies"]["records"][0]
    row["record_ids"].append(assigned)
    task = {
        "kind": "studies",
        "target_ids": [assigned],
        "base_digests": proposal["base_digests"],
    }
    engine._validate_batch_scope(task, proposal)
    row["record_ids"].append("r-outside")
    with pytest.raises(ValidationError, match="only add"):
        engine._validate_batch_scope(task, proposal)

def test_source_access_is_bounded_to_active_task(tmp_path):
    engine, _ = selector_workspace(tmp_path)
    route = engine.route_next(COORDINATOR)
    engine.role_next("select", route["task_id"], SELECTOR)
    source_id = engine.source_list(route["task_id"], 1, SELECTOR)["source_ids"][0]
    page = engine.source_show(route["task_id"], source_id, 1, SELECTOR)
    assert len(page["content"].encode()) <= 16 * 1024
    with pytest.raises(ValidationError, match="bounded corpus"):
        engine.source_show(route["task_id"], "outside:document", 1, SELECTOR)
    with pytest.raises(ValidationError, match="active task"):
        engine.source_show(
            route["task_id"],
            source_id,
            1,
            Actor("hmr-extractor", "other-session", "extractor"),
        )


def test_auditor_is_independent_and_every_receipt_is_immutable(tmp_path):
    workspace = modern_workspace(tmp_path)
    manifest = workspace.load()
    audit_tasks = [
        task
        for task in manifest["task_engine"]["tasks"].values()
        if task["kind"] == "audit" and task["state"] == "accepted"
    ]
    assert audit_tasks
    assert {task["actor_profile"] for task in audit_tasks} == {"hmr-auditor"}
    assert "hmr-auditor" not in {
        profile
        for profiles in manifest["task_engine"]["authors"].values()
        for profile in profiles
    }
    task = next(
        task
        for task in audit_tasks
        if workspace.store.read_json(task["result_file"])["records"]
    )
    reviews = workspace.read("reviews")
    result = workspace.store.read_json(task["result_file"])
    result["records"] = []
    workspace.store.write_json(task["result_file"], result)
    with pytest.raises(ValidationError, match="modified"):
        workspace.put("reviews", reviews)


def test_migration_archives_v04_review_and_requires_fresh_audit(tmp_path):
    legacy = Workspace(tmp_path / "legacy")
    legacy.init(
        protocol(),
        mode="report",
        records=1,
        fulltexts=1,
        language="en",
        evidence_version="2",
    )
    manifest = legacy.load()
    manifest["tool_version"] = "0.4.0"
    legacy.save(manifest)
    legacy.store.write_json("native-review.json", {"legacy": True})

    catalog = RunCatalog(tmp_path / "store")
    migrated = catalog.migrate(legacy.path)
    workspace = catalog.workspace(migrated["run_id"])
    imported = workspace.load()
    assert imported["migration"]["fresh_audit_required"]
    assert not (workspace.path / "native-review.json").exists()
    assert (workspace.path / "provenance/v0.4/native-review.json").is_file()


def test_actor_roles_are_enforced(tmp_path):
    engine, _ = selector_workspace(tmp_path)
    route = engine.route_next(COORDINATOR)
    with pytest.raises(ValidationError, match="selector role required"):
        engine.role_next(
            "select",
            route["task_id"],
            Actor("hmr-synthesizer", "wrong-session", "synthesizer"),
        )


@pytest.mark.asyncio
async def test_search_reservation_and_task_link_are_atomic(monkeypatch, tmp_path):
    engine, task_id, proposal_path = _search_fixture(tmp_path, optional_source=True)

    async def fake_preflight(strategy, _session, _credentials):
        return _search_preflight(strategy, core_count=7)

    async def fake_execute(strategy, store, _session, _credentials, inspected):
        key = strategy_digest(strategy)
        reservation = engine.workspace.load()["searches"].get(key)
        assert reservation is not None
        assert reservation["status"] == "reserved"
        assert reservation["path"] == str(store.path.resolve())
        manifest = store.read_json("manifest.json")
        for source, state in manifest["sources"].items():
            detail = inspected["sources"][source]
            state.update(
                status="complete" if detail["status"] == "available" else "omitted",
                retrieved=0,
                retained=0,
                filtered_out=0,
                reported_total=detail["count"],
                error=detail["error"],
            )
        manifest["status"] = "complete"
        store.write_manifest(manifest)
        summary = {
            "schema_version": "2",
            "records_by_source": {source: 0 for source in strategy.strategies},
            "source_failures": {
                source: detail["error"]
                for source, detail in inspected["sources"].items()
                if detail["status"] != "available"
            },
        }
        store.write_json("summary.json", summary)
        return summary

    monkeypatch.setattr("hermes_medical_research.tasks.preflight", fake_preflight)
    monkeypatch.setattr("hermes_medical_research.tasks.execute_search", fake_execute)
    accepted = await engine.execute_search_plan(
        task_id,
        SEARCHER,
        proposal_path=proposal_path,
    )
    assert accepted["state"] == "accepted"
    manifest = engine.workspace.load()
    reservation = next(iter(manifest["searches"].values()))
    assert reservation["status"] == "attached"
    snapshot = engine.workspace.store.read_json(reservation["snapshot"])
    assert snapshot["manifest"]["sources"]["openalex"] == {
        "cursor": None,
        "error": "optional fixture outage",
        "filtered_out": 0,
        "reported_total": None,
        "retained": 0,
        "retrieved": 0,
        "status": "omitted",
        "truncated": False,
    }


@pytest.mark.asyncio
async def test_zero_core_hits_freezes_materialized_search_plan(monkeypatch, tmp_path):
    engine, task_id, proposal_path = _search_fixture(tmp_path)

    async def fake_preflight(strategy, _session, _credentials):
        return _search_preflight(strategy, core_count=0)

    monkeypatch.setattr("hermes_medical_research.tasks.preflight", fake_preflight)
    with pytest.raises(ValidationError, match="zero_core_hits"):
        await engine.execute_search_plan(
            task_id,
            SEARCHER,
            proposal_path=proposal_path,
        )
    task = engine.workspace.load()["task_engine"]["tasks"][task_id]
    assert task["search_path"]
    assert len(engine.workspace.load()["searches"]) == 1

    changed = json.loads(proposal_path.read_text())
    changed["limit_per_source"] -= 1
    proposal_path.write_text(json.dumps(changed))
    with pytest.raises(ValidationError, match="search plan is frozen"):
        await engine.execute_search_plan(
            task_id,
            SEARCHER,
            proposal_path=proposal_path,
        )


def test_hmr_cli_creates_and_routes_an_opaque_run(tmp_path, capsys):
    request = tmp_path / "request.json"
    request.write_text(json.dumps(protocol()))
    store = tmp_path / "store"
    assert main(["--store", str(store), "run", "create", "--request", str(request)]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["run_id"].startswith("run-")
    assert (
        main(
            [
                "--store",
                str(store),
                "--actor",
                "hmr-coordinator",
                "run",
                "next",
                created["run_id"],
            ]
        )
        == 0
    )
    routed = json.loads(capsys.readouterr().out)
    assert routed["run_id"] == created["run_id"]
    assert routed["profile"] == "hmr-searcher"


def test_global_options_are_accepted_after_the_subcommand(tmp_path, capsys):
    from hermes_medical_research.cli import hoist_global_options

    assert hoist_global_options(
        ["extract", "claim", "--actor", "hmr-extractor", "--store=/tmp/x"]
    ) == ["--actor", "hmr-extractor", "--store=/tmp/x", "extract", "claim"]
    request = tmp_path / "request.json"
    request.write_text(json.dumps(protocol()))
    store = tmp_path / "store"
    assert main(["run", "create", "--request", str(request), "--store", str(store)]) == 0
    created = json.loads(capsys.readouterr().out)
    assert main(["run", "next", created["run_id"], "--store", str(store), "--actor",
                 "hmr-coordinator"]) == 0
    assert json.loads(capsys.readouterr().out)["profile"] == "hmr-searcher"
