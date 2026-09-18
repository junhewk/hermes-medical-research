"""Stage one protocol outcome of an assessment at a time.

An assessment proposal carries a scaffold extraction and appraisal row per protocol outcome, one
full study appraisal the rows copy, and one disposition row that must decide every outcome. Writing
all of that by hand cost about ten minutes of generation for a 26 KB file on the host, and a single
missing rationale sent the whole thing back.

Here a session answers one outcome per call. Each answer is checked against its shape, mapped into
the rows the task already minted, and written back; the task is submitted only when every outcome
has a decision, through the same ``TaskEngine.submit`` as before. Nothing here relaxes a rule: it
decides which fields the model owns and leaves identity, scaffolding and derived fields alone.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from . import answers
from .search.models import ValidationError

SCOPE_FIELDS = ("population", "comparison", "outcome", "timepoint")
EXTRACTION_FIELDS = (
    *SCOPE_FIELDS,
    "comparator_type",
    "outcome_type",
    "result",
    "favors",
    "direction_rationale",
    "support_rationale",
)


def record(
    shape: str, result: Any, proposal: dict[str, Any], packet: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Apply one answer and report which protocol outcomes still have no decision."""
    answers.check_shape(result, answers.ASSESSMENT_SCHEMAS[shape])
    staged = deepcopy(proposal)
    if shape == "study_appraisal":
        _apply_study_appraisal(staged, result)
    elif shape == "outcome_extracted":
        _apply_extracted(staged, result, packet)
    elif shape == "outcome_missing":
        _apply_missing(staged, result, packet)
    else:  # pragma: no cover - guarded by ASSESSMENT_TOOLS at the call site
        raise ValidationError(f"unknown assessment answer: {shape}")
    return staged, _remaining(staged, packet)


def _rows(proposal: dict[str, Any], stage: str) -> list[dict[str, Any]]:
    rows = ((proposal.get("stages") or {}).get(stage) or {}).get("records")
    if not isinstance(rows, list):
        raise ValidationError(f"this task has no {stage} rows to record into")
    return rows


def _checklist(packet: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The packet's per-outcome entries, keyed by the protocol outcome they name."""
    entries = packet.get("outcome_checklist")
    if not isinstance(entries, list) or not entries:
        raise ValidationError("this task has no outcome checklist; it is not a per-outcome run")
    return {
        str(entry["protocol_outcome"]): entry
        for entry in entries
        if isinstance(entry, dict) and entry.get("protocol_outcome")
    }


def _entry(packet: dict[str, Any], name: str) -> dict[str, Any]:
    checklist = _checklist(packet)
    if name not in checklist:
        raise ValidationError(
            f"{name!r} is not one of this record's protocol outcomes; they are "
            f"{sorted(checklist)}"
        )
    return checklist[name]


def _disposition(proposal: dict[str, Any], name: str) -> dict[str, Any]:
    row = _rows(proposal, "dispositions")[0]
    for item in row["outcomes"]:
        if item["protocol_outcome"] == name:
            return item
    raise ValidationError(f"the disposition row has no entry for {name!r}")


def _apply_study_appraisal(proposal: dict[str, Any], result: dict[str, Any]) -> None:
    """Fill the one full appraisal the outcome rows copy, deriving what the rules derive."""
    rows = _rows(proposal, "appraisals")
    template = next((row for row in rows if "same_as" not in row and row.get("domains")), None)
    if template is None:
        raise ValidationError("this task has no full appraisal template to fill")
    domains = template["domains"]
    answered = {str(item["name"]): item for item in result["domains"]}
    unknown = sorted(set(answered) - set(domains))
    if unknown:
        raise ValidationError(
            f"{unknown[0]!r} is not a domain of this appraisal method; its domains are "
            f"{sorted(domains)}"
        )
    missing = sorted(set(domains) - set(answered))
    if missing:
        raise ValidationError(f"every domain needs a status; {missing[0]!r} has none")
    for name, domain in domains.items():
        item = answered[name]
        status = item["status"]
        domain["status"] = status
        # An unfinished or unavailable domain must not assert a judgment, so only an assessed one
        # carries the model's words here.
        domain["judgment"] = item["judgment"] if status == "assessed" else "not_assessed"
        domain["rationale"] = item["rationale"]
        domain["assessment_basis"] = item.get("assessment_basis", "")
        domain["missing_reason"] = item.get("missing_reason", "") if status == "unavailable" else ""
        domain["inspected_locations"] = [
            dict(location) for location in item.get("inspected_locations") or []
        ]
        # An assessed domain needs the locations it read; the validator refuses a judgment without
        # them, so they are recorded exactly as answered.
        domain["source_locations"] = [
            dict(location) for location in item.get("source_locations") or []
        ]
    statuses = {domain["status"] for domain in domains.values()}
    # Completion follows from the domains, so the model never states it and cannot get it wrong.
    template["completion"] = (
        "pending" if "pending" in statuses else "limited" if "unavailable" in statuses
        else "complete"
    )
    template["overall_judgment"] = result["overall_judgment"]
    template["overall"] = result["overall"]
    template["rationale"] = result["rationale"]


def _apply_extracted(
    proposal: dict[str, Any], result: dict[str, Any], packet: dict[str, Any]
) -> None:
    name = result["protocol_outcome"]
    entry = _entry(packet, name)
    rows = _rows(proposal, "extractions")
    # The checklist names the scaffold rows the task minted for this outcome.
    scaffolds = [str(value) for value in entry.get("extraction_ids") or []]
    row = next(
        (item for item in rows
         if item.get("protocol_outcome") == name or item["extraction_id"] in scaffolds),
        None,
    )
    if row is None:
        raise ValidationError(f"this task has no extraction row for {name!r}")
    for field in EXTRACTION_FIELDS:
        row[field] = result[field]
    row["sample_size"] = result.get("sample_size")
    row["support_checked"] = True  # the model asserts it by calling this tool with a quote
    row["source_location"] = dict(result["source_location"])
    effect = {**row.get("effect", {}), **result["effect"]}
    for key in ("value", "ci_low", "ci_high", "interval_level"):
        effect.setdefault(key, None)
    effect.setdefault("missing_reason", "")
    row["effect"] = effect
    decided = _disposition(proposal, name)
    decided.update(
        status="extracted",
        rationale=decided.get("rationale") or "",
        extraction_ids=[row["extraction_id"]],
        inspected_locations=[dict(result["source_location"])],
    )


def _apply_missing(
    proposal: dict[str, Any], result: dict[str, Any], packet: dict[str, Any]
) -> None:
    name = result["protocol_outcome"]
    _entry(packet, name)
    decided = _disposition(proposal, name)
    decided.update(
        status=result["status"],
        rationale=result["rationale"],
        extraction_ids=[],
        inspected_locations=[dict(location) for location in result["inspected_locations"]],
    )


def _remaining(proposal: dict[str, Any], packet: dict[str, Any]) -> list[str]:
    """Outcomes with no decision yet, plus the appraisal when it is still untouched."""
    pending = [
        item["protocol_outcome"]
        for item in _rows(proposal, "dispositions")[0]["outcomes"]
        if not str(item.get("status") or "").strip()
    ]
    rows = _rows(proposal, "appraisals")
    template = next((row for row in rows if "same_as" not in row and row.get("domains")), None)
    if template is not None and not str(template.get("rationale") or "").strip():
        pending.append("the study appraisal")
    return pending
