"""Small bounded packet templates used by the task engine."""

from __future__ import annotations

from typing import Any

from hermes_medical_research.search.models import ValidationError

from .validation import GRADE_DOMAINS, METHODS


def documents(
    workspace,
    *,
    record_id=None,
    document_id=None,
    locator=None,
    query=None,
    offset=0,
    limit=10,
):
    if offset < 0 or limit < 1:
        raise ValidationError("offset must be nonnegative and limit positive")
    items = []
    for document in workspace.source_documents(fresh=False):
        if record_id and document["record_id"] != record_id:
            continue
        if document_id and document["document_id"] != document_id:
            continue
        segments = document["segments"]
        for index, segment in enumerate(segments):
            if locator and segment["locator"] != locator:
                continue
            if query and query.casefold() not in segment["text"].casefold():
                continue
            items.append(
                {
                    "document_id": document["document_id"],
                    "record_id": document["record_id"],
                    "kind": document["kind"],
                    **segment,
                    "previous_locator": segments[index - 1]["locator"] if index else None,
                    "next_locator": (
                        segments[index + 1]["locator"] if index + 1 < len(segments) else None
                    ),
                }
            )
    return {"total": len(items), "offset": offset, "segments": items[offset : offset + limit]}


def _source(workspace, record_id: str) -> dict[str, Any]:
    record = workspace.index("records")[record_id]
    docs = [
        document
        for document in workspace.source_documents(fresh=False)
        if document["record_id"] == record_id
    ]
    excerpts = []
    locators = []
    for document in docs:
        locators.append(
            {
                "document_id": document["document_id"],
                "kind": document["kind"],
                "segment_count": len(document["segments"]),
                "segments": [
                    {"locator": segment["locator"], "preview": segment["text"][:100]}
                    for segment in document["segments"][:20]
                ],
                "truncated": len(document["segments"]) > 20,
            }
        )
        for segment in document["segments"][:2]:
            excerpts.append(
                {
                    "document_id": document["document_id"],
                    "locator": segment["locator"],
                    "text": segment["text"][:3000],
                    "truncated": len(segment["text"]) > 3000,
                }
            )
    return {
        "record_id": record_id,
        **{
            key: record.get(key)
            for key in ("title", "authors", "year", "doi", "pmid", "url")
        },
        "documents": locators,
        "excerpts": excerpts,
        "fulltext_attempt": workspace.load()["fulltext_attempts"].get(record_id),
    }


def _extraction(workspace, record_id: str, study_id: str, extraction_id: str) -> dict:
    return {
        "extraction_id": extraction_id,
        "record_id": record_id,
        "study_id": study_id,
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


def _appraisal(extraction_id: str, method: str) -> dict:
    version, domains = METHODS[method]
    return {
        "extraction_id": extraction_id,
        "method": method,
        "method_version": version,
        "completion": "pending",
        "overall_judgment": "not_assessable",
        "overall": "",
        "rationale": "",
        "domains": {
            key: {
                "status": "pending",
                "judgment": "not_assessed",
                "rationale": "",
                "source_locations": [],
                "missing_reason": "",
                "assessment_basis": "",
                "inspected_locations": [],
            }
            for key in domains.split()
        },
    }


def _finding(outcome: str, number: int) -> dict:
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
