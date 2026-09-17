"""Content-addressed runs; the manifest is the atomic commit point."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from copy import deepcopy
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from filelock import FileLock

from hermes_medical_research import __version__
from hermes_medical_research.search.artifacts import RunStore, canonical_json, strategy_digest
from hermes_medical_research.search.config import Credentials
from hermes_medical_research.search.models import (
    BIOMEDICAL_INDEX_SOURCES,
    CORE_SOURCES,
    Question,
    ValidationError,
)
from hermes_medical_research.search.ranking import deduplicate, rank_records

DEPENDENCIES = {
    "records": (),
    "documents": ("records",),
    "screening": ("records",),
    "studies": ("records", "screening"),
    "extractions": ("records", "documents", "screening", "studies"),
    "appraisals": ("extractions",),
    # One row per assessed record deciding every protocol outcome: extracted, not reported,
    # or not applicable.  It follows extractions so a batch validates bindings in one pass.
    "dispositions": ("records", "documents", "screening", "studies", "extractions"),
    "coverage": ("records", "screening"),
    "synthesis": ("extractions", "appraisals", "studies", "dispositions"),
    "reviews": (
        "synthesis",
        "extractions",
        "appraisals",
        "dispositions",
        "studies",
        "documents",
        "records",
        "screening",
        "coverage",
    ),
}
ID_FIELDS = {
    "records": "record_id",
    "documents": "document_id",
    "screening": "record_id",
    "studies": "study_id",
    "extractions": "extraction_id",
    "appraisals": "extraction_id",
    "dispositions": "record_id",
    "coverage": "record_id",
    "reviews": "finding_id",
}
# Runs created or first assessed by 0.5.8+ must decide every protocol outcome per record.
# The marker lives in the manifest, outside the protocol, so Review protocol digests are stable.
OUTCOME_CONTRACT = "outcome-dispositions"


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def now() -> str:
    return datetime.now(UTC).isoformat()


def require_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a nonempty string")
    return value.strip()


def positive_limit(value: Any, name: str, *, allow_all: bool = False) -> int | str:
    if allow_all and value == "all":
        return "all"
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError(
            f"{name} must be a positive integer" + (" or all" if allow_all else "")
        )
    return value


class Workspace:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.store = RunStore(self.path)

    @property
    def lock(self) -> FileLock:
        return FileLock(str(self.path / ".research.lock"), timeout=5)

    def load(self) -> dict[str, Any]:
        data = self.store.read_json("research.json")
        if not isinstance(data, dict) or data.get("schema_version") != "1":
            raise ValidationError("not a supported research workspace")
        if digest(data["protocol"]) != data.get("protocol_digest"):
            raise ValidationError(
                "protocol changed; initialize a new workspace for a revised protocol"
            )
        return data

    def save(self, manifest: dict[str, Any]) -> None:
        manifest["updated_at"] = now()
        self.store.write_json("research.json", manifest)

    def init(
        self,
        payload: dict[str, Any],
        *,
        mode: str,
        records: int | str | None,
        fulltexts: int | str | None,
        language: str,
        evidence_version: str = "2",
    ) -> dict[str, Any]:
        if (self.path / "research.json").exists() or (self.path / "manifest.json").exists():
            raise ValidationError("output already contains a run; use research status to resume")
        question = Question.from_dict(payload)
        # New research uses explicit schema-3 search blocks; standalone legacy searches stay v2.
        normalized = question.to_dict()
        normalized["schema_version"] = "3"
        question = Question.from_dict(normalized)
        eligibility = payload.get("eligibility")
        if not isinstance(eligibility, dict) or not eligibility.get("include"):
            raise ValidationError(
                "research input needs eligibility.include and eligibility.exclude lists"
            )
        for key in ("include", "exclude"):
            if not isinstance(eligibility.get(key), list):
                raise ValidationError(f"eligibility.{key} must be a list")
            for item in eligibility[key]:
                require_text(item, f"eligibility.{key}")
        outcomes = payload.get("outcomes", [])
        if not isinstance(outcomes, list) or (not outcomes and question.framework != "PCC"):
            raise ValidationError("clinical research needs an explicit outcomes list")
        if not outcomes and question.framework == "PCC" and evidence_version == "2":
            outcomes = ["Evidence map"]
        for item in outcomes:
            require_text(item, "outcome")
        rationale = require_text(payload.get("search_rationale"), "search_rationale")
        if mode == "review-prep" and (records is None or fulltexts is None):
            raise ValidationError(
                "review-prep requires explicit records and full-text limits (or all)"
            )
        records = positive_limit(
            records if records is not None else 100,
            "records_per_source",
            allow_all=mode == "review-prep",
        )
        fulltexts = positive_limit(
            fulltexts if fulltexts is not None else 30, "fulltexts", allow_all=mode == "review-prep"
        )
        sources = question.sources or [*CORE_SOURCES, "europe-pmc"]
        if not question.sources and question.framework != "PCC":
            sources.append("clinicaltrials")
        if not question.sources and Credentials.from_env().scopus_api_key:
            sources.append("scopus")
        question.sources = [s for s in sources if s not in question.exclude_sources]
        if not question.sources:
            raise ValidationError("research must select at least one source")
        if mode == "report" and not set(question.sources).intersection(BIOMEDICAL_INDEX_SOURCES):
            raise ValidationError("report mode requires PubMed or Europe PMC")
        language = require_text(language, "language")
        protocol = {
            "question": question.to_dict(),
            "mode": mode,
            "language": language,
            "eligibility": eligibility,
            "outcomes": outcomes,
            "search_rationale": rationale,
            "records_per_source": records,
            "fulltexts": fulltexts,
            "appraisal_status": "provisional-agent-assessment",
        }
        data = {
            "schema_version": "1",
            "tool_version": __version__,
            "evidence_version": evidence_version,
            "created_at": now(),
            "protocol": protocol,
            "protocol_digest": digest(protocol),
            "datasets": {},
            "searches": {},
            "fulltext_attempts": {},
        }
        if evidence_version == "2":
            data["assessment_contract"] = OUTCOME_CONTRACT
        self.path.mkdir(parents=True, exist_ok=True)
        self.store.write_json("question.json", question.to_dict())
        self.save(data)
        return self.status()

    @property
    def evidence_version(self) -> str:
        return self.load().get("evidence_version", "1")

    @property
    def outcome_contract(self) -> bool:
        manifest = self.load()
        return (
            manifest.get("evidence_version") == "2"
            and manifest.get("assessment_contract") == OUTCOME_CONTRACT
        )

    @property
    def required_stages(self) -> tuple[str, ...]:
        modern = self.evidence_version == "2"
        contract = self.outcome_contract
        return tuple(
            s
            for s in DEPENDENCIES
            if (modern or s not in {"coverage", "reviews"})
            and (contract or s != "dispositions")
        )

    def stage_version(self, stage: str) -> str:
        return "1" if stage in {"records", "documents"} else self.evidence_version

    def read(self, stage: str, *, fresh: bool = True) -> dict[str, Any]:
        manifest = self.load()
        entry = manifest["datasets"].get(stage)
        if not entry:
            return {"schema_version": "1", "records": []}
        path = self.path / entry["file"]
        if not path.resolve().is_relative_to(self.path):
            raise ValidationError("artifact path escapes the research workspace")
        data = json.loads(path.read_text(encoding="utf-8"))
        if digest(data) != entry["digest"]:
            raise ValidationError(f"{stage} artifact was modified outside research record")
        if fresh and self.stale(stage, manifest):
            raise ValidationError(
                f"{stage} is stale; review and resubmit it after upstream changes"
            )
        return data

    def stale(self, stage: str, manifest: dict[str, Any] | None = None) -> bool:
        manifest = manifest or self.load()
        entry = manifest["datasets"].get(stage)
        if not entry:
            return False
        return any(
            manifest["datasets"].get(dep, {}).get("digest") != entry["depends"].get(dep)
            or self.stale(dep, manifest)
            for dep in DEPENDENCIES[stage]
        )

    def put(self, stage: str, payload: dict[str, Any], *, validate: bool = True) -> None:
        if stage not in DEPENDENCIES or not isinstance(payload, dict):
            raise ValidationError("unsupported research stage or payload")
        if payload.get("schema_version") != self.stage_version(stage):
            raise ValidationError(f"{stage} requires schema_version {self.stage_version(stage)!r}")
        if stage != "synthesis":
            rows = payload.get("records")
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise ValidationError("stage records must be an array of objects")
            ids = [require_text(row.get(ID_FIELDS[stage]), ID_FIELDS[stage]) for row in rows]
            if len(ids) != len(set(ids)):
                raise ValidationError(f"duplicate identifiers in {stage}")
            if stage == "documents":
                payload = {**payload, "records": sorted(rows, key=lambda row: row["document_id"])}
        manifest = self.load()
        for dep in DEPENDENCIES[stage]:
            if self.stale(dep, manifest):
                raise ValidationError(f"{dep} is stale; refresh it before recording {stage}")
            self.read(dep)
        if validate:
            from .validation import validate_stage

            validate_stage(self, stage, payload)
        checksum = digest(payload)
        relative = f"revisions/{stage}-{checksum}.json"
        self.store.write_json(relative, payload)
        manifest["datasets"][stage] = {
            "file": relative,
            "digest": checksum,
            "recorded_at": now(),
            "depends": {
                dep: manifest["datasets"].get(dep, {}).get("digest") for dep in DEPENDENCIES[stage]
            },
        }
        self.save(manifest)

    def rows(self, stage: str, *, fresh: bool = True) -> list[dict[str, Any]]:
        return self.read(stage, fresh=fresh).get("records", [])

    def index(self, stage: str) -> dict[str, dict[str, Any]]:
        return {row[ID_FIELDS[stage]]: row for row in self.rows(stage)}

    def source_documents(self, *, fresh: bool = True) -> list[dict[str, Any]]:
        """Return citable documents plus a deterministic title segment for every record.

        Titles are projected at read time so older workspaces gain citable bibliographic
        context without rewriting their document stage or making downstream assessments stale.
        """
        records = self.index("records")
        documents = deepcopy(self.rows("documents", fresh=fresh))
        by_id = {row["document_id"]: row for row in documents}
        for record_id, record in records.items():
            document_id = f"{record_id}:metadata"
            title = require_text(record.get("title"), "record title")
            document = by_id.get(document_id)
            if document is None:
                document = {
                    "document_id": document_id,
                    "record_id": record_id,
                    "kind": "metadata",
                    "url": record.get("url"),
                    "retrieved_at": record.get("retrieved_at"),
                    "segments": [],
                }
                documents.append(document)
                by_id[document_id] = document
            elif document.get("record_id") != record_id or document.get("kind") != "metadata":
                raise ValidationError(f"reserved metadata document conflicts for {record_id}")
            existing = next(
                (
                    segment
                    for segment in document.get("segments", [])
                    if segment.get("locator") == "title"
                ),
                None,
            )
            if existing is None:
                document.setdefault("segments", []).append({"locator": "title", "text": title})
            elif normalized_text(existing.get("text", "")) != normalized_text(title):
                raise ValidationError(f"stored metadata title conflicts with record {record_id}")
        return sorted(documents, key=lambda row: row["document_id"])

    def source_index(self) -> dict[str, dict[str, Any]]:
        return {row["document_id"]: row for row in self.source_documents()}

    def allocations(
        self, *, excluding: str | None = None, manifest: dict[str, Any] | None = None
    ) -> Counter:
        result: Counter = Counter()
        current = manifest if manifest is not None else self.load()
        for key, entry in current["searches"].items():
            if key == excluding:
                continue
            for source, count in entry["allocation"].items():
                result[source] += count
        return result

    def check_budget(
        self,
        allocations: dict[str, int],
        *,
        excluding: str | None = None,
        manifest: dict[str, Any] | None = None,
    ) -> None:
        current = manifest if manifest is not None else self.load()
        protocol = current["protocol"]
        allowed = protocol["question"]["sources"]
        if set(allocations) - set(allowed):
            raise ValidationError(
                "source is outside the protocol; initialize a revised research run"
            )
        limit = protocol["records_per_source"]
        used = self.allocations(excluding=excluding, manifest=current)
        if limit != "all":
            for source, count in allocations.items():
                if used[source] + count > limit:
                    raise ValidationError(
                        f"{source} exceeds research budget: {used[source]} + {count} > {limit}"
                    )

    def reserve_in(
        self, manifest: dict[str, Any], search_path: Path, strategy: Any
    ) -> str:
        """Mutate one already-locked manifest with an idempotent search reservation."""
        if strategy.question.question != manifest["protocol"]["question"]["question"]:
            raise ValidationError("child search must preserve the protocol's original question")
        if manifest["protocol"]["mode"] == "review-prep" and strategy.mode != "review":
            raise ValidationError("review-prep requires approval-gated review searches")
        key = strategy_digest(strategy)
        previous = manifest["searches"].get(key, {})
        if previous.get("path") and previous["path"] != str(search_path.resolve()):
            raise ValidationError("this strategy already has a child run; resume its recorded path")
        limit = strategy.limit_per_source
        if limit == "all" and manifest["protocol"]["records_per_source"] != "all":
            raise ValidationError("an all-results search requires an all-results research budget")
        allocation = {source: int(limit) if limit != "all" else 0 for source in strategy.strategies}
        self.check_budget(allocation, excluding=key, manifest=manifest)
        manifest["searches"][key] = {
            **previous,
            "allocation": allocation,
            "status": "reserved",
            "path": str(search_path.resolve()),
            "strategy_digest": key,
        }
        return key

    def reserve(self, search_path: Path, strategy: Any) -> None:
        manifest = self.load()
        self.reserve_in(manifest, search_path, strategy)
        self.save(manifest)

    def attach(self, search_path: Path) -> dict[str, Any]:
        from hermes_medical_research.search.cli import (
            _load_run,
            _require_strategy_approval,
            _validate_preflight,
        )

        source_store = RunStore(search_path.resolve())
        strategy, source_manifest = _load_run(source_store)
        key = strategy_digest(strategy)
        manifest = self.load()
        if source_manifest["status"] not in {"complete", "failed"}:
            raise ValidationError("finish or checkpoint the search before attaching it")
        if manifest["protocol"]["mode"] == "review-prep":
            if strategy.mode != "review":
                raise ValidationError("review-prep cannot import an unapproved quick search")
            _require_strategy_approval(source_store, strategy)
            _validate_preflight(strategy, source_store.read_json("preflight.json"))
            if strategy.limit_per_source == "all":
                approval = source_store.read_json("approval.json")
                if not approval.get("all_results"):
                    raise ValidationError("all-results confirmation is missing")
        if strategy.question.question != manifest["protocol"]["question"]["question"]:
            raise ValidationError("attached search belongs to a different original question")
        allocation = {
            source: int(info.get("retrieved") or 0)
            for source, info in source_manifest["sources"].items()
        }
        self.check_budget(allocation, excluding=key)
        native = {source: source_store.read_source(source) for source in strategy.strategies}
        for source, values in native.items():
            state = source_manifest["sources"][source]
            counts = [state.get(name) for name in ("retrieved", "retained", "filtered_out")]
            if any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in counts):
                raise ValidationError("search checkpoint has invalid retrieval counts")
            retrieved, retained, filtered = counts
            if retained != len(values) or retained + filtered > retrieved:
                raise ValidationError(
                    "search counts disagree with its checkpoint; resume the search first"
                )
        snapshot = {
            "strategy": strategy.to_dict(),
            "manifest": source_manifest,
            "summary": source_store.read_json("summary.json"),
            "sources": native,
            "approval": source_store.read_json("approval.json", default=None),
            "preflight": source_store.read_json("preflight.json", default=None),
        }
        checksum = digest(snapshot)
        relative = f"searches/snapshot-{checksum}.json"
        self.store.write_json(relative, snapshot)
        manifest["searches"][key] = {
            "path": str(search_path.resolve()),
            "allocation": allocation,
            "status": "attached",
            "snapshot": relative,
            "snapshot_digest": checksum,
            "strategy_digest": key,
        }
        self.save(manifest)
        raw = []
        ranking_dates: list[date] = []
        for search in manifest["searches"].values():
            if search["status"] != "attached":
                continue
            snap = self.store.read_json(search["snapshot"])
            if digest(snap) != search["snapshot_digest"]:
                raise ValidationError("search snapshot digest mismatch")
            summary = snap.get("summary") or {}
            ranking_anchor = summary.get("ranked_as_of")
            if not ranking_anchor:
                ranking_anchor = (snap.get("strategy") or {}).get("created_at")
            try:
                ranking_dates.append(date.fromisoformat(str(ranking_anchor)[:10]))
            except ValueError as exc:
                raise ValidationError("attached search has an invalid ranking date") from exc
            for values in snap["sources"].values():
                raw.extend(values)
        records = rank_records(
            deduplicate(raw),
            Question.from_dict(manifest["protocol"]["question"]),
            today=max(ranking_dates),
        )
        for record in records:
            # The evidence workspace needs the stable priority order, not a second copy of the
            # search-only heuristic scores.  ranked-results.jsonl remains the ranking audit trail.
            record.pop("ranking", None)
        for record in records:
            identity = record["canonical_id"]
            if identity.startswith("record:"):
                identity = canonical_json(
                    sorted(
                        (str(r.get("source")), str(r.get("source_id")))
                        for r in record["source_records"]
                    )
                )
            record["record_id"] = "r-" + hashlib.sha256(identity.encode()).hexdigest()[:20]
        self.put("records", {"schema_version": "1", "records": records}, validate=False)
        old_docs = self.rows("documents", fresh=False)
        valid_ids = {r["record_id"] for r in records}
        docs = [
            d
            for d in old_docs
            if d["kind"] not in {"abstract", "metadata", "registry"} and d["record_id"] in valid_ids
        ]
        for record in records:
            text = str(record.get("abstract") or record["title"])
            kind = "abstract" if record.get("abstract") else "metadata"
            if record.get("record_kind") == "registration":
                kind = "registry"
                text = json.dumps(
                    record.get("registry_data") or record, ensure_ascii=False, indent=2
                )
            docs.append(
                {
                    "document_id": record["record_id"] + ":" + kind,
                    "record_id": record["record_id"],
                    "kind": kind,
                    "url": record.get("url"),
                    "retrieved_at": record.get("retrieved_at"),
                    "segments": [{"locator": kind, "text": text}],
                }
            )
        self.put("documents", {"schema_version": "1", "records": docs}, validate=False)
        return self.status()

    def status(self) -> dict[str, Any]:
        manifest = self.load()
        stages = {}
        for stage in self.required_stages:
            present = stage in manifest["datasets"]
            stages[stage] = (
                "stale" if self.stale(stage, manifest) else "recorded" if present else "missing"
            )
        records = self.rows("records", fresh=False)
        screening = self.rows("screening", fresh=False)
        screened = (
            {r["record_id"] for r in screening} if stages["screening"] == "recorded" else set()
        )
        current = {k: v["digest"] for k, v in manifest["datasets"].items()}
        verification = self.store.read_json("verification.json", default=None)
        completion = self.store.read_json("completion.json", default=None)
        verified = bool(
            verification and verification.get("datasets") == current and verification.get("ready")
        )
        exported = bool(
            completion and completion.get("datasets") == current and completion.get("completed")
        )
        if exported:
            from .workflow import _file_digest

            exported = all(
                Path(a["path"]).resolve().is_relative_to(self.path)
                and Path(a["path"]).is_file()
                and _file_digest(Path(a["path"])) == a["sha256"]
                for a in completion["artifacts"].values()
            )
        return {
            "run_dir": str(self.path),
            "verification": "current" if verified else "pending_or_stale",
            "export": "current" if exported else "pending_or_stale",
            "claim_review": stages.get("reviews", "legacy_unreviewed"),
            "mode": manifest["protocol"]["mode"],
            "evidence_version": self.evidence_version,
            "protocol_digest": manifest["protocol_digest"],
            "stages": stages,
            "records": len(records),
            "screening_remaining": len(records) - len(screened),
            "allocated_by_source": dict(self.allocations()),
            "fulltext_attempts": len(manifest["fulltext_attempts"]),
            "limits": {k: manifest["protocol"][k] for k in ("records_per_source", "fulltexts")},
        }


def normalized_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
