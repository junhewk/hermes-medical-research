"""Regression cases for the failed host workflow, using synthetic evidence only."""

from __future__ import annotations

import csv
import json
from copy import deepcopy
from pathlib import Path

import pytest
from test_research import assessed_workspace

from hermes_medical_research.evidence import REVIEW_CHECKS, review_digest
from hermes_medical_research.packets import documents
from hermes_medical_research.search.models import ValidationError
from hermes_medical_research.tasks import Actor, TaskEngine, _empty_ledger
from hermes_medical_research.validation import validate_stage
from hermes_medical_research.workflow import (
    check,
    current_digests,
    finalize,
    stage_errors,
    submit_batch,
)
from hermes_medical_research.workspace import digest


def modern_workspace(tmp_path):
    workspace, search = assessed_workspace(tmp_path)
    original = {s: workspace.read(s) for s in workspace.required_stages}
    manifest = workspace.load()
    manifest["evidence_version"] = "2"
    manifest["run_id"] = "run-" + "a" * 32
    manifest["task_engine"] = _empty_ledger()
    manifest["task_engine"]["authors"] = {
        "screening": ["mdr-selector"],
        "coverage": ["mdr-selector"],
        "studies": ["mdr-extractor"],
        "assessment": ["mdr-extractor"],
        "synthesis": ["mdr-synthesizer"],
    }
    workspace.save(manifest)
    for stage in ("screening", "studies", "extractions", "appraisals", "synthesis"):
        original[stage]["schema_version"] = "2"
    for e in original["extractions"]["records"]:
        e.update(comparator_type="inactive", outcome_type="benefit")
        e["effect"].update(basis="between_group", interval_type="confidence", interval_level=95)
    docs = workspace.rows("documents")
    for a, d in zip(original["appraisals"]["records"], docs, strict=True):
        a.update(completion="limited", overall_judgment="not_assessable")
        for v in a["domains"].values():
            v.update(
                status="unavailable",
                missing_reason="insufficient_detail",
                assessment_basis="Synthetic abstract supplies effects without methods.",
                inspected_locations=[
                    {"document_id": d["document_id"], "locator": d["segments"][0]["locator"]}
                ],
            )
    for f in original["synthesis"]["findings"]:
        f.update(
            protocol_outcomes=["Synthetic outcome"],
            claim_basis="comparative",
            comparator_type="inactive",
            overlap={"status": "not_applicable", "rationale": "Separate synthetic trials."},
        )
        f["certainty"].update(origin="report_assessment", rating="not-assessable")
        for c in f["evidence"]:
            c.update(
                use="direct",
                alignment_rationale="Same synthetic population, comparator, outcome and timepoint.",
            )
    for stage in ("screening", "studies", "extractions", "appraisals"):
        workspace.put(stage, original[stage])
    workspace.put(
        "coverage",
        {
            "schema_version": "2",
            "records": [
                {
                    "record_id": r["record_id"],
                    "selection": "selected",
                    "reason": "Direct synthetic evidence.",
                    "protocol_outcomes": ["Synthetic outcome"],
                }
                for r in workspace.rows("records")
            ],
        },
    )
    workspace.put("synthesis", original["synthesis"])
    record_reviews(workspace)
    return workspace


def test_assessment_packet_exposes_outcome_scope_and_scaffold_contract(tmp_path):
    workspace = modern_workspace(tmp_path)
    record_id = workspace.rows("records")[0]["record_id"]
    study = next(
        row for row in workspace.rows("studies") if record_id in row["record_ids"]
    )

    spec = TaskEngine(workspace)._assessment_spec(record_id, study)

    assert spec["packet_data"]["protocol_outcomes"] == ["Synthetic outcome"]
    assert spec["packet_data"]["coverage"] == workspace.index("coverage")[record_id]
    assert "scaffolds, not a one-row cap" in spec["instructions"]


def record_reviews(workspace):
    from hermes_medical_research import audit

    engine = TaskEngine(workspace)
    coordinator = Actor("mdr-coordinator", "fixture-coordinator", "coordinator")
    auditor = Actor("mdr-auditor", "fixture-auditor", "auditor")
    while True:
        route = engine.route_next(coordinator)
        if route["task_id"] is None:
            assert route["state"] == "ready"
            return
        assert route["role"] == "auditor"
        opened = engine.role_next("audit", route["task_id"], auditor)
        payload = json.loads(Path(opened["proposal_path"]).read_text())
        manifest = workspace.load()
        task = manifest["task_engine"]["tasks"][route["task_id"]]
        packet = workspace.store.read_json(task["packet_file"])
        targets = {
            target["target_id"]: target
            for target in packet["audit_group"]["targets"]
        }
        fallback_source = workspace.rows("extractions")[0]["source_location"]
        extractions = workspace.index("extractions")
        containers = [payload["record"]] if "record" in payload else payload["report_reviews"]
        for container in containers:
            container["status"] = "pass"
            if "checks" in container:
                container["checks"] = {
                    check: {
                        "status": "pass",
                        "rationale": "Checked against the frozen synthetic evidence.",
                    }
                    for check in REVIEW_CHECKS
                }
            for observation in container["observations"]:
                observation.update(
                    verdict="supported",
                    rationale="Fixture source and candidate target checked.",
                )
                target = targets[observation["target_id"]]
                if not target.get("requires_sources"):
                    continue
                if target["kind"] in {"extractions", "appraisals"}:
                    observation["sources"] = [
                        extractions[target["entity_id"]]["source_location"]
                    ]
                else:
                    observation["sources"] = [fallback_source]
        result = audit.validate_task_result(workspace, engine._audit_task(task), payload)
        for row in [*result["records"], *result["report_reviews"]]:
            row["audit_task_id"] = route["task_id"]
        with workspace.lock:
            engine._accept(route["task_id"], auditor, digest(payload), result)


def mapped_review_workspace(tmp_path):
    w = modern_workspace(tmp_path)
    saved = {s: w.read(s) for s in ("extractions", "appraisals", "synthesis")}
    studies = w.read("studies")
    studies["records"][0]["kind"] = "systematic-review"
    w.put("studies", studies)
    docs = w.read("documents")
    docs["records"][0]["segments"].append(
        {"locator": "membership", "text": "This synthetic review includes trial s1."}
    )
    w.put("documents", docs)
    for s in ("extractions", "appraisals"):
        w.put(s, saved[s])
    f = saved["synthesis"]["findings"][0]
    f["overlap"] = {
        "status": "mapped",
        "rationale": "General inclusion is known; outcome-pool membership remains unknown.",
        "mappings": [
            {
                "review_study_id": "s0",
                "primary_study_ids": ["s1"],
                "scope": "review",
                "protocol_outcome": None,
                "source_location": {
                    "document_id": docs["records"][0]["document_id"],
                    "locator": "membership",
                    "quote": "This synthetic review includes trial s1.",
                },
            }
        ],
    }
    return w, saved["synthesis"]


def test_review_membership_cannot_declare_an_outcome_pool(tmp_path):
    w, synthesis = mapped_review_workspace(tmp_path)
    validate_stage(w, "synthesis", synthesis)
    mapping = synthesis["findings"][0]["overlap"]["mappings"][0]
    mapping["protocol_outcome"] = "Synthetic outcome"
    with pytest.raises(ValidationError, match="review-level membership"):
        validate_stage(w, "synthesis", synthesis)
    mapping["scope"] = "outcome"
    mapping["protocol_outcome"] = "Unrelated outcome"
    with pytest.raises(ValidationError, match="protocol_outcome"):
        validate_stage(w, "synthesis", synthesis)


def test_overlap_requires_review_source_and_primary_identities(tmp_path):
    w, synthesis = mapped_review_workspace(tmp_path)
    overlap = synthesis["findings"][0]["overlap"]
    original = deepcopy(overlap["mappings"][0])
    for field, value, error in [
        ("primary_study_ids", ["s0"], "primary studies"),
        ("review_study_id", "s1", "systematic review"),
        (
            "source_location",
            {
                "document_id": w.rows("documents")[1]["document_id"],
                "locator": "abstract",
                "quote": "Synthetic effect was 2 units at week 12.",
            },
            "mapped review",
        ),
        (
            "source_location",
            {**original["source_location"], "quote": "Invented membership"},
            "does not occur",
        ),
    ]:
        overlap["mappings"][0] = {**original, field: value}
        with pytest.raises(ValidationError, match=error):
            validate_stage(w, "synthesis", synthesis)
    overlap["mappings"] = []
    with pytest.raises(ValidationError, match="source-grounded mappings"):
        validate_stage(w, "synthesis", synthesis)


def test_overlap_source_is_bound_to_claim_review(tmp_path):
    w, synthesis = mapped_review_workspace(tmp_path)
    w.put("synthesis", synthesis)
    finding = synthesis["findings"][0]
    old_digest = review_digest(w, finding)
    docs = w.read("documents")
    docs["records"][0]["segments"][-1]["text"] += " Outcome membership is unspecified."
    w.put("documents", docs)
    for s in ("extractions", "appraisals"):
        w.put(s, w.read(s, fresh=False))
    assert review_digest(w, finding) != old_digest


def test_aggregate_check_preserves_mapped_review_and_primary_contributors(tmp_path):
    w, synthesis = mapped_review_workspace(tmp_path)
    assert stage_errors(w, "synthesis", synthesis) == []
    result = submit_batch(w, batch(w, synthesis=synthesis))
    assert result["accepted"]
    assert len(w.read("synthesis")["findings"][0]["evidence"]) == 2


def test_aggregate_check_assesses_certainty_across_the_whole_evidence_body(tmp_path):
    w = modern_workspace(tmp_path)
    appraisals = w.read("appraisals")
    complete = appraisals["records"][0]
    complete.update(completion="complete", overall_judgment="some_concerns")
    loc = w.rows("extractions")[0]["source_location"]
    for domain in complete["domains"].values():
        domain.update(status="assessed", judgment="some_concerns", source_locations=[loc])
    w.put("appraisals", appraisals)
    synthesis = w.read("synthesis", fresh=False)
    synthesis["findings"][0]["certainty"]["rating"] = "low"
    # One completed and one limited appraisal may support a qualified assessed body.
    assert stage_errors(w, "synthesis", synthesis) == []
    assert submit_batch(w, batch(w, synthesis=synthesis))["accepted"]


def batch(workspace, **stages):
    return {"schema_version": "2", "base_digests": current_digests(workspace), "stages": stages}


@pytest.mark.asyncio
async def test_qualified_report_finalizes_and_repeats_with_consistent_exports(tmp_path):
    w = modern_workspace(tmp_path)
    assert check(w)["ready"]
    first = await finalize(w, offline=True)
    assert first["completed"] and first["quality"] == "qualified"
    assert len(first["artifacts"]) == 11
    report = (w.path / "report.md").read_text()
    assert report.index("## Summary of findings") < report.index("## Search methods")
    assert "not-assessable" in report
    with (w.path / "evidence.csv").open() as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["effect_basis"] == "between_group"
    assert rows[0]["effect_value"] == "2"
    assert rows[0]["effect_interval_type"] == "confidence"
    assert w.status()["export"] == "current"
    second = await finalize(w, offline=True)
    assert second["datasets"] == first["datasets"]
    (w.path / "evidence.csv").write_text("corrupted")
    assert w.status()["export"] == "pending_or_stale"


@pytest.mark.parametrize("basis", ["group_summary", "within_group", "ranking"])
def test_group_means_changes_and_rankings_cannot_be_comparative_effects(tmp_path, basis):
    w = modern_workspace(tmp_path)
    es = w.read("extractions")
    es["records"][0]["effect"]["basis"] = basis
    w.put("extractions", es)
    aps = w.read("appraisals", fresh=False)
    w.put("appraisals", aps)
    with pytest.raises(ValidationError, match="between_group"):
        validate_stage(w, "synthesis", w.read("synthesis", fresh=False))


def test_active_comparator_requires_explicit_indirect_alignment(tmp_path):
    w = modern_workspace(tmp_path)
    es = w.read("extractions")
    es["records"][0]["comparator_type"] = "active_other"
    w.put("extractions", es)
    w.put("appraisals", w.read("appraisals", fresh=False))
    synthesis = w.read("synthesis", fresh=False)
    with pytest.raises(ValidationError, match="comparator"):
        validate_stage(w, "synthesis", synthesis)
    synthesis["findings"][0]["evidence"][0].update(use="context", relationship="context")
    w.put("synthesis", synthesis)


def test_unreported_harms_cannot_be_encoded_as_zero_events(tmp_path):
    w = modern_workspace(tmp_path)
    es = w.read("extractions")
    e = es["records"][0]
    e.update(
        outcome_type="harm",
        harms={
            "reporting": "monitored_without_counts",
            "attribution": "Monitoring described; counts unavailable.",
            "arms": [
                {"name": "exercise", "event": "any adverse event", "events": 0, "denominator": 20}
            ],
        },
    )
    with pytest.raises(ValidationError, match="including zero"):
        w.put("extractions", es)


def test_credible_intervals_and_standardised_units_are_not_relabeled(tmp_path):
    w = modern_workspace(tmp_path)
    es = w.read("extractions")
    es["records"][0]["effect"].update(
        interval_type="credible", measure="standardised mean difference", units="mmHg"
    )
    with pytest.raises(ValidationError, match="standardised"):
        w.put("extractions", es)
    es["records"][0]["effect"]["units"] = "SMD"
    w.put("extractions", es)
    assert w.rows("extractions")[0]["effect"]["interval_type"] == "credible"


def test_unassessed_domains_cannot_assert_low_risk_or_moderate_certainty(tmp_path):
    w = modern_workspace(tmp_path)
    aps = w.read("appraisals")
    aps["records"][0]["overall_judgment"] = "low"
    with pytest.raises(ValidationError, match="incomplete appraisal"):
        w.put("appraisals", aps)
    synthesis = w.read("synthesis")
    synthesis["findings"][0]["certainty"]["rating"] = "moderate"
    with pytest.raises(ValidationError, match="no completed"):
        w.put("synthesis", synthesis)


def test_pending_is_distinct_from_diligently_unavailable(tmp_path):
    w = modern_workspace(tmp_path)
    aps = w.read("appraisals")
    aps["records"][0]["completion"] = "pending"
    list(aps["records"][0]["domains"].values())[0]["status"] = "pending"
    w.put("appraisals", aps)
    with pytest.raises(ValidationError, match="substantive contributions"):
        w.put("synthesis", w.read("synthesis", fresh=False))


def test_fulltext_access_failure_requires_real_attempt(tmp_path):
    w = modern_workspace(tmp_path)
    aps = w.read("appraisals")
    list(aps["records"][0]["domains"].values())[0]["missing_reason"] = "access_unavailable"
    with pytest.raises(ValidationError, match="recorded full-text attempt"):
        w.put("appraisals", aps)


def test_batch_is_atomic_and_errors_cover_multiple_records(tmp_path):
    w = modern_workspace(tmp_path)
    before = (w.path / "research.json").read_bytes()
    coverage = w.read("coverage")
    coverage["records"][0]["reason"] = "Reviewed priority."
    es = w.read("extractions")
    for e in es["records"]:
        e["effect"]["value"] = "not numeric"
    update = batch(w, coverage=coverage, extractions=es)
    result = submit_batch(w, update)
    assert not result["accepted"]
    assert len({e["record_id"] for e in result["errors"] if e["stage"] == "extractions"}) == 2
    assert (w.path / "research.json").read_bytes() == before
    assert not check(w, update)["ready"]
    assert (w.path / "research.json").read_bytes() == before


def test_missing_alignments_report_each_contribution(tmp_path):
    w = modern_workspace(tmp_path)
    synthesis = w.read("synthesis")
    for c in synthesis["findings"][0]["evidence"]:
        c.pop("alignment_rationale")
    result = check(w, batch(w, synthesis=synthesis))
    eids = {c["extraction_id"] for c in synthesis["findings"][0]["evidence"]}
    assert all(any(e["field"] == f"evidence.{eid}" for e in result["errors"]) for eid in eids)


def test_old_packet_cannot_overwrite_newer_decisions(tmp_path):
    w = modern_workspace(tmp_path)
    old = batch(w, coverage=w.read("coverage"))
    update = w.read("coverage")
    update["records"][0]["reason"] = "Updated priority after review."
    w.put("coverage", update)
    assert "stale" in submit_batch(w, old)["errors"][0]["message"]


def test_changed_result_requires_explicit_appraisal_resubmission(tmp_path):
    w = modern_workspace(tmp_path)
    es = w.read("extractions")
    es["records"][0]["result"] = "Revised result interpretation."
    aps = w.read("appraisals")
    aps["records"] = aps["records"][1:]
    result = submit_batch(w, batch(w, extractions=es, appraisals=aps))
    assert not result["accepted"]
    assert any("resubmit appraisals" in e["message"] for e in result["errors"])


def test_review_digest_cannot_survive_claim_changes(tmp_path):
    w = modern_workspace(tmp_path)
    old = w.read("reviews")
    synthesis = w.read("synthesis")
    synthesis["findings"][0]["conclusion"] = "Changed claim."
    w.put("synthesis", synthesis)
    assert not check(w)["ready"]
    with pytest.raises(ValidationError, match="review_digest"):
        w.put("reviews", old)
    assert not submit_batch(w, batch(w, synthesis=synthesis, reviews=old))["accepted"]


def test_every_outcome_needs_evidence_or_a_gap(tmp_path):
    w = modern_workspace(tmp_path)
    synthesis = w.read("synthesis")
    synthesis["findings"] = []
    w.put("synthesis", synthesis)
    w.put("reviews", {"schema_version": "2", "records": []})
    assert any("each protocol outcome" in e["message"] for e in check(w)["errors"])


def test_source_lookup_keeps_context_and_exact_text(tmp_path):
    w = modern_workspace(tmp_path)
    rid = w.rows("records")[0]["record_id"]
    result = documents(w, record_id=rid, query="2 UNITS")
    assert result["total"] == 1
    assert result["segments"][0]["text"] == "Synthetic effect was 2 units at week 12."
    assert result["segments"][0]["locator"] == "abstract"


def test_titles_are_citable_without_rewriting_evidence_stages(tmp_path):
    w = modern_workspace(tmp_path)
    from hermes_medical_research.validation import validate_location

    before_manifest = w.load()
    before_documents = w.read("documents")
    projected = w.source_documents()
    assert w.source_documents() == projected
    assert len([row for row in projected if row["kind"] == "metadata"]) == len(
        w.rows("records")
    )
    record = w.rows("records")[0]
    location = {
        "document_id": f"{record['record_id']}:metadata",
        "locator": "title",
        "quote": record["title"],
    }
    assert validate_location(w, location, record["record_id"])["kind"] == "metadata"
    extraction = w.read("extractions")
    extraction["records"][0]["source_location"] = location
    with pytest.raises(ValidationError, match="metadata alone cannot support a result"):
        w.put("extractions", extraction)
    assert w.read("documents") == before_documents
    assert w.load()["datasets"] == before_manifest["datasets"]


def test_task_proposal_is_digest_bound_and_not_overwritten(tmp_path):
    w = modern_workspace(tmp_path)
    manifest = w.load()
    accepted = [
        task
        for task in manifest["task_engine"]["tasks"].values()
        if task["kind"] == "audit" and task["state"] == "accepted"
    ]
    assert accepted
    proposal = w.store.read_json(accepted[0]["proposal_file"])
    assert digest(proposal) == accepted[0]["proposal_digest"]
    result = w.store.read_json(accepted[0]["result_file"])
    assert digest(result) == accepted[0]["result_digest"]


@pytest.mark.asyncio
async def test_interrupted_export_can_resume(tmp_path, monkeypatch):
    w = modern_workspace(tmp_path)
    import hermes_medical_research.reporting as reporting

    original = reporting.export
    monkeypatch.setattr(
        reporting, "export", lambda _: (_ for _ in ()).throw(OSError("interrupted"))
    )
    with pytest.raises(OSError, match="interrupted"):
        await finalize(w, offline=True)
    assert not (w.path / "completion.json").exists()
    monkeypatch.setattr(reporting, "export", original)
    assert (await finalize(w, offline=True))["completed"]
