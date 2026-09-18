"""Small bounded packet templates used by the task engine."""

from __future__ import annotations

import math
import re
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


_TERM = re.compile(r"[0-9a-z][0-9a-z\-]*[0-9a-z]|[0-9a-z]", re.IGNORECASE)
_STOP_TERMS = frozenset(
    {
        "about",
        "among",
        "and",
        "between",
        "for",
        "from",
        "into",
        "other",
        "outcome",
        "outcomes",
        "that",
        "the",
        "their",
        "these",
        "this",
        "those",
        "with",
    }
)
FIND_LIMIT = 20
SNIPPET_BEFORE = 120
SNIPPET_AFTER = 240


def terms(text: str) -> list[str]:
    """Distinct casefolded search terms; hyphenated names such as Mini-CEX stay whole."""
    values = []
    for match in _TERM.finditer(text.casefold()):
        term = match.group(0)
        if len(term) < 3 or term in _STOP_TERMS:
            continue
        values.append(term)
        # "mini-cex" should also match text that writes "Mini CEX" or "CEX".
        values.extend(part for part in term.split("-") if len(part) >= 3)
    return list(dict.fromkeys(values))


def _snippet(
    text: str,
    needles: list[str],
    *,
    before: int = SNIPPET_BEFORE,
    after: int = SNIPPET_AFTER,
) -> str:
    folded = text.casefold()
    positions = [folded.find(needle) for needle in needles if needle in folded]
    start = min(positions) if positions else 0
    begin = max(0, start - before)
    end = min(len(text), start + after)
    value = " ".join(text[begin:end].split())
    return ("..." if begin else "") + value + ("..." if end < len(text) else "")


def find_in_documents(
    documents: list[dict[str, Any]], query: str, *, offset: int = 0, limit: int = 5
) -> dict[str, Any]:
    """Rank segments by distinct query terms across documents, keeping document order on ties."""
    if offset < 0 or not 1 <= limit <= FIND_LIMIT:
        raise ValidationError(f"offset must be nonnegative and limit between 1 and {FIND_LIMIT}")
    needles = terms(query)
    if not needles:
        raise ValidationError("query needs at least one term of three or more characters")
    ranked = []
    position = 0
    for document in documents:
        segments = document.get("segments", [])
        for index, segment in enumerate(segments):
            folded = segment["text"].casefold()
            matched = [needle for needle in needles if needle in folded]
            if matched:
                ranked.append((-len(matched), position, document, index, matched))
            position += 1
    ranked.sort(key=lambda item: (item[0], item[1]))
    hits = []
    for negative, _, document, index, matched in ranked[offset : offset + limit]:
        segments = document["segments"]
        segment = segments[index]
        hits.append(
            {
                "document_id": document["document_id"],
                "locator": segment["locator"],
                "matched_terms": matched,
                "match_count": -negative,
                "previous_locator": segments[index - 1]["locator"] if index else None,
                "next_locator": (
                    segments[index + 1]["locator"] if index + 1 < len(segments) else None
                ),
                "snippet": _snippet(segment["text"], matched),
            }
        )
    return {
        "query_terms": needles,
        "total": len(ranked),
        "offset": offset,
        "limit": limit,
        "hits": hits,
    }


def outcome_hits(
    workspace,
    record_id: str,
    outcomes: list[str],
    *,
    per_outcome: int = 3,
    preview: int = 120,
) -> dict[str, list[dict[str, str]]]:
    """Point the Extractor at likely result locations for each protocol outcome.

    Hits are navigation aids only.  Terms are weighted by rarity within the record's citable
    non-metadata segments so generic words such as "clinical" do not dominate.
    """
    segments = [
        (document["document_id"], segment)
        for document in workspace.source_documents(fresh=False)
        if document["record_id"] == record_id and document["kind"] != "metadata"
        for segment in document["segments"]
    ]
    folded = [segment["text"].casefold() for _, segment in segments]
    result: dict[str, list[dict[str, str]]] = {}
    for outcome in outcomes:
        needles = terms(outcome)
        weights = {}
        for needle in needles:
            frequency = sum(needle in text for text in folded)
            if frequency:
                weights[needle] = math.log((1 + len(segments)) / frequency)
        scored = []
        for index, ((document_id, segment), text) in enumerate(
            zip(segments, folded, strict=True)
        ):
            matched = [needle for needle in weights if needle in text]
            if not matched:
                continue
            score = sum(weights[needle] for needle in matched)
            scored.append((-score, index, document_id, segment, matched))
        scored.sort(key=lambda item: (item[0], item[1]))
        result[outcome] = [
            {
                "document_id": document_id,
                "locator": segment["locator"],
                "preview": _snippet(
                    segment["text"], matched, before=preview // 4, after=preview - preview // 4
                ),
            }
            for _, _, document_id, segment, matched in scored[:per_outcome]
        ]
    return result


def assessment_field_rules(contract: bool) -> dict[str, Any]:
    """Allowed values and cross-field rules that the validators enforce, stated up front."""
    from .evidence import BASES, COMPARATORS
    from .validation import DISPOSITION_STATUSES

    rules: dict[str, Any] = {
        "extraction": {
            "protocol_outcome": "exactly one protocol outcome string as listed in the packet",
            "outcome": "the paper's own label for this measure",
            "population, comparison, timepoint, result, direction_rationale, support_rationale": (
                "nonempty text"
            ),
            "support_checked": "true once you confirmed the quote supports the result",
            "sample_size": "positive integer or null",
            "comparator_type": sorted(COMPARATORS),
            "outcome_type": ["benefit", "context", "harm"],
            "favors": ["comparator", "intervention", "neither", "not-applicable", "uncertain"],
            "effect.basis": sorted(BASES),
            "effect.measure, effect.units": (
                "nonempty text; an effect estimate such as mean difference, risk ratio, or "
                "proportion, never a test statistic or P value"
            ),
            "effect.value, effect.ci_low, effect.ci_high": (
                "number or null; a null value needs effect.missing_reason"
            ),
            "effect.interval_type": {
                "none": "ci_low and ci_high must be null",
                "confidence or credible": "both endpoints and interval_level between 0 and 100",
            },
            "between_group basis": "requires comparator_type other than none or not_applicable",
            "qualitative basis": "effect.value must be null",
            "single-arm or intervention-only data": (
                "group_summary or qualitative, never between_group"
            ),
            "source_location": "document_id, locator, and a quote copied verbatim from source_read",
            "harms (outcome_type harm only)": (
                "reporting counts, monitored_without_counts, or not_reported; attribution; arms"
            ),
        },
        "appraisal": {
            "domains.*.status": ["assessed", "pending", "unavailable"],
            "domains.*.judgment": (
                "a judgment such as low, some_concerns, or high when assessed; "
                "not_assessed otherwise"
            ),
            "domains.*.source_locations": "document_id, locator, quote for every assessed domain",
            "domains.*.missing_reason (unavailable only)": [
                "access_unavailable",
                "insufficient_detail",
                "not_reported",
            ],
            "unavailable domains": "assessment_basis text and inspected_locations",
            "completion": "complete if all assessed, limited if any unavailable, else pending",
            "overall_judgment": [
                "descriptive",
                "high",
                "low",
                "not_assessable",
                "some_concerns",
                "unclear",
            ],
            "limited or pending appraisal": "overall_judgment unclear or not_assessable",
            "same_as": (
                "a row {extraction_id, same_as: ID} copies the full appraisal ID at submission; "
                "fill the study appraisal once and write a separate full row only when an "
                "outcome's risk of bias differs"
            ),
            "pending": "an extracted row's appraisal cannot stay pending at submission",
            "overall, rationale": "nonempty text",
        },
    }
    if contract:
        rules["dispositions"] = {
            "outcomes[].status": list(DISPOSITION_STATUSES),
            "reported outcome": (
                "the study measured it; remarks in the discussion, limitations, or author opinion "
                "are not a reported outcome"
            ),
            "extracted": "at least one filled extraction row with this protocol_outcome",
            "extraction_ids": "leave empty; hmr fills them",
            "not_reported or not_applicable": (
                "rationale plus inspected_locations with document_id and locator"
            ),
        }
    return rules


def synthesis_field_rules() -> dict[str, Any]:
    from .evidence import COMPARATORS
    from .validation import GRADE_DOMAINS, RELATIONSHIPS

    return {
        "finding.claim_basis": [
            "association",
            "comparative",
            "context",
            "diagnostic_accuracy",
            "gap",
            "ranking",
            "within_group",
        ],
        "finding.comparator_type": sorted(COMPARATORS),
        "gap": "empty evidence, gap_reason, gap_basis, and not-assessable certainty",
        "evidence[].use": ["context", "direct", "indirect"],
        "evidence[].relationship": sorted(RELATIONSHIPS),
        "evidence[]": (
            "weight_rationale, alignment_rationale, and claim_support_checked true; direct use "
            "needs the same comparator_type and an extraction bound to this protocol outcome"
        ),
        "comparative claims": "non-context evidence must have effect.basis between_group",
        "certainty.framework": ["GRADE-informed", "descriptive"],
        "certainty.rating": [
            "high",
            "low",
            "moderate",
            "not-applicable",
            "not-assessable",
            "very-low",
        ],
        "GRADE-informed": (
            "rationale, starting_point, rating_explanation, and text for every domain: "
            + ", ".join(GRADE_DOMAINS)
        ),
        "descriptive": "rating not-applicable",
        "overlap.status": ["mapped", "not_applicable", "suspected", "unknown"],
    }


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
        "fulltext_attempt": workspace.manifest_view()["fulltext_attempts"].get(record_id),
    }


def _extraction(
    workspace,
    record_id: str,
    study_id: str,
    extraction_id: str,
    protocol_outcome: str | None = None,
) -> dict:
    row = {
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
    if protocol_outcome is not None:
        row["protocol_outcome"] = protocol_outcome
    return row


def study_appraisal_id(record_id: str) -> str:
    """Identity of the one full appraisal template that outcome rows copy with ``same_as``."""
    return f"study-appraisal-{record_id}"


def scaffold_extraction_id(record_id: str, index: int) -> str:
    """Deterministic scaffold identity for the index-th (1-based) protocol outcome."""
    return f"result-{record_id}-o{index}"


def _disposition(record_id: str, study_id: str, outcomes: list[str]) -> dict:
    return {
        "record_id": record_id,
        "study_id": study_id,
        "outcomes": [
            {
                "protocol_outcome": outcome,
                "status": "",
                "rationale": "",
                "extraction_ids": [],
                "inspected_locations": [],
            }
            for outcome in outcomes
        ],
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
