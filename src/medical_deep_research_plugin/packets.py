"""Small source packets and editable inputs for the host's next reasoning step."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from hermes_medical_search.artifacts import _atomic_write
from hermes_medical_search.models import ValidationError

from .evidence import REVIEW_CHECKS, review_digest
from .validation import GRADE_DOMAINS, METHODS
from .workflow import Preview, current_digests
from .workspace import Workspace, digest


def documents(
    workspace, *, record_id=None, document_id=None, locator=None, query=None, offset=0, limit=10
):
    if offset < 0 or limit < 1:
        raise ValidationError("offset must be nonnegative and limit positive")
    items = []
    for doc in workspace.rows("documents", fresh=False):
        if record_id and doc["record_id"] != record_id:
            continue
        if document_id and doc["document_id"] != document_id:
            continue
        segments = doc["segments"]
        for i, segment in enumerate(segments):
            if locator and segment["locator"] != locator:
                continue
            if query and query.casefold() not in segment["text"].casefold():
                continue
            items.append(
                {
                    "document_id": doc["document_id"],
                    "record_id": doc["record_id"],
                    "kind": doc["kind"],
                    **segment,
                    "previous_locator": segments[i - 1]["locator"] if i else None,
                    "next_locator": segments[i + 1]["locator"] if i + 1 < len(segments) else None,
                }
            )
    return {"total": len(items), "offset": offset, "segments": items[offset : offset + limit]}


def _source(workspace, rid):
    record = workspace.index("records")[rid]
    docs = [d for d in workspace.rows("documents", fresh=False) if d["record_id"] == rid]
    # Keep methods/results and table context discoverable; never hide omitted text.
    excerpts = []
    locators = []
    for d in docs:
        locators.append(
            {
                "document_id": d["document_id"],
                "kind": d["kind"],
                "segments": [
                    {"locator": s["locator"], "preview": s["text"][:100]} for s in d["segments"]
                ],
            }
        )
        for s in d["segments"][:2]:
            excerpts.append(
                {
                    "document_id": d["document_id"],
                    "locator": s["locator"],
                    "text": s["text"][:3000],
                    "truncated": len(s["text"]) > 3000,
                }
            )
    return {
        "record_id": rid,
        **{k: record.get(k) for k in ("title", "authors", "year", "doi", "pmid", "url")},
        "documents": locators,
        "excerpts": excerpts,
        "fulltext_attempt": workspace.load()["fulltext_attempts"].get(rid),
    }


def _extraction(workspace, rid, sid, eid):
    return {
        "extraction_id": eid,
        "record_id": rid,
        "study_id": sid,
        "population": "",
        "comparison": "",
        "outcome": "",
        "timepoint": "",
        "comparator_type": "",
        "outcome_type": "benefit",
        "sample_size": None,
        "result": "",
        "effect": {
            "measure": "",
            "basis": "",
            "value": None,
            "ci_low": None,
            "ci_high": None,
            "units": "",
            "interval_type": "none",
            "interval_level": None,
            "missing_reason": "",
        },
        "favors": "uncertain",
        "direction_rationale": "",
        "source_location": {"document_id": "", "locator": "", "quote": ""},
        "support_checked": False,
        "support_rationale": "",
    }


def _appraisal(eid, method):
    version, domains = METHODS[method]
    return {
        "extraction_id": eid,
        "method": method,
        "method_version": version,
        "completion": "pending",
        "overall_judgment": "not_assessable",
        "overall": "",
        "rationale": "",
        "domains": {
            k: {
                "status": "pending",
                "judgment": "not_assessed",
                "rationale": "",
                "source_locations": [],
                "missing_reason": "",
                "assessment_basis": "",
                "inspected_locations": [],
            }
            for k in domains.split()
        },
    }


def _finding(outcome, number):
    return {
        "finding_id": f"finding-{number}",
        "protocol_outcomes": [outcome],
        "population": "",
        "comparison": "",
        "outcome": outcome,
        "timepoint": "",
        "claim_basis": "",
        "comparator_type": "",
        "conclusion": "",
        "evidence": [],
        "overlap": {"status": "unknown", "rationale": ""},
        "certainty": {
            "origin": "report_assessment",
            "framework": "GRADE-informed",
            "rating": "not-assessable",
            "rationale": "",
            "starting_point": "",
            "rating_explanation": "",
            "domains": {key: "" for key in GRADE_DOMAINS},
        },
    }


def next_packet(
    workspace: Workspace,
    *,
    stage: str | None = None,
    limit: int | None = None,
    output: Path | None = None,
) -> dict:
    if workspace.evidence_version != "2":
        raise ValidationError(
            "legacy workspace: read with research status; create a v2 run for "
            "explicit re-assessment"
        )
    if limit is not None and limit < 1:
        raise ValidationError("limit must be positive")
    view = Preview(workspace)
    manifest = view.load()
    records = view.index("records")
    screening = {r["record_id"]: r for r in view.rows("screening", fresh=False)}
    coverage = {r["record_id"]: r for r in view.rows("coverage", fresh=False)}
    studies = view.rows("studies", fresh=False)
    by_record = {rid: s["study_id"] for s in studies for rid in s["record_ids"]}
    extractions = view.rows("extractions", fresh=False)
    appraisals = {a["extraction_id"]: a for a in view.rows("appraisals", fresh=False)}
    selected = [rid for rid, c in coverage.items() if c["selection"] == "selected"]
    unscreened = [rid for rid in records if rid not in screening]
    uncovered = [
        rid for rid, s in screening.items() if s["decision"] == "include" and rid not in coverage
    ]
    no_text = [rid for rid in selected if rid not in manifest["fulltext_attempts"]]
    unlinked = [rid for rid in selected if rid not in by_record]
    unfinished = [
        rid
        for rid in selected
        if not any(e["record_id"] == rid for e in extractions)
        or any(
            e["extraction_id"] not in appraisals
            or appraisals[e["extraction_id"]].get("completion") == "pending"
            for e in extractions
            if e["record_id"] == rid
        )
    ]
    fulltext_limit = manifest["protocol"]["fulltexts"]
    text_capacity = fulltext_limit == "all" or len(manifest["fulltext_attempts"]) < fulltext_limit
    if stage is None:
        if not records and "records" not in manifest["datasets"]:
            stage = "retrieval"
        elif unscreened or view.stale("screening"):
            stage = "screening"
        elif uncovered or "coverage" not in manifest["datasets"] or view.stale("coverage"):
            stage = "coverage"
        elif no_text and text_capacity:
            stage = "fulltext"
        elif unlinked or "studies" not in manifest["datasets"] or view.stale("studies"):
            stage = "studies"
        elif unfinished or any(
            s not in manifest["datasets"] or view.stale(s) for s in ("extractions", "appraisals")
        ):
            stage = "assessment"
        elif "synthesis" not in manifest["datasets"] or view.stale("synthesis"):
            stage = "synthesis"
        else:
            stage = "review"
    size = limit or (25 if stage in {"screening", "coverage"} else 3)
    payloads = {}
    ids = []
    task = ""

    def rows(name, values):
        payloads[name] = {"schema_version": "2", "records": values}

    if stage == "screening":
        ids = (unscreened or list(records))[:size]
        rows(
            "screening",
            [
                deepcopy(screening[rid])
                if rid in screening
                else {"record_id": rid, "decision": "", "basis": "title-abstract", "reason": ""}
                for rid in ids
            ],
        )
        task = (
            "Screen every record against eligibility. Do not exclude for a cap or "
            "missing full text."
        )
    elif stage == "coverage":
        ids = (uncovered or [rid for rid, s in screening.items() if s["decision"] == "include"])[
            :size
        ]
        rows(
            "coverage",
            [
                deepcopy(coverage[rid])
                if rid in coverage
                else {"record_id": rid, "selection": "", "reason": "", "protocol_outcomes": []}
                for rid in ids
            ],
        )
        task = (
            "Select detailed assessments by relevance, methods, important outcomes "
            "and disagreements. Explain deferred/unavailable records; retain their "
            "eligibility."
        )
    elif stage == "studies":
        ids = (unlinked or selected)[:size]
        rows(
            "studies",
            [
                deepcopy(next(s for s in studies if s["study_id"] == by_record[rid]))
                if rid in by_record
                else {"study_id": "study-" + rid, "record_ids": [rid], "kind": "", "basis": ""}
                for rid in ids
            ],
        )
        task = (
            "Link reports of the same study using identity evidence. Reuse existing"
            " study IDs when appropriate. Keep reviews/guidelines distinct."
        )
    elif stage == "assessment":
        ids = (unfinished or selected)[:size]
        es, aps = [], []
        for rid in ids:
            if rid not in by_record:
                raise ValidationError("record studies before requesting assessment packets")
            values = [deepcopy(e) for e in extractions if e["record_id"] == rid]
            if not values:
                values = [_extraction(view, rid, by_record[rid], "result-" + rid)]
            es.extend(values)
            study = next(s for s in studies if s["study_id"] == by_record[rid])
            method = "robis" if study["kind"] == "systematic-review" else "descriptive"
            for e in values:
                aps.append(
                    deepcopy(appraisals[e["extraction_id"]])
                    if e["extraction_id"] in appraisals
                    else _appraisal(e["extraction_id"], method)
                )
        rows("extractions", es)
        rows("appraisals", aps)
        task = (
            "Read source methods/results in context. Add one extraction per "
            "outcome/estimand needed, each with its own appraisal. Choose the "
            "design-appropriate method. Fill every pending domain or document "
            "genuinely unavailable detail."
        )
    elif stage == "synthesis":
        payloads["synthesis"] = {
            "schema_version": "2",
            "title": "",
            "limitations": [],
            "findings": [_finding(o, i) for i, o in enumerate(manifest["protocol"]["outcomes"], 1)],
        }
        ids = list(dict.fromkeys(e["record_id"] for e in extractions))
        task = (
            "Address every protocol outcome. Use appraised estimates and explicit "
            "scope alignment. Explain overlapping reviews and disagreements. Record"
            " gaps where unsupported; do not infer equivalence or safety from "
            "missing data."
        )
    elif stage == "review":
        findings = view.read("synthesis")["findings"]
        previous = {r["finding_id"]: r for r in view.rows("reviews", fresh=False)}
        pending = [
            f
            for f in findings
            if f["finding_id"] not in previous
            or previous[f["finding_id"]].get("status") != "pass"
            or previous[f["finding_id"]].get("review_digest") != review_digest(view, f)
        ]
        # Re-record an unchanged, stale review stage after checking its evidence digests.
        chosen = (
            pending or findings
            if view.stale("reviews") or "reviews" not in manifest["datasets"]
            else pending
        )[:size]
        rows(
            "reviews",
            [
                {
                    "finding_id": f["finding_id"],
                    "review_digest": review_digest(view, f),
                    "status": "revise",
                    "checks": {k: {"status": "revise", "rationale": ""} for k in REVIEW_CHECKS},
                }
                for f in chosen
            ],
        )
        ids = list(
            dict.fromkeys(
                e["record_id"]
                for f in chosen
                for c in f["evidence"]
                for e in extractions
                if e["extraction_id"] == c["extraction_id"]
            )
        )
        task = (
            "Separate claim-review pass: re-read each conclusion against its "
            "sources. Inspect estimates, scope/comparators, harms, overlap, and "
            "certainty. Explain each check; fix synthesis first if any claim fails."
        )
        if not chosen and "reviews" in manifest["datasets"]:
            stage, payloads, task = (
                "finalize",
                {},
                "Run finalization; it checks evidence, verifies identities, and exports.",
            )
    elif stage == "fulltext":
        ids = no_text[:size]
        task = (
            "Acquire selected full texts in a batch. Inspect recorded failures; "
            "retry only transient failures."
        )
    elif stage == "retrieval":
        task = (
            "Run doctor once; use the recorded protocol to plan, search, and attach"
            " results. Reserve 30% of source capacity for gaps; at most two "
            "supplementary rounds in report mode."
        )
    elif stage != "finalize":
        raise ValidationError("unknown work packet stage")
    batch = {"schema_version": "2", "base_digests": current_digests(view), "stages": payloads}
    packet_key = digest({"stage": stage, "batch": batch})[:12]
    directory = (output or workspace.path / "work" / f"{stage}-{packet_key}").resolve()
    directory.mkdir(parents=True, exist_ok=True)
    input_path = directory / "input.json"
    if payloads and not input_path.exists():
        _atomic_write(input_path, json.dumps(batch, ensure_ascii=False, indent=2) + "\n")
    cli = ["medical-deep-research-plugin", "research"]
    commands = {
        "inspect": cli
        + [
            "status",
            str(workspace.path),
            "--stage",
            "documents",
            "--record-id",
            "RECORD_ID",
            "--query",
            "TEXT",
        ],
        "submit": cli + ["record", str(workspace.path), "--batch", "--input", str(input_path)],
        "next": cli + ["next", str(workspace.path)],
        "check": cli + ["check", str(workspace.path)],
        "finalize": cli + ["finalize", str(workspace.path)],
    }
    if stage == "fulltext":
        commands["acquire"] = cli + ["fulltext", str(workspace.path), "--ids", ",".join(ids)]
    packet = {
        "schema_version": "2",
        "stage": stage,
        "task": task,
        "run_dir": str(workspace.path),
        "protocol": manifest["protocol"],
        "base_digests": batch["base_digests"],
        "input_path": str(input_path) if payloads else None,
        "commands": commands,
        "sources": [_source(view, rid) for rid in ids],
        "budget_guidance": (
            "Reserve the last 20% of a known host turn budget for "
            "review/finalization. CLI does not measure host turns."
        ),
    }
    if stage in {"synthesis", "review"}:
        packet["extractions"] = extractions
        packet["appraisals"] = list(appraisals.values())
        packet["studies"] = studies
        packet["coverage"] = list(coverage.values())
        packet["contribution_template"] = {
            "extraction_id": "",
            "relationship": "",
            "use": "",
            "weight_rationale": "",
            "alignment_rationale": "",
            "claim_support_checked": False,
        }
        if stage == "review":
            packet["findings"] = chosen
    _atomic_write(
        directory / "packet.json", json.dumps(packet, ensure_ascii=False, indent=2) + "\n"
    )
    workspace.store.write_json(
        "resume.json",
        {
            "stage": stage,
            "packet": str(directory / "packet.json"),
            "input": packet["input_path"],
            "base_digests": batch["base_digests"],
            "commands": commands,
        },
    )
    # Full packet is on disk. Compact stdout avoids replaying all study data after compaction.
    return {
        "stage": stage,
        "task": task,
        "packet_path": str(directory / "packet.json"),
        "input_path": packet["input_path"],
        "records": ids,
        "commands": commands,
    }
