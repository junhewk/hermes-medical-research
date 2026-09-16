"""Synthetic evidence fixtures exercise provenance and export gates, never clinical claims."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path

import httpx
import pytest

from hermes_medical_research.citations import plan_snowball
from hermes_medical_research.fulltext import fetch_fulltexts, parse_jats, public_url
from hermes_medical_research.reporting import export
from hermes_medical_research.search.artifacts import RunStore
from hermes_medical_research.search.config import Credentials
from hermes_medical_research.search.http import HttpSession
from hermes_medical_research.search.models import Question, ValidationError
from hermes_medical_research.search.query import compile_strategy
from hermes_medical_research.validation import GRADE_DOMAINS, METHODS, validate_complete
from hermes_medical_research.verification import verify
from hermes_medical_research.workspace import Workspace


def protocol():
    return {
        "schema_version": "3",
        "framework": "PICO",
        "question": "Synthetic treatment question",
        "components": {
            "population": {"groups": [{"label": "population", "text": "adults"}]},
            "intervention": {"groups": [{"label": "intervention", "text": "exercise"}]},
        },
        "sources": ["europe-pmc"],
        "eligibility": {"include": ["Synthetic adults"], "exclude": ["Animals"]},
        "outcomes": ["Synthetic outcome"],
        "search_rationale": "Sensitive population/intervention search.",
    }


def workspace_at(path: Path, *, records=100, fulltexts=30, mode="report"):
    workspace = Workspace(path)
    workspace.init(
        protocol(),
        mode=mode,
        records=records,
        fulltexts=fulltexts,
        language="en",
        evidence_version="1",
    )
    return workspace


def test_report_protocol_requires_a_biomedical_index(tmp_path):
    request = protocol()
    request["sources"] = ["openalex"]
    with pytest.raises(ValidationError, match="PubMed or Europe PMC"):
        Workspace(tmp_path / "research").init(
            request,
            mode="report",
            records=10,
            fulltexts=1,
            language="en",
            evidence_version="2",
        )


def completed_search(workspace, path, *, count=2, title="Synthetic trial", mode="quick"):
    question = Question.from_dict(workspace.load()["protocol"]["question"])
    strategy = compile_strategy(question, mode=mode, limit_per_source=count, sources=["europe-pmc"])
    store = RunStore(path)
    manifest = store.initialize(question, strategy, Credentials())
    records = [
        {
            "source": "europe-pmc",
            "source_id": f"MED:{i}",
            "pmid": str(i),
            "title": f"{title} {i}",
            "abstract": "Synthetic effect was 2 units at week 12.",
            "year": "2025",
            "authors": ["Fixture Author"],
            "publication_types": ["Randomized trial"],
            "retrieved_at": "2026-09-12T00:00:00Z",
            "url": f"https://example.org/fixture/{i}",
        }
        for i in range(1, count + 1)
    ]
    store.append_source("europe-pmc", records)
    manifest["status"] = "complete"
    manifest["sources"]["europe-pmc"].update(
        status="complete", retrieved=count, retained=count, reported_total=count
    )
    store.write_manifest(manifest)
    return store, strategy


def test_attach_preserves_relevance_ranking_for_screening_order(tmp_path):
    workspace = workspace_at(tmp_path / "research")
    question = Question.from_dict(workspace.load()["protocol"]["question"])
    strategy = compile_strategy(
        question, mode="quick", limit_per_source=2, sources=["europe-pmc"]
    )
    store = RunStore(tmp_path / "search")
    manifest = store.initialize(question, strategy, Credentials())
    store.append_source(
        "europe-pmc",
        [
            {
                "source": "europe-pmc",
                "source_id": "MED:1",
                "pmid": "1",
                "title": "Unrelated laboratory methods report",
                "abstract": "A technical validation study.",
                "year": "2025",
                "publication_types": ["Randomized trial"],
                "retrieved_at": "2026-09-12T00:00:00Z",
                "url": "https://example.org/irrelevant",
            },
            {
                "source": "europe-pmc",
                "source_id": "MED:2",
                "pmid": "2",
                "title": "Exercise intervention for adults",
                "abstract": "Exercise was compared in adults.",
                "year": "2025",
                "publication_types": ["Randomized trial"],
                "retrieved_at": "2026-09-12T00:00:00Z",
                "url": "https://example.org/relevant",
            },
        ],
    )
    manifest["status"] = "complete"
    manifest["sources"]["europe-pmc"].update(
        status="complete", retrieved=2, retained=2, reported_total=2
    )
    store.write_manifest(manifest)

    workspace.attach(store.path)

    assert [row["pmid"] for row in workspace.rows("records")] == ["2", "1"]


def stage(workspace, name, records):
    workspace.put(name, {"schema_version": "1", "records": records})


def assessed_workspace(tmp_path, *, mixed_access=False):
    workspace = workspace_at(tmp_path / "research")
    search, _ = completed_search(workspace, tmp_path / "search", title="<script>alert(1)</script>")
    workspace.attach(search.path)
    records = workspace.rows("records")
    stage(
        workspace,
        "screening",
        [
            {
                "record_id": r["record_id"],
                "decision": "include",
                "basis": "title-abstract",
                "reason": "Synthetic eligibility",
            }
            for r in records
        ],
    )
    stage(
        workspace,
        "studies",
        [
            {
                "study_id": f"s{i}",
                "record_ids": [r["record_id"]],
                "kind": "primary",
                "basis": "Separate synthetic trials",
            }
            for i, r in enumerate(records)
        ],
    )
    if mixed_access:
        raw = b"<article><body><p>Synthetic effect was 2 units at week 12.</p></body></article>"
        checksum = hashlib.sha256(raw).hexdigest()
        relative = f"fulltext/{checksum}.xml"
        (workspace.path / relative).parent.mkdir(exist_ok=True)
        (workspace.path / relative).write_bytes(raw)
        docs = workspace.rows("documents")
        docs.append(
            {
                "document_id": "fulltext-fixture",
                "record_id": records[0]["record_id"],
                "kind": "fulltext",
                "file": relative,
                "sha256": checksum,
                "segments": parse_jats(raw),
            }
        )
        stage(workspace, "documents", docs)
    extractions = []
    for i, record in enumerate(records):
        extractions.append(
            {
                "extraction_id": f"e{i}",
                "record_id": record["record_id"],
                "study_id": f"s{i}",
                "population": "Synthetic adults",
                "comparison": "Exercise versus control",
                "outcome": "Synthetic outcome",
                "timepoint": "12 weeks",
                "result": "=Synthetic result",
                "sample_size": 20,
                "effect": {
                    "measure": "mean difference",
                    "value": 2,
                    "ci_low": -1,
                    "ci_high": 5,
                    "units": "synthetic units",
                },
                "favors": "uncertain",
                "direction_rationale": "Interval includes no difference.",
                "support_checked": True,
                "support_rationale": "Synthetic result matches the fixture.",
                "source_location": {
                    "document_id": "fulltext-fixture"
                    if mixed_access and i == 0
                    else record["record_id"] + ":abstract",
                    "locator": "paragraph:1" if mixed_access and i == 0 else "abstract",
                    "quote": "Synthetic effect was 2 units at week 12.",
                },
            }
        )
    stage(workspace, "extractions", extractions)
    stage(
        workspace,
        "appraisals",
        [
            {
                "extraction_id": e["extraction_id"],
                "method": "rob2",
                "method_version": "2019-08-22",
                "overall": "not_assessed",
                "rationale": "Synthetic fixture lacks methods.",
                "domains": {
                    key: {"judgment": "not_assessed", "rationale": "Methods unavailable."}
                    for key in METHODS["rob2"][1].split()
                },
            }
            for e in extractions
        ],
    )
    workspace.put(
        "synthesis",
        {
            "schema_version": "1",
            "title": "Synthetic report <script>alert(1)</script>",
            "limitations": ["Synthetic fixture; no clinical interpretation."],
            "findings": [
                {
                    "finding_id": "f1",
                    "population": "Synthetic adults",
                    "comparison": "Exercise versus control",
                    "outcome": "Synthetic outcome",
                    "timepoint": "12 weeks",
                    "conclusion": "Uncertain synthetic result.",
                    "evidence": [
                        {
                            "extraction_id": e["extraction_id"],
                            "relationship": "supports" if i == 0 else "contradicts",
                            "weight_rationale": "Limited synthetic result.",
                            "claim_support_checked": True,
                        }
                        for i, e in enumerate(extractions)
                    ],
                    "certainty": {
                        "framework": "GRADE-informed",
                        "rating": "very-low",
                        "rationale": "Fixture methods unavailable.",
                        "starting_point": "High for randomized trials",
                        "rating_explanation": "Downgraded for unavailable methods and imprecision.",
                        "domains": {
                            key: "Synthetic rationale; human review needed."
                            for key in GRADE_DOMAINS
                        },
                    },
                }
            ],
        },
    )
    return workspace, search


@pytest.mark.asyncio
async def test_complete_export_preserves_mixed_access_and_contradictions(tmp_path):
    workspace, search = assessed_workspace(tmp_path, mixed_access=True)
    checked = await verify(workspace, offline=True)
    assert checked["ready"]
    assert {v["status"] for v in checked["identity"].values()} == {"unverified"}
    result = export(workspace)
    assert len(result["artifacts"]) == 8
    report = (workspace.path / "report.md").read_text()
    page = (workspace.path / "report.html").read_text()
    assert "contradicts; abstract evidence" in report
    assert "supports; fulltext evidence" in report
    assert '<h3 id="reference-1">' in page and 'href="#reference-1"' in page
    assert "<script>" not in page and "&lt;script&gt;" in page
    evidence = list(csv.DictReader(io.StringIO((workspace.path / "evidence.csv").read_text())))
    assert evidence[0]["result"].startswith("'=Synthetic")
    assert {e["access"] for e in evidence} == {"abstract", "fulltext"}
    assert (workspace.path / "references.ris").read_text().count("TY  - JOUR") == 2
    assert (
        json.loads((workspace.path / "selection-counts.json").read_text())["duplicates_removed"]
        == 0
    )
    before = {name: value["digest"] for name, value in workspace.load()["datasets"].items()}
    workspace.attach(search.path)
    assert {name: value["digest"] for name, value in workspace.load()["datasets"].items()} == before
    export(workspace)  # Reattachment is idempotent, including verification freshness.


def test_upstream_change_invalidates_transitive_assessments(tmp_path):
    workspace, _ = assessed_workspace(tmp_path)
    docs = workspace.rows("documents")
    docs[0]["segments"][0]["text"] += " Additional methods."
    stage(workspace, "documents", docs)
    assert workspace.status()["stages"]["screening"] == "recorded"
    assert workspace.status()["stages"]["extractions"] == "stale"
    assert workspace.status()["stages"]["synthesis"] == "stale"
    with pytest.raises(ValidationError, match="stale"):
        export(workspace)


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda e: e["source_location"].update(quote="Invented quote"), "does not occur"),
        (lambda e: e["effect"].update(ci_low=6), "reversed"),
        (lambda e: e["effect"].update(value=float("nan")), "finite"),
        (lambda e: e.update(support_checked=False), "host must check"),
        (lambda e: e.update(record_id="unknown"), "unknown record_id"),
        (lambda e: e.update(favors=[]), "favors must"),
    ],
)
def test_invalid_extractions_rejected_without_replacing_revision(tmp_path, mutation, match):
    workspace, _ = assessed_workspace(tmp_path)
    before = workspace.load()["datasets"]["extractions"]["digest"]
    rows = workspace.rows("extractions")
    mutation(rows[0])
    with pytest.raises(ValidationError, match=match):
        stage(workspace, "extractions", rows)
    assert workspace.load()["datasets"]["extractions"]["digest"] == before


@pytest.mark.parametrize("bad", [[], "wrong", None])
def test_bad_synthesis_nested_objects_are_validation_errors(tmp_path, bad):
    workspace, _ = assessed_workspace(tmp_path)
    value = workspace.read("synthesis")
    value["findings"] = [bad]
    with pytest.raises(ValidationError, match="finding must"):
        workspace.put("synthesis", value)


def test_alignment_appraisal_and_fulltext_integrity_gates(tmp_path):
    workspace, _ = assessed_workspace(tmp_path, mixed_access=True)
    value = workspace.read("synthesis")
    value["findings"][0]["timepoint"] = "1 year"
    with pytest.raises(ValidationError, match="alignment_rationale"):
        workspace.put("synthesis", value)
    appraisals = workspace.rows("appraisals")
    appraisals[0]["domains"]["randomization"]["judgment"] = "low"
    with pytest.raises(ValidationError, match="source locations"):
        stage(workspace, "appraisals", appraisals)
    doc = next(d for d in workspace.rows("documents") if d["kind"] == "fulltext")
    (workspace.path / doc["file"]).write_bytes(b"tampered")
    with pytest.raises(ValidationError, match="digest changed"):
        validate_complete(workspace)


@pytest.mark.asyncio
async def test_identity_mismatch_blocks_export(tmp_path):
    workspace, _ = assessed_workspace(tmp_path)

    def handler(request):
        return httpx.Response(
            200,
            json={
                "resultList": {
                    "result": [
                        {"id": "999", "source": "MED", "title": "Wrong bibliographic identity"}
                    ]
                }
            },
        )

    async with HttpSession(
        transport=httpx.MockTransport(handler), intervals={"europe-pmc": 0}
    ) as session:
        value = await verify(workspace, session=session)
    assert not value["ready"]
    with pytest.raises(ValidationError, match="identity mismatches"):
        export(workspace)


def test_budget_counts_across_searches_and_review_requires_approval(tmp_path):
    workspace = workspace_at(tmp_path / "research", records=3)
    search, strategy = completed_search(workspace, tmp_path / "search")
    workspace.reserve(search.path, strategy)
    workspace.attach(search.path)
    assert workspace.allocations()["europe-pmc"] == 2
    rid = workspace.rows("records")[0]["record_id"]
    with pytest.raises(ValidationError, match="exceeds research budget"):
        plan_snowball(workspace, rid, "references", 2)
    assert plan_snowball(workspace, rid, "references", 1)["status"] == "planned"
    review = workspace_at(tmp_path / "review", mode="review-prep")
    with pytest.raises(ValidationError, match="unapproved quick search"):
        review.attach(search.path)
    with pytest.raises(ValidationError, match="explicit"):
        workspace_at(tmp_path / "missing-limits", mode="review-prep", records=None, fulltexts=None)


def test_interrupted_attachment_recovers(tmp_path, monkeypatch):
    workspace = workspace_at(tmp_path / "research")
    search, _ = completed_search(workspace, tmp_path / "search")
    original = workspace.put

    def interrupt(stage, *args, **kwargs):
        if stage == "documents":
            raise OSError("simulated interruption")
        return original(stage, *args, **kwargs)

    monkeypatch.setattr(workspace, "put", interrupt)
    with pytest.raises(OSError, match="interruption"):
        workspace.attach(search.path)
    monkeypatch.setattr(workspace, "put", original)
    workspace.attach(search.path)
    assert len(workspace.rows("documents")) == 2


def test_attachment_rejects_counts_that_disagree_with_source_files(tmp_path):
    workspace = workspace_at(tmp_path / "research")
    search, _ = completed_search(workspace, tmp_path / "search")
    manifest = search.read_json("manifest.json")
    manifest["sources"]["europe-pmc"]["retained"] = 1
    search.write_manifest(manifest)
    with pytest.raises(ValidationError, match="counts disagree"):
        workspace.attach(search.path)
    assert not workspace.load()["searches"]


@pytest.mark.asyncio
async def test_fulltext_pending_resume_and_retry_do_not_double_count(tmp_path, monkeypatch):
    workspace = workspace_at(tmp_path / "research", fulltexts=1)
    search, _ = completed_search(workspace, tmp_path / "search")
    workspace.attach(search.path)
    records = workspace.rows("records")
    stage(
        workspace,
        "screening",
        [
            {
                "record_id": r["record_id"],
                "decision": "include",
                "basis": "title-abstract",
                "reason": "Synthetic",
            }
            for r in records
        ],
    )
    rid = records[0]["record_id"]
    manifest = workspace.load()
    manifest["fulltext_attempts"][rid] = {"status": "running"}
    workspace.save(manifest)

    async def unavailable(*_args):
        raise ValidationError("Synthetic inaccessible full text")

    monkeypatch.setattr("hermes_medical_research.fulltext.acquire", unavailable)
    async with HttpSession(transport=httpx.MockTransport(lambda _: httpx.Response(500))) as session:
        result = await fetch_fulltexts(workspace, None, session=session)
        assert result["fulltexts"][rid]["status"] == "unavailable"
        assert workspace.status()["fulltext_attempts"] == 1
        result = await fetch_fulltexts(workspace, None, retry=True, session=session)
        assert rid in result["fulltexts"]
        with pytest.raises(ValidationError, match="budget"):
            await fetch_fulltexts(workspace, [records[1]["record_id"]], session=session)


def test_jats_tables_and_url_restrictions(tmp_path):
    segments = parse_jats(
        b"<article><body><sec><title>Results</title><p>A paragraph.</p>"
        b"<table-wrap><label>Table 1</label><table><tr><th>Arm</th><th>N</th></tr>"
        b"<tr><td>A</td><td>42</td></tr></table></table-wrap></sec></body></article>"
    )
    assert {s["locator"] for s in segments} == {"heading:1", "paragraph:1", "table:1"}
    assert "A | 42" in segments[-1]["text"]
    local_file = tmp_path / "example.txt"
    local_file.write_text("Harmless local-file fixture.")
    for url in (local_file.as_uri(), "https://127.0.0.1/file", "https://user:pass@example.org/a"):
        with pytest.raises(ValidationError):
            public_url(url)


def test_tampered_protocol_and_artifact_are_rejected(tmp_path):
    workspace, _ = assessed_workspace(tmp_path)
    entry = workspace.load()["datasets"]["screening"]
    (workspace.path / entry["file"]).write_text("{}")
    with pytest.raises(ValidationError, match="modified outside"):
        workspace.read("screening")
    manifest = workspace.load()
    manifest["protocol"]["outcomes"].append("unapproved change")
    workspace.save(manifest)
    with pytest.raises(ValidationError, match="protocol changed"):
        workspace.status()


@pytest.mark.asyncio
async def test_user_pdf_has_a_persisted_page_and_invalidates_extractions(tmp_path):
    workspace, _ = assessed_workspace(tmp_path)
    rid = workspace.rows("records")[0]["record_id"]
    async with HttpSession(transport=httpx.MockTransport(lambda _: httpx.Response(500))) as session:
        result = await fetch_fulltexts(
            workspace, [rid], pdf=Path(__file__).parent / "fixtures/synthetic.pdf", session=session
        )
    assert result["fulltexts"][rid]["status"] == "available"
    doc = next(d for d in workspace.rows("documents") if d["kind"] == "fulltext")
    assert doc["segments"] == [{"locator": "page:1", "text": "Synthetic PDF result."}]
    assert (workspace.path / doc["file"]).exists()
    assert workspace.status()["stages"]["extractions"] == "stale"
