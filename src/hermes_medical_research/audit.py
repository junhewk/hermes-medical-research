"""Digest-bound, host-neutral audit targets and receipt validation."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from hermes_medical_research.search.artifacts import canonical_json
from hermes_medical_research.search.models import ValidationError

from .evidence import REVIEW_CHECKS, review_digest
from .workspace import Workspace, digest, normalized_text

AUDIT_CONTRACT_VERSION = "2"
VERDICTS = {"supported", "unsupported", "uncertain"}
CHECK_STATUSES = {"pass", "revise", "not_applicable"}
# Report targets are packed into bounded groups so one auditor session covers a whole record.
GROUP_TARGET_BYTES = 14 * 1024
SCREENING_BATCH = 12


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


def _without_quotes(value: Any) -> Any:
    """``value`` with every ``quote`` removed, wherever it is nested.

    A quote is the answer to the question the target asks, so it cannot be part of the question.
    An ``extractions`` or ``appraisals`` target requires a citation, and its own row carried a
    ``source_location.quote`` that was already inside ``allowed_document_ids`` and had already
    passed the same verbatim test at submit time: an auditor could satisfy the requirement by
    copying it out of the assertion without opening a document.  ``document_id`` and ``locator``
    stay, because the auditor still has to be told where to read.
    """
    if isinstance(value, dict):
        return {key: _without_quotes(item) for key, item in value.items() if key != "quote"}
    if isinstance(value, list):
        return [_without_quotes(item) for item in value]
    return value


def _locations_in(value: Any) -> list[dict[str, str]]:
    """Every ``{document_id, locator}`` an assertion points at, in order, without duplicates."""
    found: list[dict[str, str]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            document_id, locator = node.get("document_id"), node.get("locator")
            if isinstance(document_id, str) and isinstance(locator, str):
                entry = {"document_id": document_id, "locator": locator}
                if entry not in found:
                    found.append(entry)
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(value)
    return found


def _target(
    *,
    prefix: str,
    kind: str,
    entity_id: str,
    field: str,
    paths: list[str],
    value: Any,
    evidence_digest: str,
    check: str | None = None,
) -> dict[str, Any]:
    """A target's review digest covers its assertion and every source it may be checked
    against, so an unchanged target keeps its receipt when unrelated evidence changes."""
    body = {
        "kind": kind,
        "entity_id": entity_id,
        "field": field,
        "paths": paths,
        "assertion": canonical_json(_without_quotes(value)),
    }
    result = {
        "target_id": f"{prefix}-{digest(body)[:20]}",
        **body,
        "review_digest": digest({"evidence_digest": evidence_digest, **body}),
        # Where this assertion says it came from.  The packet carries the text at these locators so
        # a tool-free call can quote it; the quote itself is never in the assertion.
        "cited_locations": _locations_in(value),
    }
    if check is not None:
        result["check"] = check
    return result


def build_review_targets(
    frozen: dict[str, Any],
    finding_evidence: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Build exhaustive, content-bound target sets for the frozen candidate."""
    stages = frozen["stages"]
    protocol = frozen["protocol"]
    documents = stages["documents"]["records"]
    document_digests = {document["document_id"]: digest(document) for document in documents}
    documents_by_record: dict[str, list[str]] = {}
    for document in documents:
        documents_by_record.setdefault(document["record_id"], []).append(
            document["document_id"]
        )
    finding_evidence = finding_evidence or {}

    def sources_digest(document_ids: list[str], **extra: Any) -> str:
        return digest(
            {
                "documents": [[item, document_digests[item]] for item in sorted(document_ids)],
                "protocol": protocol,
                **extra,
            }
        )

    report_targets: list[dict[str, Any]] = []
    finding_targets: list[dict[str, Any]] = []

    # There is deliberately no protocol target.  It carried no allowed documents, so nothing could
    # ever be cited for or against it, and it required no sources either: the only answer it could
    # receive was an unevidenced opinion.  The protocol's structure is already decided by code --
    # `workflow.check` runs every stage validator over the whole run and `workflow.finalize`
    # refuses to complete unless it passes -- so asking a model to restate that bought nothing.
    synthesis = stages["synthesis"]
    narrative_target = _target(
        prefix="report",
        kind="report",
        entity_id="report",
        field="synthesis.report",
        paths=["/stages/synthesis/title", "/stages/synthesis/limitations"],
        value={"title": synthesis["title"], "limitations": synthesis["limitations"]},
        evidence_digest=sources_digest(
            list(document_digests), findings=synthesis["findings"], workflow=frozen["workflow"]
        ),
    )
    narrative_target["result_group"] = "report-narrative"
    narrative_target["allowed_document_ids"] = [
        document["document_id"] for document in documents
    ]
    narrative_target["record_id"] = None
    report_targets.append(narrative_target)

    extraction_rows = {row["extraction_id"]: row for row in stages["extractions"]["records"]}
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
            if stage == "studies":
                record_id = None
                allowed = [
                    document_id
                    for linked in row["record_ids"]
                    for document_id in documents_by_record.get(linked, [])
                ]
                extra: dict[str, Any] = {}
                group_kind, group_id = "study", entity_id
            else:
                record_id = (
                    extraction_rows[entity_id]["record_id"]
                    if stage == "appraisals"
                    else row["record_id"]
                )
                allowed = documents_by_record.get(record_id, [])
                if stage == "appraisals":
                    extra = {"extraction": extraction_rows[entity_id]}
                elif stage == "dispositions":
                    extra = {
                        "extractions": [
                            item
                            for item in stages["extractions"]["records"]
                            if item["record_id"] == record_id
                        ]
                    }
                else:
                    extra = {}
                group_kind, group_id = "record", record_id
            target = _target(
                prefix="report",
                kind=stage,
                entity_id=entity_id,
                field=f"stages.{stage}.records[{index}]",
                paths=[f"/stages/{stage}/records/{index}"],
                value=row,
                evidence_digest=sources_digest(allowed, **extra),
            )
            target.update(
                requires_sources=stage in {"extractions", "appraisals"},
                # An absence claim is checked against full text; titles cannot settle it.
                allow_metadata_only=stage in {"screening", "studies", "coverage"},
                allowed_document_ids=allowed,
                result_group=f"report-{group_kind}-{digest([group_kind, group_id])[:12]}",
                record_id=record_id,
            )
            if stage == "studies":
                target["study_record_ids"] = list(row["record_ids"])
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
            # The evidence list is deliberately absent here even though harms and certainty are both
            # judged against it.  An assertion has to be echoed back verbatim by the auditor, so a
            # list embedded in two of them is paid for twice in the packet and twice again in the
            # answer: on the first real review one finding's two assertions carried the same 11 KB,
            # which alone put the group over the 32 KB packet bound.  The group carries it once, and
            # each target's review digest still covers the whole finding, so nothing is unfrozen.
            "harms": {"conclusion": finding.get("conclusion")},
            "overlap": finding.get("overlap"),
            "certainty": {
                "certainty": finding.get("certainty"),
                "published_certainty": finding.get("published_certainty", []),
            },
        }
        extraction_ids = {
            contribution["extraction_id"]
            for contribution in finding.get("evidence", [])
            if isinstance(contribution, dict)
            and isinstance(contribution.get("extraction_id"), str)
        }
        allowed = list(
            dict.fromkeys(
                row["source_location"]["document_id"]
                for row in stages["extractions"]["records"]
                if row["extraction_id"] in extraction_ids
            )
        )
        for check, value in values.items():
            target = _target(
                prefix="finding",
                kind="finding",
                entity_id=finding["finding_id"],
                field=f"stages.synthesis.findings[{index}].{check}",
                paths=[f"/stages/synthesis/findings/{index}"],
                value=value,
                evidence_digest=sources_digest(
                    allowed, finding=finding_evidence.get(finding["finding_id"], digest(finding))
                ),
                check=check,
            )
            target["allow_metadata_only"] = check == "scope"
            target["allowed_document_ids"] = allowed
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
                    evidence_digest=sources_digest(
                        allowed,
                        finding=finding_evidence.get(finding["finding_id"], digest(finding)),
                    ),
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


def _report_units(report_targets: list[dict[str, Any]]) -> list[tuple[str, list[dict]]]:
    """Order report targets into indivisible units keyed by the record they describe."""
    narrative = [t for t in report_targets if t["record_id"] is None and t["kind"] != "studies"]
    by_record: dict[str, dict[str, list[dict[str, Any]]]] = {}
    order: list[str] = []
    multi_record_studies: list[dict[str, Any]] = []
    for target in report_targets:
        if target["kind"] == "studies":
            linked = target["study_record_ids"]
            if len(linked) == 1:
                record_id = linked[0]
            else:
                multi_record_studies.append(target)
                continue
        elif target["record_id"] is None:
            continue
        else:
            record_id = target["record_id"]
        if record_id not in by_record:
            by_record[record_id] = {"core": [], "results": []}
            order.append(record_id)
        bucket = "results" if target["kind"] in {"extractions", "appraisals"} else "core"
        by_record[record_id][bucket].append(target)
    units: list[tuple[str, list[dict[str, Any]]]] = [("narrative", narrative)]
    for record_id in order:
        core = by_record[record_id]["core"]
        results = by_record[record_id]["results"]
        if not results and [t["kind"] for t in core] == ["screening"]:
            units.append(("screening-only", core))
            continue
        units.append((f"record:{record_id}", core))
        appraisals = {t["entity_id"]: t for t in results if t["kind"] == "appraisals"}
        for target in results:
            if target["kind"] == "extractions":
                pair = [target]
                if target["entity_id"] in appraisals:
                    pair.append(appraisals.pop(target["entity_id"]))
                units.append((f"record:{record_id}", pair))
        for target in appraisals.values():
            units.append((f"record:{record_id}", [target]))
    for target in multi_record_studies:
        units.append((f"study:{target['entity_id']}", [target]))
    return units


def _pack_report_groups(report_targets: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Pack units into bounded groups without mixing records, except screening-only batches."""
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_key: str | None = None
    size = 0
    for key, unit in _report_units(report_targets):
        if not unit:
            continue
        unit_size = len(canonical_json(unit).encode())
        limit = SCREENING_BATCH if key == "screening-only" else None
        fits = (
            current
            and key == current_key
            and size + unit_size <= GROUP_TARGET_BYTES
            and (limit is None or len(current) + len(unit) <= limit)
        )
        if not fits:
            if current:
                groups.append(current)
            current, current_key, size = [], key, 0
        current.extend(unit)
        size += unit_size
    if current:
        groups.append(current)
    return groups


#: The facts of a contributing extraction that a finding's checks are judged against.  Quotes are
#: deliberately absent; see ``_without_quotes``.  ``source_location`` is in the widest level only:
#: the target assertions already carry their own locators.
CONTRIBUTION_FACTS = (
    "protocol_outcome",
    "comparator_type",
    "outcome_type",
    "sample_size",
    "effect",
    "favors",
    "source_location",
)
#: What a finding group may spend on its contribution detail.  Its five target assertions already
#: cost about 11 KB on the widest real finding, so this leaves headroom under TASK_PACKET_LIMIT.
EVIDENCE_DETAIL_BYTES = 16 * 1024
#: Descending detail: (text cap for the synthesizer's own rationales, keep source_location).  The
#: same graduated shortening ``_synthesis_spec`` uses, so a review with many contributions narrows
#: instead of overflowing.  The last level always fits.
EVIDENCE_DETAIL_LEVELS = ((None, True), (240, False), (120, False), (0, False))


def _evidence_detail(frozen: dict[str, Any], finding: dict[str, Any]) -> list[dict[str, Any]]:
    """The finding's contributions, each carrying the extraction facts its checks need."""
    rows = {row["extraction_id"]: row for row in frozen["stages"]["extractions"]["records"]}
    appraisals = {row["extraction_id"]: row for row in frozen["stages"]["appraisals"]["records"]}

    def build(cap: int | None, locations: bool) -> list[dict[str, Any]]:
        keys = CONTRIBUTION_FACTS if locations else tuple(
            key for key in CONTRIBUTION_FACTS if key != "source_location"
        )
        detail = []
        for contribution in finding.get("evidence") or []:
            entry = dict(contribution)
            for field in ("alignment_rationale", "weight_rationale"):
                if cap is None or field not in entry:
                    continue
                entry[field] = str(entry[field])[:cap] if cap else ""
            row = rows.get(contribution.get("extraction_id"))
            if row is not None:
                entry["extraction"] = _without_quotes(
                    {key: row.get(key) for key in keys if key in row}
                )
                appraisal = appraisals.get(row["extraction_id"]) or {}
                if appraisal.get("same_as"):
                    appraisal = appraisals.get(appraisal["same_as"], appraisal)
                if appraisal:
                    # The certainty rule at evidence.py:244 turns on these, so they must be visible.
                    entry["appraisal"] = {
                        "method": appraisal.get("method"),
                        "completion": appraisal.get("completion"),
                        "overall_judgment": appraisal.get("overall_judgment"),
                    }
            detail.append(entry)
        return detail

    for cap, locations in EVIDENCE_DETAIL_LEVELS:
        detail = build(cap, locations)
        if len(canonical_json(detail).encode()) <= EVIDENCE_DETAIL_BYTES:
            return detail
    return detail


def audit_groups(workspace: Workspace) -> tuple[str, list[dict[str, Any]]]:
    """Return bounded, exhaustive audit groups whose digests change only with their content."""
    frozen = candidate(workspace)
    candidate_digest = digest(frozen)
    findings = {row["finding_id"]: row for row in frozen["stages"]["synthesis"]["findings"]}
    finding_evidence = {
        finding_id: review_digest(workspace, finding) for finding_id, finding in findings.items()
    }
    report_targets, finding_targets, membership_targets = build_review_targets(
        frozen, finding_evidence
    )
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
                # Judged against by the harms and certainty targets, and shown once for both.  Each
                # contribution carries the checkable facts of the extraction it names, because the
                # `estimates` and `harms` assertions are only the conclusion string: without the
                # effects, the outcome types and the comparators, both checks ask about data the
                # packet does not hold, which is the defect that produced three of seven findings
                # needing revision on the first real run.
                "evidence": _evidence_detail(frozen, finding),
                "allowed_document_ids": allowed_document_ids,
                "group_digest": digest(
                    {
                        "finding": finding_evidence[finding_id],
                        "targets": [
                            [t["target_id"], t["review_digest"]] for t in [*targets, *memberships]
                        ],
                    }
                ),
                "proposal": {
                    "schema_version": AUDIT_CONTRACT_VERSION,
                    "record": {
                        "finding_id": finding_id,
                        "review_digest": finding_evidence[finding_id],
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
    for targets in _pack_report_groups(report_targets):
        identity = digest([[t["kind"], t["entity_id"]] for t in targets])[:20]
        groups.append(
            {
                "group_id": f"report:{identity}",
                "kind": "report",
                "entity_id": identity,
                "targets": targets,
                "membership_targets": [],
                "allowed_document_ids": list(
                    dict.fromkeys(
                        document_id
                        for target in targets
                        for document_id in target["allowed_document_ids"]
                    )
                ),
                "group_digest": digest([[t["target_id"], t["review_digest"]] for t in targets]),
                "proposal": {
                    "schema_version": AUDIT_CONTRACT_VERSION,
                    "report_reviews": [
                        {
                            "target_id": target["target_id"],
                            "review_digest": target["review_digest"],
                            "result_group": target["result_group"],
                            "status": "revise",
                            "observations": [_observation_template(target)],
                        }
                        for target in targets
                    ],
                },
            }
        )
    return candidate_digest, groups


def citation_contract(workspace: Workspace) -> dict[str, Any]:
    return {
        "rules": [
            "Cite a document_id and locator returned by hmr source show.",
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
    group = task.get("audit_group")
    if not isinstance(group, dict):
        raise ValidationError("audit task has no frozen target group")
    current = current_group_digests(workspace)
    if current.get(task.get("group_id")) != task.get("group_digest"):
        raise ValidationError("audit targets changed; obtain a fresh task")
    if not isinstance(payload, dict) or payload.get("schema_version") != AUDIT_CONTRACT_VERSION:
        raise AuditContentError(
            [_problem("schema", f"audit schema_version must be {AUDIT_CONTRACT_VERSION!r}")]
        )
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
        rows = payload.get("report_reviews")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise AuditContentError(
                [_problem("schema", "report audit requires a report_reviews array")]
            )
        returned = [row.get("target_id") for row in rows]
        if len(returned) != len(targets) or set(returned) != set(targets):
            problems.append(
                _problem("target_coverage", "return every report target exactly once")
            )
        for row in rows:
            target = targets.get(row.get("target_id"))
            if target is None:
                continue
            context = {"target_id": target["target_id"]}
            for key in ("review_digest", "result_group"):
                if row.get(key) != target[key]:
                    problems.append(_problem("target_mismatch", f"report {key} differs", **context))
            if row.get("status") not in {"pass", "revise"}:
                problems.append(
                    _problem("status", "report audit status must be pass or revise", **context)
                )
            observations = row.get("observations")
            if not isinstance(observations, list) or len(observations) != 1:
                problems.append(
                    _problem("target_coverage", "report target needs one observation", **context)
                )
                continue
            _validate_observation(problems, observations[0], target, documents)
            if (
                observations[0].get("verdict") in {"unsupported", "uncertain"}
                and row.get("status") != "revise"
            ):
                problems.append(
                    _problem(
                        "unresolved", "unsupported report assertion requires revision", **context
                    )
                )
        order = {target_id: index for index, target_id in enumerate(targets)}
        report_reviews.extend(
            deepcopy(row)
            for row in sorted(rows, key=lambda item: order.get(item.get("target_id"), len(order)))
        )
    else:
        raise ValidationError("unknown audit group kind")

    if problems:
        raise AuditContentError(problems)
    for row in [*records, *report_reviews]:
        row.pop("audit_task_id", None)
    return {"records": records, "report_reviews": report_reviews}


def current_group_digests(workspace: Workspace) -> dict[str, str]:
    _, groups = audit_groups(workspace)
    return {group["group_id"]: group["group_digest"] for group in groups}


def _accepted_task(
    workspace: Workspace, task_id: Any, digests: dict[str, str] | None = None
) -> dict[str, Any]:
    manifest = workspace.load()
    ledger = manifest.get("task_engine") or {}
    task = (ledger.get("tasks") or {}).get(task_id)
    if not task or task.get("state") != "accepted" or task.get("role") != "auditor":
        raise ValidationError("audit row lacks an accepted independent task receipt")
    digests = current_group_digests(workspace) if digests is None else digests
    if digests.get(task.get("group_id")) != task.get("group_digest"):
        raise ValidationError("audit receipt is stale after its targets changed")
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


def validate_receipt(
    workspace: Workspace, row: dict[str, Any], digests: dict[str, str] | None = None
) -> None:
    task = _accepted_task(workspace, row.get("audit_task_id"), digests)
    if row not in task["result"].get("records", []):
        raise ValidationError("finding audit row does not match its immutable receipt")


def validate_report_receipts(workspace: Workspace, payload: dict[str, Any]) -> None:
    if payload.get("audit_contract_version") != AUDIT_CONTRACT_VERSION:
        raise ValidationError("a fresh audit under the current audit contract is required")
    records = payload.get("records")
    report_reviews = payload.get("report_reviews")
    if not isinstance(records, list) or not isinstance(report_reviews, list):
        raise ValidationError("audit stage requires records and report_reviews arrays")
    _, groups = audit_groups(workspace)
    digests = {group["group_id"]: group["group_digest"] for group in groups}
    expected_findings = {group["entity_id"] for group in groups if group["kind"] == "finding"}
    expected_reports = {
        target["target_id"]: target
        for group in groups
        if group["kind"] == "report"
        for target in group["targets"]
    }
    if {row.get("finding_id") for row in records if isinstance(row, dict)} != expected_findings:
        raise ValidationError("audit every finding exactly once")
    returned = [row.get("target_id") for row in report_reviews if isinstance(row, dict)]
    if len(returned) != len(report_reviews) or len(set(returned)) != len(returned):
        raise ValidationError("audit every report target exactly once")
    if set(returned) != set(expected_reports):
        raise ValidationError("audit every report target exactly once")
    for row in records:
        validate_receipt(workspace, row, digests)
    for row in report_reviews:
        if row.get("review_digest") != expected_reports[row["target_id"]]["review_digest"]:
            raise ValidationError("report audit row belongs to changed target content")
        task = _accepted_task(workspace, row.get("audit_task_id"), digests)
        if row not in task["result"].get("report_reviews", []):
            raise ValidationError("report audit row does not match its immutable receipt")
