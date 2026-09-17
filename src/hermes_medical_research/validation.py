"""Validate evidence references and completeness, without pretending to judge a paper."""

from __future__ import annotations

import hashlib
import math
from typing import Any

from hermes_medical_research.search.models import ValidationError

from .workspace import Workspace, normalized_text, require_text

METHODS = {
    "robis": ("2016", "eligibility identification data_collection synthesis"),
    "rob2": ("2019-08-22", "randomization deviations missing_data measurement selection"),
    "rob2-cluster": (
        "2021-03-18",
        "randomization recruitment deviations missing_data measurement selection",
    ),
    "rob2-crossover": (
        "2021-03-18",
        "randomization carryover deviations missing_data measurement selection",
    ),
    "robins-i": (
        "2016",
        "confounding selection classification deviations "
        "missing_data measurement selection_reporting",
    ),
    "robins-e": (
        "2024",
        "confounding exposure selection post_exposure missing_data measurement selection_reporting",
    ),
    "quadas3": ("1.2", "participants index_test target_condition analysis"),
    "quips": (
        "2013",
        "participation attrition factor_measurement "
        "outcome_measurement confounding analysis_reporting",
    ),
    "probast-ai": ("2025", "participants predictors outcome analysis"),
    "descriptive": ("1", "limitations applicability"),
}
GRADE_DOMAINS = ("risk_of_bias", "inconsistency", "indirectness", "imprecision", "publication_bias")
RELATIONSHIPS = {"supports", "contradicts", "mixed", "incomparable", "context"}
SCOPE_FIELDS = ("population", "comparison", "outcome", "timepoint")
DISPOSITION_STATUSES = ("extracted", "not_reported", "not_applicable")
TEST_STATISTIC_TERMS = (
    "chi-square",
    "chi square",
    "chi2",
    "χ2",
    "χ²",
    "t statistic",
    "t-statistic",
    "t value",
    "t-value",
    "f statistic",
    "f-statistic",
    "z statistic",
    "z-statistic",
    "test statistic",
    "p value",
    "p-value",
)


def _choice(value: Any, choices: set[str], name: str) -> None:
    if not isinstance(value, str) or value not in choices:
        raise ValidationError(f"{name} must be one of {', '.join(sorted(choices))}")


def _known(value: Any, index: dict[str, Any], name: str) -> Any:
    if not isinstance(value, str) or value not in index:
        raise ValidationError(f"unknown {name}: {value!r}")
    return index[value]


def validate_location(workspace: Workspace, location: Any, record_id: str) -> dict[str, Any]:
    if not isinstance(location, dict):
        raise ValidationError("source_location must contain document_id, locator, and quote")
    doc = _known(location.get("document_id"), workspace.source_index(), "document_id")
    if doc["record_id"] != record_id:
        raise ValidationError("source document belongs to another record")
    segments = {s["locator"]: s["text"] for s in doc["segments"]}
    text = _known(location.get("locator"), segments, "source locator")
    quote = require_text(location.get("quote"), "source quote")
    if normalized_text(quote) not in normalized_text(text):
        raise ValidationError("source quote does not occur at the recorded location")
    return doc


def validate_inspected(
    workspace: Workspace, record_id: str, locations: Any, name: str
) -> None:
    """Require real, non-metadata locations of this record that the author says they read."""
    if not isinstance(locations, list) or not locations:
        raise ValidationError(f"{name} must list the document_id and locator you inspected")
    documents = {
        document["document_id"]: document
        for document in workspace.source_documents()
        if document["record_id"] == record_id and document["kind"] != "metadata"
    }
    for location in locations:
        if not isinstance(location, dict):
            raise ValidationError(f"{name} entries must be objects with document_id and locator")
        document = documents.get(location.get("document_id"))
        if document is None:
            raise ValidationError(
                f"{name} names an unknown or metadata-only document: "
                f"{location.get('document_id')!r}"
            )
        if location.get("locator") not in {s["locator"] for s in document["segments"]}:
            raise ValidationError(
                f"{name} names an unknown locator: {location.get('locator')!r}"
            )


def validate_dispositions(workspace: Workspace, rows: list[dict[str, Any]]) -> None:
    """Every assessed record decides every protocol outcome exactly once."""
    records = workspace.index("records")
    screening = workspace.index("screening")
    studies = workspace.index("studies")
    extractions = workspace.rows("extractions")
    outcomes = workspace.load()["protocol"]["outcomes"]
    for row in rows:
        rid = row.get("record_id")
        _known(rid, records, "record_id")
        if _known(rid, screening, "screened record")["decision"] != "include":
            raise ValidationError("outcome dispositions describe included records only")
        study = _known(row.get("study_id"), studies, "study_id")
        if rid not in study["record_ids"]:
            raise ValidationError("disposition record is not linked to its study")
        items = row.get("outcomes")
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise ValidationError("dispositions.outcomes must be an array of objects")
        named = [item.get("protocol_outcome") for item in items]
        missing = [outcome for outcome in outcomes if outcome not in named]
        unknown = [str(value) for value in named if value not in outcomes]
        duplicates = sorted({str(value) for value in named if named.count(value) > 1})
        if missing or unknown or duplicates:
            detail = "; ".join(
                part
                for part in (
                    "missing: " + ", ".join(missing) if missing else "",
                    "unknown: " + ", ".join(unknown) if unknown else "",
                    "duplicated: " + ", ".join(duplicates) if duplicates else "",
                )
                if part
            )
            raise ValidationError(
                f"dispositions must decide every protocol outcome exactly once ({detail})"
            )
        bound: dict[str, set[str]] = {}
        for extraction in extractions:
            if extraction["record_id"] == rid:
                bound.setdefault(str(extraction.get("protocol_outcome")), set()).add(
                    extraction["extraction_id"]
                )
        for item in items:
            outcome = item["protocol_outcome"]
            status = item.get("status")
            if status is None or status == "":
                raise ValidationError(
                    f"outcome {outcome!r} is undecided; set status to "
                    + ", ".join(DISPOSITION_STATUSES)
                )
            _choice(status, set(DISPOSITION_STATUSES), f"outcome {outcome!r} status")
            ids = item.get("extraction_ids")
            if not isinstance(ids, list) or not all(isinstance(value, str) for value in ids):
                raise ValidationError(f"outcome {outcome!r} extraction_ids must be an array")
            expected = bound.get(outcome, set())
            if status == "extracted":
                if not expected:
                    raise ValidationError(
                        f"outcome {outcome!r} is extracted but no extraction row has "
                        f"protocol_outcome {outcome!r}"
                    )
                if set(ids) != expected:
                    raise ValidationError(
                        f"outcome {outcome!r} extraction_ids must equal the rows bound to it: "
                        + ", ".join(sorted(expected))
                    )
            else:
                if expected:
                    raise ValidationError(
                        f"outcome {outcome!r} is {status} but extraction rows are bound to it: "
                        + ", ".join(sorted(expected))
                    )
                if ids:
                    raise ValidationError(
                        f"outcome {outcome!r} is {status}; extraction_ids must be empty"
                    )
                require_text(item.get("rationale"), f"outcome {outcome!r} rationale")
                validate_inspected(
                    workspace,
                    rid,
                    item.get("inspected_locations"),
                    f"outcome {outcome!r} inspected_locations",
                )


def validate_stage(workspace: Workspace, stage: str, payload: dict[str, Any]) -> None:
    records = workspace.index("records")
    rows = payload.get("records", [])
    if stage == "documents":
        for row in rows:
            _known(row.get("record_id"), records, "record_id")
            _choice(
                row.get("kind"), {"abstract", "metadata", "registry", "fulltext"}, "document kind"
            )
            segments = row.get("segments")
            if not isinstance(segments, list) or not segments:
                raise ValidationError("documents need source segments")
            locators = set()
            for segment in segments:
                if not isinstance(segment, dict):
                    raise ValidationError("source segment must be an object")
                locator = require_text(segment.get("locator"), "segment locator")
                require_text(segment.get("text"), "segment text")
                if locator in locators:
                    raise ValidationError("duplicate document locator")
                locators.add(locator)
            if row["kind"] == "fulltext":
                path = workspace.path / require_text(row.get("file"), "full-text file")
                if not path.resolve().is_relative_to(workspace.path):
                    raise ValidationError("full-text path escapes workspace")
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != row.get(
                    "sha256"
                ):
                    raise ValidationError("full-text file is missing or its digest changed")
    elif stage == "screening":
        for row in rows:
            _known(row.get("record_id"), records, "record_id")
            _choice(row.get("decision"), {"include", "exclude", "uncertain"}, "screening decision")
            _choice(row.get("basis"), {"title-abstract", "fulltext", "registry"}, "screening basis")
            require_text(row.get("reason"), "screening reason")
            if row["basis"] == "fulltext":
                docs = workspace.rows("documents")
                if not any(
                    d["record_id"] == row["record_id"] and d["kind"] == "fulltext" for d in docs
                ):
                    raise ValidationError("full-text screening requires a stored full text")
    elif stage == "studies":
        assigned: set[str] = set()
        for row in rows:
            require_text(row.get("basis"), "study-link basis")
            ids = row.get("record_ids")
            if not isinstance(ids, list) or not ids:
                raise ValidationError("a study must link at least one record")
            for rid in ids:
                _known(rid, records, "record_id")
                if rid in assigned:
                    raise ValidationError("a record cannot belong to two study groups")
                assigned.add(rid)
            _choice(
                row.get("kind"),
                {"primary", "systematic-review", "guideline", "other"},
                "study kind",
            )
    elif stage == "extractions":
        screening, studies = workspace.index("screening"), workspace.index("studies")
        for row in rows:
            rid = row.get("record_id")
            record = _known(rid, records, "record_id")
            decision = _known(rid, screening, "screened record")
            if decision["decision"] != "include":
                raise ValidationError("only included records may contribute extractions")
            if record.get("is_retracted"):
                raise ValidationError("retracted records cannot supply evidence")
            study = _known(row.get("study_id"), studies, "study_id")
            if rid not in study["record_ids"]:
                raise ValidationError("extraction record is not linked to its study")
            if workspace.outcome_contract or "protocol_outcome" in row:
                _choice(
                    row.get("protocol_outcome"),
                    set(workspace.load()["protocol"]["outcomes"]),
                    "protocol_outcome",
                )
            for name in SCOPE_FIELDS:
                require_text(row.get(name), name)
            require_text(row.get("result"), "reported result")
            require_text(row.get("support_rationale"), "support_rationale")
            if row.get("support_checked") is not True:
                raise ValidationError(
                    "the host must check that the source supports the extracted result"
                )
            doc = validate_location(workspace, row.get("source_location"), rid)
            if doc["kind"] == "metadata":
                raise ValidationError("bibliographic metadata alone cannot support a result")
            effect = row.get("effect")
            if not isinstance(effect, dict):
                raise ValidationError(
                    "effect must record measure, value, ci_low, ci_high, and units"
                )
            for field in ("measure", "units"):
                require_text(effect.get(field), f"effect.{field}")
            if workspace.outcome_contract and any(
                term in effect["measure"].casefold() for term in TEST_STATISTIC_TERMS
            ):
                raise ValidationError(
                    "effect.measure must be an effect estimate such as a mean difference, risk "
                    "ratio, or proportion; put test statistics and P values in result and use "
                    "null with effect.missing_reason when no estimate is reported"
                )
            for name in ("value", "ci_low", "ci_high"):
                if name not in effect:
                    raise ValidationError(f"effect.{name} is required; use null when unreported")
                value = effect[name]
                if value is not None and (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                ):
                    raise ValidationError(f"effect.{name} must be a finite number or null")
            low, high = effect["ci_low"], effect["ci_high"]
            if low is not None and high is not None and low > high:
                raise ValidationError("confidence interval is reversed")
            if effect["value"] is None:
                require_text(effect.get("missing_reason"), "effect.missing_reason")
            size = row.get("sample_size")
            if size is not None and (
                isinstance(size, bool) or not isinstance(size, int) or size < 1
            ):
                raise ValidationError("sample_size must be positive or null")
            _choice(
                row.get("favors"),
                {"intervention", "comparator", "neither", "uncertain", "not-applicable"},
                "favors",
            )
            require_text(row.get("direction_rationale"), "direction_rationale")
    elif stage == "appraisals":
        extractions = workspace.index("extractions")
        for row in rows:
            extraction = _known(row.get("extraction_id"), extractions, "extraction_id")
            method = row.get("method")
            _choice(method, set(METHODS), "appraisal method")
            version, domains = METHODS[method]
            if row.get("method_version") != version:
                raise ValidationError(f"{method} uses method_version {version}")
            require_text(row.get("overall"), "overall appraisal")
            require_text(row.get("rationale"), "appraisal rationale")
            values = row.get("domains")
            if not isinstance(values, dict) or set(values) != set(domains.split()):
                raise ValidationError(f"{method} requires domains: {domains}")
            for name, item in values.items():
                if not isinstance(item, dict):
                    raise ValidationError(f"domain {name} must be an object")
                require_text(item.get("judgment"), "domain judgment")
                require_text(item.get("rationale"), "domain rationale")
                locations = item.get("source_locations", [])
                if not isinstance(locations, list):
                    raise ValidationError("source_locations must be an array")
                if item["judgment"] != "not_assessed" and not locations:
                    raise ValidationError(
                        "assessed domains need source locations; otherwise use not_assessed"
                    )
                for location in locations:
                    validate_location(workspace, location, extraction["record_id"])
    elif stage == "dispositions":
        if not workspace.outcome_contract:
            raise ValidationError(
                "dispositions require the per-outcome assessment contract; this Run predates it"
            )
        validate_dispositions(workspace, rows)
    elif stage == "synthesis":
        validate_synthesis(workspace, payload)
    elif stage not in {"records", "documents", "coverage", "reviews"}:
        raise ValidationError("unknown stage")
    if workspace.evidence_version == "2":
        from .evidence import validate_v2

        validate_v2(workspace, stage, payload)


def validate_contribution(workspace: Workspace, finding: dict, contribution: Any) -> None:
    """Check one contribution in the context of its unchanged, complete finding."""
    if not isinstance(contribution, dict):
        raise ValidationError("evidence contribution must be an object")
    eid = contribution.get("extraction_id")
    extraction = _known(eid, workspace.index("extractions"), "extraction_id")
    _known(eid, workspace.index("appraisals"), "appraised extraction")
    _choice(contribution.get("relationship"), RELATIONSHIPS, "evidence relationship")
    require_text(contribution.get("weight_rationale"), "weight_rationale")
    if any(extraction[field] != finding[field] for field in SCOPE_FIELDS):
        require_text(
            contribution.get("alignment_rationale"), "alignment_rationale for differing scopes"
        )
    if contribution.get("claim_support_checked") is not True:
        raise ValidationError(
            "host must check each finding against its contributing source evidence"
        )
    if workspace.evidence_version == "2":
        from .evidence import validate_contribution_v2

        validate_contribution_v2(workspace, finding, contribution)


def validate_synthesis(workspace: Workspace, payload: dict[str, Any]) -> None:
    require_text(payload.get("title"), "report title")
    findings = payload.get("findings")
    if not isinstance(findings, list):
        raise ValidationError("synthesis.findings must be an array")
    if not isinstance(payload.get("limitations"), list):
        raise ValidationError("synthesis.limitations must be an array")
    for limitation in payload["limitations"]:
        require_text(limitation, "synthesis limitation")
    seen: set[str] = set()
    for finding in findings:
        if not isinstance(finding, dict):
            raise ValidationError("finding must be an object")
        fid = require_text(finding.get("finding_id"), "finding_id")
        if fid in seen:
            raise ValidationError("duplicate finding_id")
        seen.add(fid)
        require_text(finding.get("conclusion"), "finding conclusion")
        for field in SCOPE_FIELDS:
            require_text(finding.get(field), field)
        evidence = finding.get("evidence")
        if not isinstance(evidence, list) or (not evidence and finding.get("claim_basis") != "gap"):
            raise ValidationError("each finding needs linked evidence or an explicit evidence gap")
        refs: set[str] = set()
        for contribution in evidence:
            validate_contribution(workspace, finding, contribution)
            eid = contribution.get("extraction_id")
            if eid in refs:
                raise ValidationError("duplicate evidence contribution")
            refs.add(eid)
        certainty = finding.get("certainty")
        if not isinstance(certainty, dict):
            raise ValidationError("finding needs a certainty assessment")
        _choice(
            certainty.get("framework"), {"GRADE-informed", "descriptive"}, "certainty framework"
        )
        _choice(
            certainty.get("rating"),
            {"high", "moderate", "low", "very-low", "not-assessable", "not-applicable"},
            "certainty rating",
        )
        require_text(certainty.get("rationale"), "certainty rationale")
        if certainty["framework"] == "GRADE-informed":
            if certainty["rating"] == "not-applicable":
                raise ValidationError("GRADE-informed certainty cannot be not-applicable")
            domains = certainty.get("domains")
            if not isinstance(domains, dict) or set(domains) != set(GRADE_DOMAINS):
                raise ValidationError("GRADE-informed certainty needs all five domains")
            for item in domains.values():
                require_text(item, "GRADE domain rationale")
            require_text(certainty.get("starting_point"), "GRADE starting_point")
            require_text(
                certainty.get("rating_explanation"), "GRADE downgrading/upgrading explanation"
            )
        elif certainty["rating"] != "not-applicable":
            raise ValidationError("descriptive maps use not-applicable certainty")


def validate_complete(workspace: Workspace) -> list[str]:
    manifest = workspace.load()
    for stage in workspace.required_stages:
        if stage not in manifest["datasets"]:
            raise ValidationError(f"{stage} is missing")
        payload = workspace.read(stage)
        validate_stage(workspace, stage, payload)
    records, screening = workspace.index("records"), workspace.index("screening")
    if set(records) != set(screening):
        raise ValidationError("record a screening decision for every retrieved record")
    warnings = []
    for rid, attempt in manifest["fulltext_attempts"].items():
        if attempt["status"] != "available":
            detail = str(attempt.get("error") or "attempt interrupted; resume acquisition")[:500]
            warnings.append(f"Full text {attempt['status']} for {rid}: {detail}")
    if not records:
        warnings.append("No records were retrieved; this does not establish absence of evidence.")
    uncertain = sum(row["decision"] == "uncertain" for row in screening.values())
    if uncertain:
        warnings.append(f"{uncertain} records have uncertain eligibility and need human review.")
    extractions = workspace.rows("extractions")
    if {e["extraction_id"] for e in extractions} != set(workspace.index("appraisals")):
        raise ValidationError("record an appraisal for every extraction")
    covered = {e["record_id"] for e in extractions}
    if workspace.outcome_contract:
        covered |= set(workspace.index("dispositions"))
    unassessed = [
        rid for rid, row in screening.items() if row["decision"] == "include" and rid not in covered
    ]
    if unassessed:
        warnings.append(
            f"{len(unassessed)} included records have no extracted results; "
            "synthesis coverage is incomplete."
        )
    for entry in manifest["searches"].values():
        if entry["status"] == "reserved":
            warnings.append("A planned search has not been attached; retrieval is incomplete.")
            continue
        snapshot = workspace.store.read_json(entry["snapshot"])
        from .workspace import digest

        if digest(snapshot) != entry["snapshot_digest"]:
            raise ValidationError("search snapshot digest mismatch")
        for source, info in snapshot["manifest"]["sources"].items():
            if info["status"] != "complete":
                warnings.append(f"{source} was {info['status']}: {info.get('error')}")
            if info.get("truncated"):
                warnings.append(f"{source} retrieval was capped or truncated.")
    if workspace.evidence_version == "2":
        from .evidence import readiness

        warnings.extend(readiness(workspace))
    return list(dict.fromkeys(warnings))
