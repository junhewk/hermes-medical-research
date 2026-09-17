"""Digest-bound, host-neutral audit targets and receipt validation."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from hermes_medical_research.search.artifacts import canonical_json
from hermes_medical_research.search.models import ValidationError

from .evidence import REVIEW_CHECKS, review_digest
from .workspace import Workspace, digest, normalized_text

AUDIT_CONTRACT_VERSION = "1"
VERDICTS = {"supported", "unsupported", "uncertain"}
CHECK_STATUSES = {"pass", "revise", "not_applicable"}


class AuditContentError(ValidationError):
    """An audit proposal failed deterministic content validation."""

    def __init__(self, problems: list[dict[str, Any]]):
        self.problems = problems
        messages = list(dict.fromkeys(str(item["message"]) for item in problems))
        super().__init__("; ".join(messages) or "invalid audit result")


def _problem(code: str, message: str, **detail: Any) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        **{key: value for key, value in detail.items() if value is not None},
    }


def candidate(workspace: Workspace) -> dict[str, Any]:
    """Freeze every report input except the audit stage itself."""
    manifest = workspace.load()
    stages = {
        stage: workspace.read(stage)
        for stage in workspace.required_stages
        if stage != "reviews" and stage in manifest["datasets"]
    }
    required = set(workspace.required_stages) - {"reviews"}
    missing = sorted(required - set(stages))
    if missing:
        raise ValidationError("audit candidate is incomplete: " + ", ".join(missing))
    stages["documents"] = {
        **stages["documents"],
        "records": workspace.source_documents(),
    }
    searches: dict[str, Any] = {}
    for key, entry in manifest["searches"].items():
        item: dict[str, Any] = {"entry": entry}
        if entry["status"] == "attached":
            snapshot = workspace.store.read_json(entry["snapshot"])
            if digest(snapshot) != entry["snapshot_digest"]:
                raise ValidationError("search snapshot was modified")
            item["execution"] = {
                name: snapshot.get(name)
                for name in ("strategy", "manifest", "summary", "approval", "preflight")
            }
        searches[key] = item
    return {
        "protocol": manifest["protocol"],
        "workflow": {
            "searches": searches,
            "fulltext_attempts": manifest["fulltext_attempts"],
        },
        "stages": stages,
    }


def _target(
    *,
    prefix: str,
    kind: str,
    entity_id: str,
    field: str,
    paths: list[str],
    value: Any,
    candidate_digest: str,
    check: str | None = None,
) -> dict[str, Any]:
    body = {
        "kind": kind,
        "entity_id": entity_id,
        "field": field,
        "paths": paths,
        "assertion": canonical_json(value),
    }
    result = {
        "target_id": f"{prefix}-{digest(body)[:20]}",
        **body,
        "review_digest": digest({"candidate_digest": candidate_digest, **body}),
    }
    if check is not None:
        result["check"] = check
    return result


def build_review_targets(
    frozen: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Build exhaustive target sets whose IDs and assertions are candidate-bound."""
    candidate_digest = digest(frozen)
    stages = frozen["stages"]
    protocol = frozen["protocol"]
    documents = stages["documents"]["records"]
    documents_by_record: dict[str, list[str]] = {}
    for document in documents:
        documents_by_record.setdefault(document["record_id"], []).append(
            document["document_id"]
        )
    report_targets: list[dict[str, Any]] = []
    finding_targets: list[dict[str, Any]] = []

    protocol_target = _target(
        prefix="report",
        kind="protocol",
        entity_id="protocol",
        field="protocol",
        paths=["/protocol"],
        value=protocol,
        candidate_digest=candidate_digest,
    )
    protocol_target["result_group"] = "report-narrative"
    protocol_target["allowed_document_ids"] = []
    report_targets.append(protocol_target)

    synthesis = stages["synthesis"]
    narrative_target = _target(
        prefix="report",
        kind="report",
        entity_id="report",
        field="synthesis.report",
        paths=["/stages/synthesis/title", "/stages/synthesis/limitations"],
        value={"title": synthesis["title"], "limitations": synthesis["limitations"]},
        candidate_digest=candidate_digest,
    )
    narrative_target["result_group"] = "report-narrative"
    narrative_target["allowed_document_ids"] = [
        document["document_id"] for document in documents
    ]
    report_targets.append(narrative_target)

    extraction_records = {
        row["extraction_id"]: row["record_id"] for row in stages["extractions"]["records"]
    }
    id_fields = {
        "screening": "record_id",
        "studies": "study_id",
        "extractions": "extraction_id",
        "appraisals": "extraction_id",
        "dispositions": "record_id",
        "coverage": "record_id",
    }
    for stage in ("screening", "studies", "extractions", "appraisals", "dispositions", "coverage"):
        if stage not in stages:
            # Runs from before per-outcome dispositions have no such stage to audit.
            continue
        for index, row in enumerate(stages[stage]["records"]):
            entity_id = str(row.get(id_fields[stage]) or index)
            target = _target(
                prefix="report",
                kind=stage,
                entity_id=entity_id,
                field=f"stages.{stage}.records[{index}]",
                paths=[f"/stages/{stage}/records/{index}"],
                value=row,
                candidate_digest=candidate_digest,
            )
            target.update(
                requires_sources=stage in {"extractions", "appraisals"},
                # An absence claim is checked against full text; titles cannot settle it.
                allow_metadata_only=stage in {"screening", "studies", "coverage"},
            )
            if stage == "studies":
                group_kind, group_id = "study", entity_id
            elif stage == "extractions":
                group_kind, group_id = "record", row["record_id"]
            elif stage == "appraisals":
                group_kind, group_id = "record", extraction_records[entity_id]
            else:
                group_kind, group_id = "record", row["record_id"]
            if stage == "studies":
                allowed = [
                    document_id
                    for record_id in row["record_ids"]
                    for document_id in documents_by_record.get(record_id, [])
                ]
            else:
                record_id = (
                    extraction_records[entity_id]
                    if stage == "appraisals"
                    else row["record_id"]
                )
                allowed = documents_by_record.get(record_id, [])
            target["allowed_document_ids"] = allowed
            target["result_group"] = f"report-{group_kind}-{digest([group_kind, group_id])[:12]}"
            report_targets.append(target)

    for index, finding in enumerate(synthesis["findings"]):
        values = {
            "estimates": {
                "outcome": finding.get("outcome"),
                "conclusion": finding.get("conclusion"),
            },
            "scope": {
                key: finding.get(key)
                for key in (
                    "protocol_outcomes",
                    "population",
                    "comparison",
                    "outcome",
                    "timepoint",
                    "claim_basis",
                    "comparator_type",
                    "gap_reason",
                    "gap_basis",
                )
                if key in finding
            },
            "harms": {
                "conclusion": finding.get("conclusion"),
                "evidence": finding.get("evidence"),
            },
            "overlap": finding.get("overlap"),
            "certainty": {
                "certainty": finding.get("certainty"),
                "published_certainty": finding.get("published_certainty", []),
                "evidence": finding.get("evidence"),
            },
        }
        for check, value in values.items():
            target = _target(
                prefix="finding",
                kind="finding",
                entity_id=finding["finding_id"],
                field=f"stages.synthesis.findings[{index}].{check}",
                paths=[f"/stages/synthesis/findings/{index}"],
                value=value,
                candidate_digest=candidate_digest,
                check=check,
            )
            target["allow_metadata_only"] = check == "scope"
            extraction_ids = {
                contribution["extraction_id"]
                for contribution in finding.get("evidence", [])
                if isinstance(contribution, dict)
                and isinstance(contribution.get("extraction_id"), str)
            }
            target["allowed_document_ids"] = list(
                dict.fromkeys(
                    row["source_location"]["document_id"]
                    for row in stages["extractions"]["records"]
                    if row["extraction_id"] in extraction_ids
                )
            )
            finding_targets.append(target)

    membership_targets: list[dict[str, Any]] = []
    studies = {row["study_id"]: row for row in stages["studies"]["records"]}
    for finding_index, finding in enumerate(synthesis["findings"]):
        mappings = finding.get("overlap", {}).get("mappings") or []
        for mapping_index, mapping in enumerate(mappings):
            review = studies[mapping["review_study_id"]]
            allowed = [
                row["document_id"]
                for row in documents
                if row["record_id"] in review["record_ids"] and row.get("kind") != "metadata"
            ]
            for primary_study_id in mapping["primary_study_ids"]:
                value = {
                    "review_study_id": mapping["review_study_id"],
                    "primary_study_id": primary_study_id,
                    "declared_scope": mapping["scope"],
                    "protocol_outcome": mapping.get("protocol_outcome"),
                }
                target = _target(
                    prefix="membership",
                    kind="outcome_membership",
                    entity_id=f"{finding['finding_id']}:{mapping_index}:{primary_study_id}",
                    field=(
                        f"stages.synthesis.findings[{finding_index}].overlap."
                        f"mappings[{mapping_index}]"
                    ),
                    paths=[
                        f"/stages/synthesis/findings/{finding_index}/overlap/mappings/"
                        f"{mapping_index}"
                    ],
                    value=value,
                    candidate_digest=candidate_digest,
                    check="overlap",
                )
                target.update(
                    finding_id=finding["finding_id"],
                    declared_scope=mapping["scope"],
                    allowed_document_ids=allowed,
                )
                membership_targets.append(target)
    return report_targets, finding_targets, membership_targets


def _observation_template(target: dict[str, Any]) -> dict[str, Any]:
    result = {
        "target_id": target["target_id"],
        "field": target["field"],
        "assertion": target["assertion"],
        "verdict": "uncertain",
        "rationale": "",
        "sources": [],
    }
    if target.get("check"):
        result["check"] = target["check"]
    return result


def audit_groups(workspace: Workspace) -> tuple[str, list[dict[str, Any]]]:
    """Return bounded, exhaustive audit groups for the current candidate."""
    frozen = candidate(workspace)
    candidate_digest = digest(frozen)
    report_targets, finding_targets, membership_targets = build_review_targets(frozen)
    findings = {row["finding_id"]: row for row in frozen["stages"]["synthesis"]["findings"]}
    groups: list[dict[str, Any]] = []
    for finding_id, finding in findings.items():
        targets = [row for row in finding_targets if row["entity_id"] == finding_id]
        for target in targets:
            target["requires_sources"] = bool(finding.get("evidence")) and target.get(
                "check"
            ) != "overlap"
        memberships = [row for row in membership_targets if row["finding_id"] == finding_id]
        allowed_document_ids = list(
            dict.fromkeys(
                document_id
                for target in [*targets, *memberships]
                for document_id in target.get("allowed_document_ids", [])
            )
        )
        groups.append(
            {
                "group_id": f"finding:{finding_id}",
                "kind": "finding",
                "entity_id": finding_id,
                "targets": targets,
                "membership_targets": memberships,
                "allowed_document_ids": allowed_document_ids,
                "proposal": {
                    "schema_version": AUDIT_CONTRACT_VERSION,
                    "record": {
                        "finding_id": finding_id,
                        "review_digest": review_digest(workspace, finding),
                        "status": "revise",
                        "checks": {
                            check: {"status": "revise", "rationale": ""} for check in REVIEW_CHECKS
                        },
                        "observations": [_observation_template(target) for target in targets],
                        "membership_assessments": [
                            {
                                "target_id": target["target_id"],
                                "review_digest": target["review_digest"],
                                "supported_scope": "unknown",
                                "rationale": "",
                                "sources": [],
                            }
                            for target in memberships
                        ],
                    },
                },
            }
        )
    for target in report_targets:
        groups.append(
            {
                "group_id": f"report:{target['target_id']}",
                "kind": "report",
                "entity_id": target["target_id"],
                "targets": [target],
                "membership_targets": [],
                "allowed_document_ids": target["allowed_document_ids"],
                "proposal": {
                    "schema_version": AUDIT_CONTRACT_VERSION,
                    "report_review": {
                        "target_id": target["target_id"],
                        "review_digest": target["review_digest"],
                        "result_group": target["result_group"],
                        "status": "revise",
                        "observations": [_observation_template(target)],
                    },
                },
            }
        )
    return candidate_digest, groups


def citation_contract(workspace: Workspace) -> dict[str, Any]:
    return {
        "rules": [
            "Cite a document_id and locator returned by mdr source show.",
            "The quote must occur verbatim at that locator, ignoring whitespace differences.",
            "Metadata titles support identity/design labels, not effects, safety, bias, or "
            "outcome membership.",
            "Use no sources only for protocol/workflow decisions or an explicit evidence gap.",
        ],
        "source_access": (
            "Use source_find to locate text in allowed documents and source_read to quote one "
            "locator; source_show pages logical records."
        ),
    }


def _validate_sources(
    problems: list[dict[str, Any]],
    item: dict[str, Any],
    documents: dict[str, dict[str, Any]],
    *,
    context: dict[str, Any],
    allowed: set[str] | None = None,
    required: bool = False,
    allow_metadata_only: bool = True,
) -> None:
    sources = item.get("sources")
    if not isinstance(sources, list):
        problems.append(_problem("sources", "sources must be an array", **context))
        return
    if required and not sources:
        problems.append(
            _problem("source_required", "this target requires source evidence", **context)
        )
    substantive = False
    recognized = False
    for source in sources:
        if not isinstance(source, dict):
            problems.append(_problem("source", "each source must be an object", **context))
            continue
        document_id = source.get("document_id")
        document = documents.get(document_id) if isinstance(document_id, str) else None
        if document is None:
            problems.append(
                _problem(
                    "unknown_document", "unknown audit document", document_id=document_id, **context
                )
            )
            continue
        if allowed is not None and document_id not in allowed:
            problems.append(
                _problem(
                    "source_scope", "document is outside this target's allowed sources", **context
                )
            )
            continue
        recognized = True
        substantive = substantive or document.get("kind") != "metadata"
        segments = {
            segment["locator"]: normalized_text(segment["text"])
            for segment in document.get("segments", [])
        }
        locator = source.get("locator")
        if locator not in segments:
            problems.append(
                _problem("unknown_locator", "unknown source locator", locator=locator, **context)
            )
            continue
        quote = source.get("quote")
        if (
            not isinstance(quote, str)
            or not quote.strip()
            or normalized_text(quote) not in segments[locator]
        ):
            problems.append(
                _problem("quote_mismatch", "source quote does not occur at the locator", **context)
            )
    if sources and recognized and not allow_metadata_only and not substantive:
        problems.append(
            _problem("metadata_scope", "metadata alone cannot support this assertion", **context)
        )


def _validate_observation(
    problems: list[dict[str, Any]],
    item: Any,
    target: dict[str, Any],
    documents: dict[str, dict[str, Any]],
) -> None:
    context = {"target_id": target["target_id"]}
    if not isinstance(item, dict):
        problems.append(_problem("observation", "observation must be an object", **context))
        return
    for key in ("target_id", "field", "assertion"):
        if item.get(key) != target[key]:
            problems.append(
                _problem("target_mismatch", f"observation {key} differs from its target", **context)
            )
    if target.get("check") and item.get("check") != target["check"]:
        problems.append(_problem("target_mismatch", "observation check differs", **context))
    if item.get("verdict") not in VERDICTS:
        problems.append(
            _problem("verdict", "verdict must be supported, unsupported, or uncertain", **context)
        )
    if not isinstance(item.get("rationale"), str) or not item["rationale"].strip():
        problems.append(_problem("rationale", "audit rationale must be nonempty", **context))
    _validate_sources(
        problems,
        item,
        documents,
        context=context,
        allowed=set(target.get("allowed_document_ids", [])),
        required=bool(target.get("requires_sources")),
        allow_metadata_only=bool(target.get("allow_metadata_only", False)),
    )


def validate_task_result(
    workspace: Workspace, task: dict[str, Any], payload: Any
) -> dict[str, list[dict[str, Any]]]:
    """Validate one bounded audit proposal against its frozen target group."""
    if task.get("candidate_digest") != digest(candidate(workspace)):
        raise ValidationError("audit candidate changed; obtain a fresh task")
    if not isinstance(payload, dict) or payload.get("schema_version") != AUDIT_CONTRACT_VERSION:
        raise AuditContentError([_problem("schema", "audit schema_version must be '1'")])
    group = task.get("audit_group")
    if not isinstance(group, dict):
        raise ValidationError("audit task has no frozen target group")
    targets = {row["target_id"]: row for row in group["targets"]}
    documents = {row["document_id"]: row for row in workspace.source_documents()}
    problems: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    report_reviews: list[dict[str, Any]] = []

    if group["kind"] == "finding":
        row = payload.get("record")
        if not isinstance(row, dict):
            raise AuditContentError([_problem("schema", "finding audit requires record")])
        finding_id = group["entity_id"]
        finding = next(
            item
            for item in workspace.read("synthesis")["findings"]
            if item["finding_id"] == finding_id
        )
        if row.get("finding_id") != finding_id:
            problems.append(_problem("finding", "finding_id differs from the task target"))
        if row.get("review_digest") != review_digest(workspace, finding):
            problems.append(
                _problem("review_digest", "review_digest differs from current evidence")
            )
        if row.get("status") not in {"pass", "revise"}:
            problems.append(_problem("status", "finding audit status must be pass or revise"))
        checks = row.get("checks")
        if not isinstance(checks, dict) or set(checks) != set(REVIEW_CHECKS):
            problems.append(_problem("checks", "finding audit requires every review check"))
            checks = checks if isinstance(checks, dict) else {}
        for name in REVIEW_CHECKS:
            check = checks.get(name)
            if not isinstance(check, dict) or check.get("status") not in CHECK_STATUSES:
                problems.append(_problem("checks", f"invalid {name} check"))
            elif not isinstance(check.get("rationale"), str) or not check["rationale"].strip():
                problems.append(_problem("checks", f"{name} rationale must be nonempty"))
        observations = row.get("observations")
        if not isinstance(observations, list):
            observations = []
            problems.append(_problem("observations", "observations must be an array"))
        actual = [item.get("target_id") for item in observations if isinstance(item, dict)]
        if len(actual) != len(targets) or set(actual) != set(targets):
            problems.append(_problem("target_coverage", "return every finding target exactly once"))
        for item in observations:
            target = targets.get(item.get("target_id")) if isinstance(item, dict) else None
            if target:
                _validate_observation(problems, item, target, documents)
                if item.get("verdict") in {"unsupported", "uncertain"}:
                    check = checks.get(item.get("check"))
                    if (
                        row.get("status") != "revise"
                        or not isinstance(check, dict)
                        or check.get("status") != "revise"
                    ):
                        problems.append(
                            _problem(
                                "unresolved", "unsupported or uncertain assertions require revision"
                            )
                        )
        membership_targets = {
            item["target_id"]: item for item in group.get("membership_targets", [])
        }
        assessments = row.get("membership_assessments")
        if not isinstance(assessments, list):
            assessments = []
            problems.append(_problem("membership", "membership_assessments must be an array"))
        actual_membership = [
            item.get("target_id") for item in assessments if isinstance(item, dict)
        ]
        if len(actual_membership) != len(membership_targets) or set(actual_membership) != set(
            membership_targets
        ):
            problems.append(
                _problem("membership_coverage", "return every membership target exactly once")
            )
        for item in assessments:
            if not isinstance(item, dict) or item.get("target_id") not in membership_targets:
                continue
            target = membership_targets[item["target_id"]]
            if item.get("review_digest") != target["review_digest"]:
                problems.append(_problem("membership_digest", "membership digest differs"))
            scope = item.get("supported_scope")
            if scope not in {"outcome", "review", "unsupported", "unknown"}:
                problems.append(_problem("membership_scope", "invalid supported_scope"))
            if not isinstance(item.get("rationale"), str) or not item["rationale"].strip():
                problems.append(_problem("membership", "membership rationale must be nonempty"))
            _validate_sources(
                problems,
                item,
                documents,
                context={"target_id": target["target_id"]},
                allowed=set(target["allowed_document_ids"]),
                required=scope in {"outcome", "review"},
                allow_metadata_only=False,
            )
            supported = (
                scope == "outcome"
                if target["declared_scope"] == "outcome"
                else scope in {"outcome", "review"}
            )
            overlap = checks.get("overlap")
            if not supported and (
                row.get("status") != "revise"
                or not isinstance(overlap, dict)
                or overlap.get("status") != "revise"
            ):
                problems.append(
                    _problem("membership_revision", "unverified membership requires revision")
                )
        if row.get("status") == "pass" and any(
            isinstance(check, dict) and check.get("status") == "revise" for check in checks.values()
        ):
            problems.append(_problem("status", "a finding with revisions cannot pass"))
        records.append(deepcopy(row))
    elif group["kind"] == "report":
        row = payload.get("report_review")
        target = next(iter(targets.values()))
        if not isinstance(row, dict):
            raise AuditContentError([_problem("schema", "report audit requires report_review")])
        for key in ("target_id", "review_digest", "result_group"):
            if row.get(key) != target[key]:
                problems.append(_problem("target_mismatch", f"report {key} differs"))
        if row.get("status") not in {"pass", "revise"}:
            problems.append(_problem("status", "report audit status must be pass or revise"))
        observations = row.get("observations")
        if not isinstance(observations, list) or len(observations) != 1:
            problems.append(_problem("target_coverage", "report target needs one observation"))
        else:
            _validate_observation(problems, observations[0], target, documents)
            if (
                observations[0].get("verdict") in {"unsupported", "uncertain"}
                and row.get("status") != "revise"
            ):
                problems.append(
                    _problem("unresolved", "unsupported report assertion requires revision")
                )
        report_reviews.append(deepcopy(row))
    else:
        raise ValidationError("unknown audit group kind")

    if problems:
        raise AuditContentError(problems)
    for row in [*records, *report_reviews]:
        row.pop("audit_task_id", None)
    return {"records": records, "report_reviews": report_reviews}


def _accepted_task(workspace: Workspace, task_id: Any) -> dict[str, Any]:
    manifest = workspace.load()
    ledger = manifest.get("task_engine") or {}
    task = (ledger.get("tasks") or {}).get(task_id)
    if not task or task.get("state") != "accepted" or task.get("role") != "auditor":
        raise ValidationError("audit row lacks an accepted independent task receipt")
    if task.get("candidate_digest") != digest(candidate(workspace)):
        raise ValidationError("audit receipt is stale after candidate changes")
    result_file = task.get("result_file")
    relative = Path(result_file) if isinstance(result_file, str) else Path(".")
    if (
        relative.is_absolute()
        or relative.parent.as_posix() != f"tasks/{task_id}"
        or relative.name != f"result-{task.get('result_digest')}.json"
    ):
        raise ValidationError("audit result path escapes its immutable task directory")
    result = workspace.store.read_json(result_file, default=None)
    if not isinstance(result, dict) or digest(result) != task.get("result_digest"):
        raise ValidationError("audit result artifact is missing or modified")
    authors = {
        profile for profiles in (ledger.get("authors") or {}).values() for profile in profiles
    }
    if task.get("actor_profile") in authors:
        raise ValidationError("auditor profile must differ from every evidence author")
    return {**task, "result": result}


def validate_receipt(workspace: Workspace, row: dict[str, Any]) -> None:
    task = _accepted_task(workspace, row.get("audit_task_id"))
    if row not in task["result"].get("records", []):
        raise ValidationError("finding audit row does not match its immutable receipt")


def validate_report_receipts(workspace: Workspace, payload: dict[str, Any]) -> None:
    if payload.get("audit_contract_version") != AUDIT_CONTRACT_VERSION:
        raise ValidationError("a fresh v0.5 audit is required")
    records = payload.get("records")
    report_reviews = payload.get("report_reviews")
    if not isinstance(records, list) or not isinstance(report_reviews, list):
        raise ValidationError("audit stage requires records and report_reviews arrays")
    expected_candidate, groups = audit_groups(workspace)
    expected_findings = {group["entity_id"] for group in groups if group["kind"] == "finding"}
    expected_reports = {group["entity_id"] for group in groups if group["kind"] == "report"}
    if {row.get("finding_id") for row in records if isinstance(row, dict)} != expected_findings:
        raise ValidationError("audit every finding exactly once")
    if {
        row.get("target_id") for row in report_reviews if isinstance(row, dict)
    } != expected_reports:
        raise ValidationError("audit every report target exactly once")
    for row in records:
        if not isinstance(row, dict):
            raise ValidationError("finding audit rows must be objects")
        validate_receipt(workspace, row)
    for row in report_reviews:
        if not isinstance(row, dict):
            raise ValidationError("report audit rows must be objects")
        task = _accepted_task(workspace, row.get("audit_task_id"))
        if task.get("candidate_digest") != expected_candidate:
            raise ValidationError("report audit belongs to another candidate")
        if row not in task["result"].get("report_reviews", []):
            raise ValidationError("report audit row does not match its immutable receipt")
