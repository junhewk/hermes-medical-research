"""Regression cases for the failed host workflow, using synthetic evidence only."""

from __future__ import annotations

import csv
import json
from copy import deepcopy

import pytest
from test_research import assessed_workspace

from hermes_medical_search.models import ValidationError
from medical_deep_research_plugin.evidence import REVIEW_CHECKS, review_digest
from medical_deep_research_plugin.packets import documents, next_packet
from medical_deep_research_plugin.validation import validate_stage
from medical_deep_research_plugin.workflow import (
    check,
    current_digests,
    finalize,
    stage_errors,
    submit_batch,
)


def modern_workspace(tmp_path):
    workspace, search = assessed_workspace(tmp_path)
    original = {s: workspace.read(s) for s in workspace.required_stages}
    manifest = workspace.load()
    manifest["evidence_version"] = "2"
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


def record_reviews(workspace):
    workspace.put(
        "reviews",
        {
            "schema_version": "2",
            "records": [
                {
                    "finding_id": f["finding_id"],
                    "review_digest": review_digest(workspace, f),
                    "status": "pass",
                    "checks": {
                        k: {
                            "status": "pass",
                            "rationale": (
                                "Reviewed synthetic claim against its source; methods limitations "
                                "remain explicit."
                            ),
                        }
                        for k in REVIEW_CHECKS
                    },
                }
                for f in workspace.read("synthesis")["findings"]
            ],
        },
    )


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
        "mappings": [{
            "review_study_id": "s0",
            "primary_study_ids": ["s1"],
            "scope": "review",
            "protocol_outcome": None,
            "source_location": {
                "document_id": docs["records"][0]["document_id"],
                "locator": "membership",
                "quote": "This synthetic review includes trial s1.",
            },
        }],
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
        ("source_location", {
            "document_id": w.rows("documents")[1]["document_id"],
            "locator": "abstract", "quote": "Synthetic effect was 2 units at week 12.",
        }, "mapped review"),
        ("source_location", {**original["source_location"], "quote": "Invented membership"},
         "does not occur"),
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


def test_next_packet_is_editable_and_does_not_overwrite_work(tmp_path):
    w = modern_workspace(tmp_path)
    packet = next_packet(w, stage="synthesis")
    from pathlib import Path

    path = Path(packet["input_path"])
    template = json.loads(path.read_text())
    assert template["base_digests"] == current_digests(w)
    assert template["stages"]["synthesis"]["findings"][0]["protocol_outcomes"] == [
        "Synthetic outcome"
    ]
    path.write_text("human work")
    next_packet(w, stage="synthesis")
    assert path.read_text() == "human work"


@pytest.mark.asyncio
async def test_interrupted_export_can_resume(tmp_path, monkeypatch):
    w = modern_workspace(tmp_path)
    import medical_deep_research_plugin.reporting as reporting

    original = reporting.export
    monkeypatch.setattr(
        reporting, "export", lambda _: (_ for _ in ()).throw(OSError("interrupted"))
    )
    with pytest.raises(OSError, match="interrupted"):
        await finalize(w, offline=True)
    assert not (w.path / "completion.json").exists()
    monkeypatch.setattr(reporting, "export", original)
    assert (await finalize(w, offline=True))["completed"]
