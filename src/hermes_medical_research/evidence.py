"""Evidence semantics declared by the host, and explicit limits on report readiness.

These checks establish consistency, not that a model understood the source correctly.
A separate digest-bound host review remains necessary.
"""

from __future__ import annotations

from hermes_medical_research.search.models import ValidationError

from .validation import _choice, _known, validate_location
from .workspace import Workspace, digest, require_text

BASES = {
    "between_group",
    "within_group",
    "group_summary",
    "association",
    "diagnostic_accuracy",
    "ranking",
    "qualitative",
}
COMPARATORS = {"inactive", "active_exercise", "active_other", "mixed", "none", "not_applicable"}
REVIEW_CHECKS = ("estimates", "scope", "harms", "overlap", "certainty")


def object_field(row: dict, key: str) -> dict:
    item = row.get(key)
    if not isinstance(item, dict):
        raise ValidationError(f"{key} must be an object")
    return item


def strings(row: dict, key: str, *, nonempty: bool = True) -> list[str]:
    items = row.get(key)
    if not isinstance(items, list) or (nonempty and not items):
        raise ValidationError(f"{key} must be {'a nonempty' if nonempty else 'an'} array")
    for item in items:
        require_text(item, key)
    return items


def review_digest(workspace: Workspace, finding: dict) -> str:
    extractions = workspace.index("extractions")
    appraisals = workspace.index("appraisals")
    studies = workspace.index("studies")
    documents = workspace.source_index()
    evidence = []
    for contribution in finding["evidence"]:
        eid = contribution["extraction_id"]
        extraction = extractions[eid]
        evidence.append(
            {
                "extraction": extraction,
                "appraisal": appraisals[eid],
                "study": studies[extraction["study_id"]],
                "document": documents[extraction["source_location"]["document_id"]],
            }
        )
    overlap_sources = [
        {
            "document": documents[m["source_location"]["document_id"]],
            "review": studies[m["review_study_id"]],
            "primary_studies": [studies[sid] for sid in m["primary_study_ids"]],
        }
        for m in (finding.get("overlap", {}).get("mappings") or [])
    ]
    return digest({"finding": finding, "evidence": evidence, "overlap_sources": overlap_sources})


def validate_extraction(workspace: Workspace, row: dict) -> None:
    effect = row["effect"]
    _choice(effect.get("basis"), BASES, "effect.basis")
    _choice(row.get("comparator_type"), COMPARATORS, "comparator_type")
    _choice(row.get("outcome_type"), {"benefit", "harm", "context"}, "outcome_type")
    interval = effect.get("interval_type")
    _choice(interval, {"confidence", "credible", "none"}, "effect.interval_type")
    endpoints = (effect["ci_low"], effect["ci_high"])
    if interval == "none":
        if any(x is not None for x in endpoints):
            raise ValidationError("effect.interval_type none cannot have interval endpoints")
    else:
        level = effect.get("interval_level")
        if isinstance(level, bool) or not isinstance(level, (int, float)) or not 0 < level < 100:
            raise ValidationError("effect.interval_level must be a percentage between 0 and 100")
        if any(x is None for x in endpoints):
            raise ValidationError("reported intervals need both endpoints")
    if effect["basis"] == "qualitative" and effect["value"] is not None:
        raise ValidationError("qualitative estimates must use null, not a group mean or count")
    if effect["basis"] == "between_group" and row["comparator_type"] in {"none", "not_applicable"}:
        raise ValidationError("between_group estimates require a comparator")
    if "standard" in effect["measure"].casefold() and effect["units"].casefold() == "mmhg":
        raise ValidationError("standardised effects cannot be labeled mmHg")
    if effect["basis"] == "ranking" and row["favors"] not in {"uncertain", "not-applicable"}:
        raise ValidationError("rankings do not establish clinical superiority")
    if row["outcome_type"] == "harm":
        harms = object_field(row, "harms")
        _choice(
            harms.get("reporting"),
            {"counts", "monitored_without_counts", "not_reported"},
            "harms.reporting",
        )
        require_text(harms.get("attribution"), "harms.attribution")
        arms = harms.get("arms")
        if not isinstance(arms, list) or (harms["reporting"] == "counts" and not arms):
            raise ValidationError("harms.arms must contain reported counts or an empty array")
        for arm in arms:
            if not isinstance(arm, dict):
                raise ValidationError("harms arm must be an object")
            require_text(arm.get("name"), "harm arm name")
            require_text(arm.get("event"), "harm event")
            for key in ("events", "denominator"):
                v = arm.get(key)
                if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 0):
                    raise ValidationError(f"harm {key} must be a nonnegative integer or null")
            if harms["reporting"] != "counts" and arm.get("events") is not None:
                raise ValidationError("unreported harms cannot have an event count, including zero")
            if arm.get("events") is None or arm.get("denominator") is None:
                require_text(arm.get("missing_reason"), "harm missing_reason")


def validate_appraisal(workspace: Workspace, row: dict) -> None:
    _choice(row.get("completion"), {"pending", "complete", "limited"}, "appraisal completion")
    _choice(
        row.get("overall_judgment"),
        {"low", "some_concerns", "high", "unclear", "not_assessable", "descriptive"},
        "overall_judgment",
    )
    extraction = workspace.index("extractions")[row["extraction_id"]]
    docs = [d for d in workspace.source_documents() if d["record_id"] == extraction["record_id"]]
    for name, domain in row["domains"].items():
        status = domain.get("status")
        _choice(status, {"pending", "assessed", "unavailable"}, f"domains.{name}.status")
        if status == "assessed" and domain["judgment"] == "not_assessed":
            raise ValidationError(f"domains.{name}: assessed requires a judgment")
        if status != "assessed" and domain["judgment"] != "not_assessed":
            raise ValidationError(
                f"domains.{name}: unfinished/unavailable cannot assert a judgment"
            )
        if status == "unavailable":
            _choice(
                domain.get("missing_reason"),
                {"access_unavailable", "not_reported", "insufficient_detail"},
                f"domains.{name}.missing_reason",
            )
            require_text(domain.get("assessment_basis"), f"domains.{name}.assessment_basis")
            inspected = domain.get("inspected_locations", [])
            if not isinstance(inspected, list):
                raise ValidationError("inspected_locations must be an array")
            if domain["missing_reason"] == "access_unavailable":
                if any(d["kind"] == "fulltext" for d in docs):
                    raise ValidationError(
                        "full text is available; inspect it before claiming no access"
                    )
                if extraction["record_id"] not in workspace.load()["fulltext_attempts"]:
                    raise ValidationError(
                        "access_unavailable requires a recorded full-text attempt"
                    )
            elif not inspected:
                raise ValidationError("unreported/insufficient methods require inspected_locations")
            for loc in inspected:
                if not isinstance(loc, dict):
                    raise ValidationError("inspected location must be an object")
                doc = next((d for d in docs if d["document_id"] == loc.get("document_id")), None)
                if not doc or loc.get("locator") not in {s["locator"] for s in doc["segments"]}:
                    raise ValidationError("unknown inspected document/locator for appraisal")
    statuses = {d["status"] for d in row["domains"].values()}
    expected = (
        "pending"
        if "pending" in statuses
        else "limited"
        if "unavailable" in statuses
        else "complete"
    )
    if row["completion"] != expected:
        raise ValidationError(f"appraisal completion must be {expected} given its domains")
    if expected != "complete" and row["overall_judgment"] not in {"unclear", "not_assessable"}:
        raise ValidationError(
            "incomplete appraisal cannot assert a completed risk-of-bias judgment"
        )


def validate_contribution_v2(workspace: Workspace, finding: dict, contribution: dict) -> None:
    use = contribution.get("use")
    _choice(use, {"direct", "indirect", "context"}, "contribution use")
    require_text(contribution.get("alignment_rationale"), "alignment_rationale")
    extraction = workspace.index("extractions")[contribution["extraction_id"]]
    if use == "context" and contribution["relationship"] not in {"context", "incomparable"}:
        raise ValidationError(
            "context-only evidence cannot directly support or contradict an effect"
        )
    if use != "context" and finding["claim_basis"] == "comparative":
        if extraction["effect"]["basis"] != "between_group":
            raise ValidationError(
                "comparative claims require between_group effects; classify other data as context"
            )
        if use == "direct" and extraction["comparator_type"] != finding["comparator_type"]:
            raise ValidationError("different comparator types require indirect or context use")
    appraisal = workspace.index("appraisals")[extraction["extraction_id"]]
    if use != "context" and appraisal["completion"] == "pending":
        raise ValidationError(
            "substantive contributions require completed or explicitly limited assessment"
        )


def validate_finding(workspace: Workspace, finding: dict) -> None:
    outcomes = workspace.load()["protocol"]["outcomes"]
    for outcome in strings(finding, "protocol_outcomes"):
        if outcome not in outcomes:
            raise ValidationError(f"protocol_outcomes contains unknown outcome: {outcome}")
    _choice(
        finding.get("claim_basis"),
        {
            "comparative",
            "within_group",
            "association",
            "diagnostic_accuracy",
            "ranking",
            "context",
            "gap",
        },
        "claim_basis",
    )
    _choice(finding.get("comparator_type"), COMPARATORS, "finding comparator_type")
    if finding["claim_basis"] == "gap":
        require_text(finding.get("gap_reason"), "gap_reason")
        require_text(finding.get("gap_basis"), "gap_basis")
        if finding["certainty"]["rating"] not in {"not-assessable", "not-applicable"}:
            raise ValidationError("evidence gaps cannot have an assessed certainty rating")
    extractions = workspace.index("extractions")
    appraisals = workspace.index("appraisals")
    substantive = [
        appraisals[c["extraction_id"]] for c in finding["evidence"] if c["use"] != "context"
    ]
    if (
        substantive
        and all(a["completion"] != "complete" or a["method"] == "descriptive" for a in substantive)
        and finding["certainty"]["rating"] not in {"not-assessable", "not-applicable"}
    ):
        raise ValidationError(
            "no completed contributing appraisal: report certainty is not-assessable"
        )
    certainty = finding["certainty"]
    _choice(certainty.get("origin"), {"report_assessment", "source_authors"}, "certainty.origin")
    if certainty["origin"] == "source_authors":
        raise ValidationError(
            "finding certainty must be the report assessment; use "
            "published_certainty for author ratings"
        )
    for published in finding.get("published_certainty", []):
        if not isinstance(published, dict):
            raise ValidationError("published_certainty entries must be objects")
        extraction = _known(
            published.get("extraction_id"), extractions, "published certainty extraction"
        )
        require_text(published.get("rating"), "published rating")
        require_text(published.get("scope"), "published certainty scope")
        validate_location(workspace, published.get("source_location"), extraction["record_id"])
    overlap = object_field(finding, "overlap")
    _choice(
        overlap.get("status"),
        {"not_applicable", "mapped", "suspected", "unknown"},
        "overlap.status",
    )
    require_text(overlap.get("rationale"), "overlap.rationale")
    if overlap["status"] == "mapped":
        mappings = overlap.get("mappings")
        if not isinstance(mappings, list) or not mappings:
            raise ValidationError("mapped overlap requires source-grounded mappings")
        studies = workspace.index("studies")
        contributing_studies = {
            extractions[c["extraction_id"]]["study_id"] for c in finding["evidence"]
        }
        for mapping in mappings:
            if not isinstance(mapping, dict):
                raise ValidationError("overlap mappings must be objects")
            review = _known(mapping.get("review_study_id"), studies, "overlap review_study_id")
            if review["kind"] != "systematic-review":
                raise ValidationError("overlap review_study_id must identify a systematic review")
            if mapping["review_study_id"] not in contributing_studies:
                raise ValidationError("mapped review must contribute evidence to this finding")
            for sid in strings(mapping, "primary_study_ids"):
                if _known(sid, studies, "overlap primary_study_id")["kind"] != "primary":
                    raise ValidationError("overlap primary_study_ids must identify primary studies")
            _choice(mapping.get("scope"), {"review", "outcome"}, "overlap mapping scope")
            outcome = mapping.get("protocol_outcome")
            if mapping["scope"] == "outcome":
                if outcome not in finding["protocol_outcomes"]:
                    raise ValidationError("outcome overlap must name a finding protocol_outcome")
            elif outcome is not None:
                raise ValidationError("review-level membership cannot declare an outcome pool")
            location = object_field(mapping, "source_location")
            doc = _known(location.get("document_id"), workspace.source_index(), "document_id")
            if doc["record_id"] not in review["record_ids"]:
                raise ValidationError("overlap source must belong to the mapped review")
            validate_location(workspace, location, doc["record_id"])
    elif overlap.get("mappings"):
        raise ValidationError("overlap mappings require mapped status")


def validate_v2(workspace: Workspace, stage: str, payload: dict) -> None:
    if stage == "extractions":
        for row in payload["records"]:
            validate_extraction(workspace, row)
    elif stage == "appraisals":
        for row in payload["records"]:
            validate_appraisal(workspace, row)
    elif stage == "synthesis":
        for finding in payload["findings"]:
            validate_finding(workspace, finding)
    elif stage == "coverage":
        for row in payload["records"]:
            screening = _known(
                row.get("record_id"), workspace.index("screening"), "coverage record_id"
            )
            if screening["decision"] != "include":
                raise ValidationError("coverage describes included records only")
            _choice(
                row.get("selection"), {"selected", "deferred", "unavailable"}, "coverage selection"
            )
            require_text(row.get("reason"), "coverage reason")
            for outcome in strings(row, "protocol_outcomes", nonempty=False):
                if outcome not in workspace.load()["protocol"]["outcomes"]:
                    raise ValidationError("unknown coverage protocol outcome")
    elif stage == "reviews":
        findings = {f["finding_id"]: f for f in workspace.read("synthesis")["findings"]}
        for row in payload["records"]:
            finding = _known(row.get("finding_id"), findings, "review finding_id")
            if row.get("review_digest") != review_digest(workspace, finding):
                raise ValidationError("review_digest differs from the current claim and evidence")
            from .audit import validate_receipt

            validate_receipt(workspace, row)
            _choice(row.get("status"), {"pass", "revise"}, "claim review status")
            checks = object_field(row, "checks")
            if set(checks) != set(REVIEW_CHECKS):
                raise ValidationError(f"claim review requires checks: {', '.join(REVIEW_CHECKS)}")
            for name, item in checks.items():
                if not isinstance(item, dict):
                    raise ValidationError(f"review {name} must be an object")
                _choice(
                    item.get("status"),
                    {"pass", "revise", "not_applicable"},
                    f"review {name} status",
                )
                require_text(item.get("rationale"), f"review {name} rationale")
            if row["status"] == "pass" and any(c["status"] == "revise" for c in checks.values()):
                raise ValidationError("a review with required revisions cannot pass")
        if "report_reviews" in payload or "audit_contract_version" in payload:
            from .audit import validate_report_receipts

            validate_report_receipts(workspace, payload)


def readiness(workspace: Workspace) -> list[str]:
    """Completion constraints beyond the validation of individual submissions."""
    warnings = []
    screening = workspace.index("screening")
    coverage = workspace.index("coverage")
    included = {rid for rid, s in screening.items() if s["decision"] == "include"}
    if set(coverage) != included:
        raise ValidationError("record detailed-assessment selection for every included record")
    extracted = {e["record_id"] for e in workspace.rows("extractions")}
    for rid, item in coverage.items():
        if item["selection"] == "selected" and rid not in extracted:
            raise ValidationError(
                f"selected record {rid} has no extraction; assess or explain unavailable evidence"
            )
        if item["selection"] != "selected":
            warnings.append(f"Detailed assessment {item['selection']} for {rid}: {item['reason']}")
    pending = [
        a["extraction_id"] for a in workspace.rows("appraisals") if a["completion"] == "pending"
    ]
    if pending:
        raise ValidationError(
            "finish pending appraisals before finalization: " + ", ".join(pending)
        )
    limited = sum(a["completion"] == "limited" for a in workspace.rows("appraisals"))
    if limited:
        warnings.append(f"{limited} result appraisals are limited by unavailable methods/details.")
    findings = workspace.read("synthesis")["findings"]
    covered = {o for f in findings for o in f["protocol_outcomes"]}
    missing = set(workspace.load()["protocol"]["outcomes"]) - covered
    if missing:
        raise ValidationError(
            "address each protocol outcome with findings or explicit gaps: "
            + ", ".join(sorted(missing))
        )
    reviews = workspace.index("reviews")
    if set(reviews) != {f["finding_id"] for f in findings}:
        raise ValidationError("record a separate claim review for every finding")
    if any(r["status"] != "pass" for r in reviews.values()):
        raise ValidationError("resolve required claim revisions before finalization")
    review_payload = workspace.read("reviews")
    from .audit import validate_report_receipts

    validate_report_receipts(workspace, review_payload)
    if any(row["status"] != "pass" for row in review_payload["report_reviews"]):
        raise ValidationError("resolve required report revisions before finalization")
    if any(f["claim_basis"] == "gap" for f in findings):
        warnings.append("One or more protocol outcomes have an explicit evidence gap.")
    if any(f["certainty"]["rating"] == "not-assessable" for f in findings):
        warnings.append("Report-level certainty could not be assessed for one or more findings.")
    return warnings
