"""Transactional evidence submissions, diagnostics, and finalization."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from hermes_medical_research.search.models import ValidationError

from .validation import validate_complete, validate_contribution, validate_stage
from .workspace import DEPENDENCIES, ID_FIELDS, Workspace, digest

EDITABLE = set(DEPENDENCIES) - {"records", "documents"}


def current_digests(workspace: Workspace) -> dict[str, str]:
    datasets = workspace.manifest_view()["datasets"]
    return {stage: entry["digest"] for stage, entry in datasets.items()}


class MemoryStore:
    def __init__(self, original):
        self.original = original
        self.values: dict[str, Any] = {}

    def read_json(self, name, **kwargs):
        if name not in self.values:
            self.values[name] = self.original.read_json(name, **kwargs)
        return deepcopy(self.values[name])

    def write_json(self, name, value):
        self.values[name] = deepcopy(value)


class Preview(Workspace):
    """Use the real source paths, but hold all proposed revisions in memory."""

    def manifest_view(self):
        if "research.json" not in self.store.values:
            self.load()
        return self.store.values["research.json"]

    def __init__(self, workspace: Workspace):
        super().__init__(workspace.path)
        self.store = MemoryStore(workspace.store)
        self.indices = {}
        self.payloads = {}

    def read(self, stage, *, fresh=True):
        manifest = self.manifest_view()
        entry = manifest["datasets"].get(stage)
        if not entry:
            return {"schema_version": self.stage_version(stage), "records": []}
        if fresh and self.stale(stage, manifest):
            raise ValidationError(f"{stage} is stale; review and resubmit it")
        if not (self.path / entry["file"]).resolve().is_relative_to(self.path):
            raise ValidationError("artifact path escapes the research workspace")
        key = (stage, entry["digest"])
        if key not in self.payloads:
            data = self.store.read_json(entry["file"])
            if digest(data) != entry["digest"]:
                raise ValidationError(f"{stage} artifact was modified outside research record")
            self.payloads[key] = data
        return self.payloads[key]

    def index(self, stage):
        rows = self.rows(stage)
        key = (stage, self.manifest_view()["datasets"].get(stage, {}).get("digest"))
        if key not in self.indices:
            self.indices[key] = {r[ID_FIELDS[stage]]: r for r in rows}
        return self.indices[key]


def error(stage: str, record: str | None, exc: Exception, field: str = "") -> dict:
    return {
        "stage": stage,
        "record_id": record,
        "field": field or str(exc).split(" ")[0],
        "message": str(exc),
    }


def stage_errors(workspace: Workspace, stage: str, payload: dict) -> list[dict]:
    """Collect errors across records/contributions instead of failing on the first record."""
    errors = []
    try:
        if not isinstance(payload, dict) or payload.get(
            "schema_version"
        ) != workspace.stage_version(stage):
            raise ValidationError(f"schema_version must be {workspace.stage_version(stage)!r}")
        key = "findings" if stage == "synthesis" else "records"
        rows = payload.get(key)
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise ValidationError(f"{key} must be an array of objects")
        id_field = "finding_id" if stage == "synthesis" else ID_FIELDS[stage]
        seen = set()
        for row in rows:
            rid = row.get(id_field)
            if not isinstance(rid, str) or not rid.strip() or rid in seen:
                errors.append(
                    error(
                        stage,
                        str(rid),
                        ValidationError("missing or duplicate identifier"),
                        id_field,
                    )
                )
            else:
                seen.add(rid)
            # Audit receipts are checked against the complete finding and report-target sets,
            # so a one-row slice of the reviews stage is never valid on its own.
            if stage != "reviews":
                try:
                    validate_stage(workspace, stage, {**payload, key: [row]})
                except (ValidationError, KeyError, TypeError, AttributeError) as exc:
                    errors.append(error(stage, str(rid), exc))
            # Collect per-contribution diagnostics without changing the finding's evidence body.
            # Body-level certainty and overlap rules must see all contributors together.
            contributions = row.get("evidence") if stage == "synthesis" else None
            for contribution in contributions if isinstance(contributions, list) else []:
                try:
                    validate_contribution(workspace, row, contribution)
                except (ValidationError, KeyError, TypeError, AttributeError) as exc:
                    field = (
                        f"evidence.{contribution.get('extraction_id', '?')}"
                        if isinstance(contribution, dict)
                        else "evidence"
                    )
                    errors.append(error(stage, str(rid), exc, field))
        # Cross-record rules (e.g. assigning a report to two study groups).
        try:
            validate_stage(workspace, stage, payload)
        except (ValidationError, KeyError, TypeError, AttributeError) as exc:
            if not any(e["message"] == str(exc) for e in errors):
                errors.append(error(stage, None, exc))
    except (ValidationError, KeyError, TypeError, AttributeError) as exc:
        errors.append(error(stage, None, exc))
    return list(
        {(e["stage"], e["record_id"], e["field"], e["message"]): e for e in errors}.values()
    )


def prepare_batch(workspace: Workspace, batch: dict) -> tuple[Preview, list[dict]]:
    preview = Preview(workspace)
    errors = []
    if not isinstance(batch, dict) or batch.get("schema_version") != "2":
        return preview, [error("batch", None, ValidationError("batch schema_version must be '2'"))]
    if batch.get("base_digests") != current_digests(workspace):
        return preview, [
            error(
                "batch", None, ValidationError("base_digests are stale; obtain research next again")
            )
        ]
    stages = batch.get("stages")
    if not isinstance(stages, dict) or not stages or set(stages) - EDITABLE:
        return preview, [
            error("batch", None, ValidationError("stages must name editable research stages"))
        ]
    if "reviews" in stages and "synthesis" in stages:
        return preview, [
            error(
                "batch",
                None,
                ValidationError("record synthesis before its separate claim-review pass"),
            )
        ]
    failed: set[str] = set()
    for stage in DEPENDENCIES:
        if stage not in stages:
            continue
        blocked_by = sorted(dep for dep in DEPENDENCIES[stage] if dep in failed)
        if blocked_by:
            # Validating against the stored upstream stage would report errors that are only
            # echoes of the upstream failure, so defer this stage with one explicit note.
            errors.append(
                error(
                    stage,
                    None,
                    ValidationError(
                        f"{stage} will be checked after the {', '.join(blocked_by)} errors "
                        "are fixed"
                    ),
                    "deferred",
                )
            )
            failed.add(stage)
            continue
        before = len(errors)
        try:
            incoming = stages[stage]
            if not isinstance(incoming, dict):
                raise ValidationError("stage payload must be an object")
            if stage == "synthesis":
                payload = incoming
            else:
                rows = incoming.get("records")
                if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                    raise ValidationError("records must be an array of objects")
                ids = [r.get(ID_FIELDS[stage]) for r in rows]
                if any(not isinstance(i, str) or not i.strip() for i in ids) or len(
                    set(ids)
                ) != len(ids):
                    raise ValidationError("batch rows need unique nonempty identifiers")
                merged = {r[ID_FIELDS[stage]]: r for r in preview.rows(stage, fresh=False)}
                merged.update({r[ID_FIELDS[stage]]: r for r in rows})
                payload = {**incoming, "records": list(merged.values())}
            if stage == "appraisals":
                old_entry = workspace.load()["datasets"].get("appraisals")
                old_digest = (old_entry or {}).get("depends", {}).get("extractions")
                if old_digest:
                    old_payload = workspace.store.read_json(
                        f"revisions/extractions-{old_digest}.json"
                    )
                    old_results = {r["extraction_id"]: r for r in old_payload["records"]}
                    changed = {
                        eid
                        for eid, r in preview.index("extractions").items()
                        if eid in old_results and r != old_results[eid]
                    }
                    submitted = {r["extraction_id"] for r in incoming["records"]}
                    if changed - submitted:
                        raise ValidationError(
                            "resubmit appraisals for changed extractions: "
                            + ", ".join(sorted(changed - submitted))
                        )
            problems = stage_errors(preview, stage, payload)
            errors.extend(problems)
            if not problems:
                preview.put(stage, payload)
        except (ValidationError, KeyError, TypeError, AttributeError) as exc:
            errors.append(error(stage, None, exc))
        if len(errors) > before:
            failed.add(stage)
    return preview, errors


def commit_preview(workspace: Workspace, preview: Preview) -> None:
    # Revision writes may leave harmless orphan files if interrupted. Manifest is last/atomic.
    manifest = preview.load()
    for entry in manifest["datasets"].values():
        name = entry["file"]
        if name in preview.store.values:
            workspace.store.write_json(name, preview.store.values[name])
    workspace.save(manifest)


def submit_batch(workspace: Workspace, batch: dict) -> dict:
    preview, errors = prepare_batch(workspace, batch)
    if errors:
        return {"accepted": False, "errors": errors, "status": workspace.status()}
    commit_preview(workspace, preview)
    return {"accepted": True, "errors": [], "status": workspace.status()}


def check(workspace: Workspace, batch: dict | None = None) -> dict:
    if batch is not None:
        view, errors = prepare_batch(workspace, batch)
    else:
        view, errors = Preview(workspace), []
    manifest = view.load()
    for stage in view.required_stages:
        if stage not in manifest["datasets"]:
            errors.append(error(stage, None, ValidationError("stage is missing")))
        elif view.stale(stage, manifest):
            errors.append(
                error(stage, None, ValidationError("stage is stale; review and resubmit"))
            )
        else:
            try:
                errors.extend(stage_errors(view, stage, view.read(stage)))
            except (ValidationError, KeyError, TypeError, AttributeError) as exc:
                errors.append(error(stage, None, exc))
    warnings = []
    if not errors:
        try:
            warnings = validate_complete(view)
        except (ValidationError, KeyError, TypeError, AttributeError) as exc:
            errors.append(error("readiness", None, exc))
    errors = list(
        {(e["stage"], e["record_id"], e["field"], e["message"]): e for e in errors}.values()
    )
    return {
        "ready": not errors,
        "quality": "blocked" if errors else "qualified" if warnings else "ready",
        "errors": errors,
        "warnings": warnings,
        "datasets": current_digests(view),
        "semantic_support": (
            "Independent task audit; deterministic checks do not establish medical correctness."
        ),
        "audit": _audit_summary(workspace),
    }


def _audit_summary(workspace: Workspace) -> dict:
    ledger = workspace.load().get("task_engine", {})
    tasks = [task for task in ledger.get("tasks", {}).values() if task.get("kind") == "audit"]
    return {
        "contract_version": "1",
        "state": ledger.get("state"),
        "accepted": sum(task.get("state") == "accepted" for task in tasks),
        "active": sum(task.get("state") in {"pending", "in_progress"} for task in tasks),
    }


async def finalize(
    workspace: Workspace, batch: dict | None = None, *, offline: bool = False, session=None
) -> dict:
    from .reporting import export
    from .verification import verify

    checked = check(workspace, batch)
    if not checked["ready"]:
        return {
            "completed": False,
            "check": checked,
            "next_command": ["mdr", "run", "next", workspace.load().get("run_id", "RUN_ID")],
        }
    if batch is not None:
        result = submit_batch(workspace, batch)
        if not result["accepted"]:
            return {"completed": False, "check": result}
    verification = await verify(workspace, offline=offline, session=session)
    if not verification["ready"]:
        return {"completed": False, "verification": verification}
    artifacts = export(workspace)
    completion = {
        "schema_version": "2",
        "completed": True,
        "quality": "qualified" if artifacts["warnings"] else "ready",
        "datasets": current_digests(workspace),
        "artifacts": {
            name: {
                "path": str(workspace.path / name),
                "sha256": _file_digest(workspace.path / name),
            }
            for name in artifacts["artifacts"]
        },
        "warnings": artifacts["warnings"],
        "checked_at": verification["checked_at"],
        "audit_contract_version": "1",
        "citation_verification": {rid: v["status"] for rid, v in verification["identity"].items()},
        "claim_review": "independent-task-audit"
        if workspace.evidence_version == "2"
        else "legacy-unreviewed",
    }
    workspace.store.write_json("completion.json", completion)
    return completion


def _file_digest(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()
