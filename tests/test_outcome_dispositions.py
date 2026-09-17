"""Per-outcome assessment completeness for a synthetic trial reporting one of three outcomes."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest
from test_evidence_workflow import batch, modern_workspace, record_reviews
from test_research import completed_search, protocol

from hermes_medical_research import audit
from hermes_medical_research.evidence import validate_contribution_v2
from hermes_medical_research.fulltext import parse_jats
from hermes_medical_research.search.models import ValidationError
from hermes_medical_research.tasks import (
    TASK_PACKET_LIMIT,
    Actor,
    RunCatalog,
    TaskEngine,
)
from hermes_medical_research.validation import GRADE_DOMAINS
from hermes_medical_research.workflow import check, finalize, submit_batch

OUTCOMES = ["Theoretical exam score", "Clinical skills (Mini-CEX)", "Learner satisfaction"]
COORDINATOR = Actor("mdr-coordinator", "coordinator-session", "coordinator")
EXTRACTOR = Actor("mdr-extractor", "extractor-session", "extractor")
JATS = b"""<article><body>
<sec><title>Methods</title>
<p>Eighty students were randomized to ChatGPT-assisted or traditional teaching.</p></sec>
<sec><title>Results</title>
<p>The ChatGPT-assisted group scored 85.38 points on the theoretical exam versus 80.30 points
in the traditional group.</p>
<table-wrap><label>Table 1</label><caption>Theoretical exam scores by group</caption>
<table><tr><td>Group</td><td>Score</td></tr><tr><td>ChatGPT</td><td>85.38</td></tr></table>
</table-wrap></sec></body></article>"""


def contract_run(tmp_path: Path, outcomes: list[str] = OUTCOMES):
    request = protocol()
    request["outcomes"] = outcomes
    catalog = RunCatalog(tmp_path / "store")
    created = catalog.create(request, records=1, fulltexts=1)
    workspace = catalog.workspace(created["run_id"])
    search, _ = completed_search(workspace, tmp_path / "search", count=1)
    workspace.attach(search.path)
    record_id = workspace.rows("records")[0]["record_id"]
    workspace.put(
        "screening",
        {
            "schema_version": "2",
            "records": [
                {
                    "record_id": record_id,
                    "decision": "include",
                    "basis": "title-abstract",
                    "reason": "Synthetic eligibility.",
                }
            ],
        },
    )
    workspace.put(
        "coverage",
        {
            "schema_version": "2",
            "records": [
                {
                    "record_id": record_id,
                    "selection": "selected",
                    "reason": "Synthetic trial.",
                    "protocol_outcomes": [outcomes[0]],
                }
            ],
        },
    )
    checksum = hashlib.sha256(JATS).hexdigest()
    relative = f"fulltext/{checksum}.xml"
    (workspace.path / relative).parent.mkdir(exist_ok=True)
    (workspace.path / relative).write_bytes(JATS)
    document_id = f"{record_id}:fulltext:{checksum[:16]}"
    documents = workspace.rows("documents")
    documents.append(
        {
            "document_id": document_id,
            "record_id": record_id,
            "kind": "fulltext",
            "file": relative,
            "sha256": checksum,
            "segments": parse_jats(JATS),
        }
    )
    workspace.put("documents", {"schema_version": "1", "records": documents}, validate=False)
    manifest = workspace.load()
    manifest["fulltext_attempts"][record_id] = {
        "status": "available",
        "document_id": document_id,
        "completed_at": "2026-09-17T00:00:00+00:00",
    }
    workspace.save(manifest)
    workspace.put(
        "studies",
        {
            "schema_version": "2",
            "records": [
                {
                    "study_id": "study-" + record_id,
                    "record_ids": [record_id],
                    "kind": "primary",
                    "basis": "Single synthetic trial.",
                }
            ],
        },
    )
    return TaskEngine(workspace), record_id, document_id


def open_assessment(engine: TaskEngine):
    route = engine.route_next(COORDINATOR)
    task = engine.workspace.load()["task_engine"]["tasks"][route["task_id"]]
    assert task["kind"] == "assessment"
    opened = engine.role_next("extract", route["task_id"], EXTRACTOR)
    packet = json.loads(Path(opened["packet_path"]).read_text())
    proposal_path = Path(opened["proposal_path"])
    return route["task_id"], opened, packet, proposal_path, json.loads(proposal_path.read_text())


def fill_exam(proposal: dict, document_id: str, outcome: str = OUTCOMES[0]) -> str:
    row = next(
        item
        for item in proposal["stages"]["extractions"]["records"]
        if item.get("protocol_outcome") == OUTCOMES[0]
    )
    row.update(
        protocol_outcome=outcome,
        population="Undergraduate medical students",
        comparison="ChatGPT-assisted versus traditional teaching",
        outcome="Theoretical exam score (points)",
        timepoint="End of course",
        comparator_type="active_other",
        outcome_type="benefit",
        sample_size=80,
        result="85.38 versus 80.30 points",
        effect={
            "measure": "mean difference",
            "basis": "between_group",
            "value": 5.08,
            "ci_low": None,
            "ci_high": None,
            "units": "points",
            "interval_type": "none",
            "interval_level": None,
            "missing_reason": "",
        },
        favors="intervention",
        direction_rationale="Higher exam scores are better.",
        source_location={
            "document_id": document_id,
            "locator": "paragraph:2",
            "quote": "scored 85.38 points on the theoretical exam versus 80.30 points",
        },
        support_checked=True,
        support_rationale="The quote reports both group means.",
    )
    appraisal = next(
        item
        for item in proposal["stages"]["appraisals"]["records"]
        if item["extraction_id"] == row["extraction_id"]
    )
    appraisal.update(
        completion="limited",
        overall_judgment="not_assessable",
        overall="Methods lack detail.",
        rationale="Synthetic methods paragraph is brief.",
    )
    for domain in appraisal["domains"].values():
        domain.update(
            status="unavailable",
            judgment="not_assessed",
            rationale="Not described.",
            missing_reason="insufficient_detail",
            assessment_basis="The methods paragraph gives no detail for this domain.",
            inspected_locations=[{"document_id": document_id, "locator": "paragraph:1"}],
        )
    return row["extraction_id"]


def decide(proposal: dict, outcome: str, status: str, document_id: str | None = None) -> None:
    row = proposal["stages"]["dispositions"]["records"][0]
    item = next(entry for entry in row["outcomes"] if entry["protocol_outcome"] == outcome)
    item["status"] = status
    if status != "extracted":
        item["rationale"] = "The results section and table report only exam scores."
        item["inspected_locations"] = [
            {"document_id": document_id, "locator": "paragraph:2"},
            {"document_id": document_id, "locator": "table:1"},
        ]


def test_assessment_packet_lists_every_protocol_outcome_with_locations(tmp_path):
    engine, record_id, document_id = contract_run(tmp_path)
    _, opened, packet, _, proposal = open_assessment(engine)

    checklist = packet["outcome_checklist"]
    assert [entry["protocol_outcome"] for entry in checklist] == OUTCOMES
    assert [entry["selector_flagged"] for entry in checklist] == [True, False, False]
    exam_locations = {item["locator"] for item in checklist[0]["likely_locations"]}
    assert exam_locations & {"paragraph:2", "table:1"}
    assert all(len(entry["likely_locations"]) <= 3 for entry in checklist)
    assert [row["extraction_id"] for row in proposal["stages"]["extractions"]["records"]] == [
        f"result-{record_id}-o{index}" for index in (1, 2, 3)
    ]
    disposition = proposal["stages"]["dispositions"]["records"][0]
    assert [item["protocol_outcome"] for item in disposition["outcomes"]] == OUTCOMES
    assert len(Path(opened["packet_path"]).read_bytes()) < TASK_PACKET_LIMIT
    assert "source find" in opened["source_find"]
    assert "source read" in opened["source_read"]
    assert "Submission is rejected while any outcome is undecided" in packet["instructions"]
    rules = packet["field_rules"]
    assert "between_group" in rules["extraction"]["effect.basis"]
    assert rules["dispositions"]["outcomes[].status"] == [
        "extracted",
        "not_reported",
        "not_applicable",
    ]


@pytest.mark.asyncio
async def test_submission_rejects_an_undecided_outcome(tmp_path):
    engine, _, document_id = contract_run(tmp_path)
    task_id, _, _, path, proposal = open_assessment(engine)
    fill_exam(proposal, document_id)
    decide(proposal, OUTCOMES[0], "extracted")
    decide(proposal, OUTCOMES[1], "not_reported", document_id)
    path.write_text(json.dumps(proposal))

    with pytest.raises(ValidationError, match="'Learner satisfaction' is undecided"):
        await engine.submit("extract", task_id, path, EXTRACTOR)
    assert engine.workspace.load()["task_engine"]["tasks"][task_id]["state"] == "in_progress"


@pytest.mark.asyncio
async def test_dispositions_are_accepted_and_untouched_scaffolds_pruned(tmp_path):
    engine, record_id, document_id = contract_run(tmp_path)
    task_id, _, _, path, proposal = open_assessment(engine)
    extraction_id = fill_exam(proposal, document_id)
    decide(proposal, OUTCOMES[0], "extracted")
    decide(proposal, OUTCOMES[1], "not_reported", document_id)
    decide(proposal, OUTCOMES[2], "not_reported", document_id)
    path.write_text(json.dumps(proposal))

    receipt = await engine.submit("extract", task_id, path, EXTRACTOR)
    assert receipt["state"] == "accepted"
    workspace = engine.workspace
    assert [row["extraction_id"] for row in workspace.rows("extractions")] == [extraction_id]
    assert set(workspace.index("appraisals")) == {extraction_id}
    stored = workspace.index("dispositions")[record_id]["outcomes"]
    assert stored[0]["extraction_ids"] == [extraction_id]
    assert stored[1]["extraction_ids"] == stored[2]["extraction_ids"] == []
    task = workspace.load()["task_engine"]["tasks"][task_id]
    result = workspace.store.read_json(task["result_file"])
    assert result["pruned_scaffolds"] == [f"result-{record_id}-o2", f"result-{record_id}-o3"]
    assert engine.route_next(COORDINATOR)["role"] == "synthesizer"


@pytest.mark.asyncio
async def test_extraction_bound_to_an_unreported_outcome_is_rejected(tmp_path):
    engine, _, document_id = contract_run(tmp_path)
    task_id, _, _, path, proposal = open_assessment(engine)
    extraction_id = fill_exam(proposal, document_id, outcome=OUTCOMES[1])
    decide(proposal, OUTCOMES[0], "extracted")
    decide(proposal, OUTCOMES[1], "not_reported", document_id)
    decide(proposal, OUTCOMES[2], "not_reported", document_id)
    path.write_text(json.dumps(proposal))

    with pytest.raises(ValidationError) as raised:
        await engine.submit("extract", task_id, path, EXTRACTOR)
    message = str(raised.value)
    assert "'Theoretical exam score' is extracted but no filled extraction row" in message
    assert f"rows are bound to it: {extraction_id}" in message


@pytest.mark.asyncio
async def test_all_outcomes_unreported_finishes_without_extractions(tmp_path):
    engine, record_id, document_id = contract_run(tmp_path)
    task_id, _, _, path, proposal = open_assessment(engine)
    for outcome in OUTCOMES:
        decide(proposal, outcome, "not_applicable", document_id)
    path.write_text(json.dumps(proposal))

    assert (await engine.submit("extract", task_id, path, EXTRACTOR))["state"] == "accepted"
    assert engine.workspace.rows("extractions") == []
    assert record_id in engine.workspace.index("dispositions")
    assert engine.route_next(COORDINATOR)["role"] == "synthesizer"


@pytest.mark.asyncio
async def test_synthesis_links_rows_by_protocol_outcome_and_lists_gaps(tmp_path):
    engine, record_id, document_id = contract_run(tmp_path)
    task_id, _, _, path, proposal = open_assessment(engine)
    extraction_id = fill_exam(proposal, document_id)
    decide(proposal, OUTCOMES[0], "extracted")
    decide(proposal, OUTCOMES[1], "not_reported", document_id)
    decide(proposal, OUTCOMES[2], "not_reported", document_id)
    path.write_text(json.dumps(proposal))
    await engine.submit("extract", task_id, path, EXTRACTOR)

    exam = engine._synthesis_spec(OUTCOMES[0])
    assert "comparative" in exam["packet_data"]["field_rules"]["finding.claim_basis"]
    assert [row["extraction_id"] for row in exam["packet_data"]["extractions"]] == [extraction_id]
    assert exam["packet_data"]["unreported_dispositions"] == []
    skills = engine._synthesis_spec(OUTCOMES[1])
    assert skills["packet_data"]["extractions"] == []
    assert [item["record_id"] for item in skills["packet_data"]["unreported_dispositions"]] == [
        record_id
    ]
    assert f"disposition:{record_id}" in skills["allowed_source_ids"]

    extraction = engine.workspace.index("extractions")[extraction_id]
    finding = {"protocol_outcomes": [OUTCOMES[1]], "claim_basis": "comparative"}
    contribution = {
        "extraction_id": extraction_id,
        "use": "direct",
        "relationship": "supports",
        "alignment_rationale": "Different outcome.",
    }
    finding["comparator_type"] = extraction["comparator_type"]
    with pytest.raises(ValidationError, match="bound to one of the finding's protocol outcomes"):
        validate_contribution_v2(engine.workspace, finding, contribution)


def test_synthesis_packet_degrades_to_fit_many_records(tmp_path, monkeypatch):
    engine, record_id, _ = contract_run(tmp_path)
    workspace = engine.workspace
    long_text = "x" * 2000
    rows = [
        {
            "extraction_id": f"e{index}",
            "record_id": record_id,
            "protocol_outcome": OUTCOMES[0],
            "population": long_text,
            "comparison": "c",
            "outcome": "o",
            "timepoint": "t",
            "effect": {"measure": "mean difference", "value": 1},
            "result": long_text,
            "source_location": {"document_id": "d", "locator": "l", "quote": long_text},
        }
        for index in range(20)
    ]
    decisions = [
        {
            "record_id": f"r-{index:020d}",
            "outcomes": [
                {"protocol_outcome": OUTCOMES[0], "status": "not_reported", "rationale": long_text}
            ],
        }
        for index in range(60)
    ]
    original_rows = workspace.rows

    def rows_for(stage, **kwargs):
        if stage == "extractions":
            return [{**row, "population": "p"} for row in rows]
        if stage == "dispositions":
            return decisions
        return original_rows(stage, **kwargs)

    monkeypatch.setattr(workspace, "rows", rows_for)
    manifest = workspace.load()
    manifest["datasets"]["dispositions"] = {"digest": "fixture"}
    monkeypatch.setattr(workspace, "load", lambda: manifest)

    spec = engine._synthesis_spec(OUTCOMES[0])
    size = len(json.dumps(spec["packet_data"]).encode())
    assert size <= TASK_PACKET_LIMIT - 4 * 1024
    assert "rationale" not in spec["packet_data"]["unreported_dispositions"][0]
    assert spec["packet_data"]["extractions"][0]["result_truncated"] is True


@pytest.mark.asyncio
async def test_missing_disposition_reopens_the_record_assessment(tmp_path):
    engine, record_id, document_id = contract_run(tmp_path)
    _, _, _, _, proposal = open_assessment(engine)
    extraction_id = fill_exam(proposal, document_id)
    rows = proposal["stages"]["extractions"]["records"]
    appraisals = proposal["stages"]["appraisals"]["records"]
    engine.workspace.put(
        "extractions",
        {
            "schema_version": "2",
            "records": [r for r in rows if r["extraction_id"] == extraction_id],
        },
    )
    engine.workspace.put(
        "appraisals",
        {
            "schema_version": "2",
            "records": [a for a in appraisals if a["extraction_id"] == extraction_id],
        },
    )

    _, _, _, _, reopened = open_assessment(engine)
    assert [row["extraction_id"] for row in reopened["stages"]["extractions"]["records"]] == [
        extraction_id,
        f"result-{record_id}-o2",
        f"result-{record_id}-o3",
    ]
    assert all(
        item["status"] == ""
        for item in reopened["stages"]["dispositions"]["records"][0]["outcomes"]
    )


def test_source_find_and_read_are_bounded_like_source_show(tmp_path):
    engine, _, document_id = contract_run(tmp_path)
    task_id, _, _, _, _ = open_assessment(engine)

    found = engine.source_find(task_id, "theoretical exam", EXTRACTOR)
    assert found["hits"][0]["document_id"] == document_id
    assert found["hits"][0]["locator"] in {"paragraph:2", "table:1"}
    read = engine.source_read(task_id, document_id, "table:1", 1, EXTRACTOR)
    assert "85.38" in read["text"]
    assert read["previous_locator"] == "paragraph:2"
    with pytest.raises(ValidationError, match="unknown locator"):
        engine.source_read(task_id, document_id, "table:9", 1, EXTRACTOR)
    with pytest.raises(ValidationError, match="bounded corpus"):
        engine.source_read(task_id, "r-outside:fulltext:0", "table:1", 1, EXTRACTOR)
    with pytest.raises(ValidationError, match="three or more characters"):
        engine.source_find(task_id, "a b", EXTRACTOR)
    with pytest.raises(ValidationError, match="active task"):
        engine.source_find(task_id, "exam", Actor("mdr-selector", "other", "selector"))


def test_legacy_run_keeps_validating_with_an_explicit_warning(tmp_path):
    workspace = modern_workspace(tmp_path)
    checked = check(workspace)
    assert checked["ready"]
    assert any("predates outcome dispositions" in item for item in checked["warnings"])
    record_id = workspace.rows("records")[0]["record_id"]
    update = batch(
        workspace,
        dispositions={
            "schema_version": "2",
            "records": [{"record_id": record_id, "study_id": "s0", "outcomes": []}],
        },
    )
    result = submit_batch(workspace, update)
    assert not result["accepted"]
    assert any("predates it" in error["message"] for error in result["errors"])


def gap_synthesis(outcomes: list[str]) -> dict:
    return {
        "schema_version": "2",
        "title": "Synthetic outcome-complete report",
        "limitations": ["Synthetic fixture; no clinical interpretation."],
        "findings": [
            {
                "finding_id": f"finding-{index}",
                "protocol_outcomes": [outcome],
                "population": "Undergraduate medical students",
                "comparison": "ChatGPT-assisted versus traditional teaching",
                "outcome": outcome,
                "timepoint": "End of course",
                "claim_basis": "gap",
                "comparator_type": "active_other",
                "conclusion": "Synthetic evidence gap.",
                "gap_reason": "Fixture only.",
                "gap_basis": "Fixture only.",
                "evidence": [],
                "overlap": {"status": "not_applicable", "rationale": "Single trial."},
                "certainty": {
                    "origin": "report_assessment",
                    "framework": "GRADE-informed",
                    "rating": "not-assessable",
                    "rationale": "No contributing evidence.",
                    "starting_point": "Randomized trials start high.",
                    "rating_explanation": "No evidence body to rate.",
                    "domains": {key: "Not assessable." for key in GRADE_DOMAINS},
                },
            }
            for index, outcome in enumerate(outcomes, start=1)
        ],
    }


@pytest.mark.asyncio
async def test_outcome_contract_run_audits_and_exports_dispositions(tmp_path, monkeypatch):
    # Three findings also guard the aggregate audit-receipt check, which once rejected every
    # report with more than one finding during final readiness checks.
    engine, record_id, document_id = contract_run(tmp_path)
    task_id, _, _, path, proposal = open_assessment(engine)
    fill_exam(proposal, document_id)
    decide(proposal, OUTCOMES[0], "extracted")
    decide(proposal, OUTCOMES[1], "not_reported", document_id)
    decide(proposal, OUTCOMES[2], "not_reported", document_id)
    path.write_text(json.dumps(proposal))
    await engine.submit("extract", task_id, path, EXTRACTOR)
    workspace = engine.workspace
    workspace.put("synthesis", gap_synthesis(OUTCOMES))

    report_targets, _, _ = audit.build_review_targets(audit.candidate(workspace))
    targets = [row for row in report_targets if row["kind"] == "dispositions"]
    assert [row["entity_id"] for row in targets] == [record_id]
    assert document_id in targets[0]["allowed_document_ids"]
    assert targets[0]["allow_metadata_only"] is False

    accepted_task = workspace.load()["task_engine"]["tasks"][task_id]
    target = {"kind": "dispositions", "entity_id": record_id}
    monkeypatch.setattr(
        engine, "_audit_task", lambda _task: {"audit_group": {"targets": [target]}}
    )
    correction = engine._correction_spec(
        {"group_id": "report:fixture", "audit_task_ids": [accepted_task["task_id"]]}
    )
    assert correction["kind"] == "assessment" and correction["target_ids"] == [record_id]
    monkeypatch.undo()

    record_reviews(workspace)
    checked = check(workspace)
    assert checked["ready"], checked["errors"]
    assert not any("predates" in warning for warning in checked["warnings"])
    completed = await finalize(workspace, offline=True)
    assert completed["completed"]
    with (workspace.path / "dispositions.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    assert [(row["protocol_outcome"], row["status"]) for row in rows] == [
        (OUTCOMES[0], "extracted"),
        (OUTCOMES[1], "not_reported"),
        (OUTCOMES[2], "not_reported"),
    ]
    assert workspace.store.read_json("report.json")["dispositions"][0]["record_id"] == record_id
    report = (workspace.path / "report.md").read_text()
    assert "### Outcome decisions per assessed record" in report
    assert "| Clinical skills (Mini-CEX) | 0 | 1 | 0 |" in report
