"""The deep Run/Task Module behind the small ``hmr`` command interface."""

from __future__ import annotations

import json
import os
import shlex
import shutil
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from filelock import FileLock

from hermes_medical_research import __version__
from hermes_medical_research.search.artifacts import (
    ARTIFACT_SCHEMA_VERSION,
    RunStore,
    confirmation_token,
    strategy_digest,
)
from hermes_medical_research.search.cli import _load_run, _validate_preflight
from hermes_medical_research.search.config import Credentials
from hermes_medical_research.search.http import HttpSession
from hermes_medical_research.search.models import (
    BIOMEDICAL_INDEX_SOURCES,
    Question,
    ValidationError,
)
from hermes_medical_research.search.orchestrator import execute_search, preflight
from hermes_medical_research.search.providers import MeshResolver
from hermes_medical_research.search.query import compile_strategy

from . import audit
from .fulltext import fetch_fulltexts
from .packets import (
    _appraisal,
    _disposition,
    _extraction,
    _finding,
    _source,
    assessment_field_rules,
    find_in_documents,
    outcome_hits,
    scaffold_extraction_id,
    study_appraisal_id,
    synthesis_field_rules,
)
from .validation import DISPOSITION_STATUSES, METHODS
from .workflow import current_digests, finalize, submit_batch
from .workspace import OUTCOME_CONTRACT, Workspace, digest, now

TASK_ENGINE_VERSION = "1"
TASK_PACKET_VERSION = "1"
TASK_PACKET_LIMIT = 32 * 1024
SOURCE_PAGE_LIMIT = 16 * 1024
SOURCE_IDS_PER_PAGE = 100
CHECKLIST_LIMIT = 6 * 1024
MAX_CORRECTIONS_PER_GROUP = 2
# (extraction text, disposition rationale) character limits, tried in order to fit the packet.
SYNTHESIS_TEXT_LIMITS = ((600, 300), (240, 160), (0, 0))
CORE_BIOMEDICAL_SOURCES = frozenset(BIOMEDICAL_INDEX_SOURCES)
RUN_ID_PREFIX = "run-"
TASK_ID_PREFIX = "task-"

ROLE_PROFILES = {
    "coordinator": "hmr-coordinator",
    "searcher": "hmr-searcher",
    "selector": "hmr-selector",
    "extractor": "hmr-extractor",
    "synthesizer": "hmr-synthesizer",
    "auditor": "hmr-auditor",
}
# 0.5.x named every profile ``mdr-*``. Receipts in existing Runs carry those strings, so they still
# resolve to their role; nothing writes them again.
LEGACY_ROLE_PROFILES = {role: f"mdr-{name}" for role, name in (
    ("coordinator", "coordinator"),
    ("searcher", "searcher"),
    ("selector", "selector"),
    ("extractor", "extractor"),
    ("synthesizer", "synthesizer"),
    ("auditor", "auditor"),
)}
COMMAND_ROLES = {
    "search": "searcher",
    "select": "selector",
    "extract": "extractor",
    "synthesize": "synthesizer",
    "audit": "auditor",
}
KIND_ROLES = {
    "search": "searcher",
    "screening": "selector",
    "coverage": "selector",
    "fulltext": "extractor",
    "studies": "extractor",
    "assessment": "extractor",
    "synthesis": "synthesizer",
    "audit": "auditor",
}

ROLE_COMMANDS = {role: command for command, role in COMMAND_ROLES.items()}
# The CLI command that owns each Task kind, which is also the name of its step.
KIND_COMMANDS = {kind: ROLE_COMMANDS[role] for kind, role in KIND_ROLES.items()}


def data_home(environ: dict[str, str] | None = None) -> Path:
    values = environ if environ is not None else os.environ
    # MDR_HOME is the 0.5.x spelling, honored so existing operator scripts keep their store.
    configured = values.get("HMR_HOME") or values.get("MDR_HOME")
    if configured:
        root = Path(configured).expanduser()
    else:
        xdg = values.get("XDG_DATA_HOME")
        root = (
            Path(xdg).expanduser() / "hermes-medical-research"
            if xdg
            else (Path.home() / ".local" / "share" / "hermes-medical-research")
        )
    if not root.is_absolute():
        raise ValidationError("HMR_HOME and XDG_DATA_HOME must resolve to absolute paths")
    return root.resolve()


def hmr_command() -> str:
    """The resolved ``hmr`` executable, so Hermes sessions never depend on a reduced PATH."""
    return shlex.quote(shutil.which("hmr") or "hmr")


def _safe_id(value: str, prefix: str) -> str:
    if not isinstance(value, str) or not value.startswith(prefix):
        raise ValidationError(f"identifier must start with {prefix}")
    suffix = value[len(prefix) :]
    if len(suffix) != 32 or any(char not in "0123456789abcdef" for char in suffix):
        raise ValidationError(f"invalid {prefix.rstrip('-')} identifier")
    return value


def new_run_id() -> str:
    return RUN_ID_PREFIX + uuid4().hex


def new_task_id() -> str:
    return TASK_ID_PREFIX + uuid4().hex


@dataclass(frozen=True)
class Actor:
    profile: str
    session_id: str
    role: str

    @classmethod
    def resolve(cls, explicit: str | None = None, session_id: str | None = None) -> Actor:
        profile = (
            explicit
            or os.environ.get("HERMES_SESSION_PROFILE")
            or os.environ.get("HERMES_PROFILE")
            or ""
        ).strip()
        if not profile:
            raise ValidationError(
                "actor identity is required; run inside Hermes or pass --actor explicitly"
            )
        normalized = profile.casefold().replace("_", "-")
        role = next(
            (
                role_name
                for role_name, role_profile in ROLE_PROFILES.items()
                if normalized in {role_name, role_profile, LEGACY_ROLE_PROFILES[role_name]}
            ),
            "",
        )
        if not role:
            raise ValidationError(f"unrecognized medical-research actor profile: {profile}")
        session = (
            session_id
            or os.environ.get("HERMES_SESSION_ID")
            or f"cli-{os.getpid()}-{uuid4().hex[:12]}"
        ).strip()
        if not session:
            raise ValidationError("actor session identity must be nonempty")
        return cls(profile=profile, session_id=session, role=role)

    def require(self, role: str) -> None:
        if self.role != role:
            raise ValidationError(f"{role} role required; actor is {self.role}")


class RunCatalog:
    """Resolve opaque run IDs without exposing corpus paths to Bot messages."""

    def __init__(self, root: Path | None = None):
        self.root = (root or data_home()).resolve()
        self.runs = self.root / "runs"

    @property
    def lock(self) -> FileLock:
        return FileLock(str(self.root / ".catalog.lock"), timeout=120)

    def workspace(self, run_id: str) -> Workspace:
        value = _safe_id(run_id, RUN_ID_PREFIX)
        path = (self.runs / value).resolve()
        if path.parent != self.runs.resolve():
            raise ValidationError("run path escapes the artifact store")
        workspace = Workspace(path)
        workspace.load()
        manifest = workspace.load()
        if manifest.get("run_id") != value:
            raise ValidationError("run identifier does not match its immutable store location")
        return workspace

    def create(
        self,
        request: dict[str, Any],
        *,
        mode: str = "report",
        records: int | str | None = None,
        fulltexts: int | str | None = None,
        language: str = "en",
    ) -> dict[str, Any]:
        self.runs.mkdir(parents=True, exist_ok=True)
        with self.lock:
            run_id = new_run_id()
            workspace = Workspace(self.runs / run_id)
            workspace.init(
                request,
                mode=mode,
                records=records,
                fulltexts=fulltexts,
                language=language,
                evidence_version="2",
            )
            manifest = workspace.load()
            manifest.update(
                run_id=run_id,
                tool_version=__version__,
                task_engine=_empty_ledger(),
            )
            workspace.save(manifest)
        return {
            "run_id": run_id,
            "state": "created",
            "protocol_digest": workspace.load()["protocol_digest"],
        }

    def migrate(self, legacy_path: Path) -> dict[str, Any]:
        expanded = legacy_path.expanduser()
        if expanded.is_symlink():
            raise ValidationError("legacy run cannot be a symlink")
        source = expanded.resolve()
        if not source.is_dir():
            raise ValidationError("legacy run must be an existing directory")
        for path in [source, *source.rglob("*")]:
            if path.is_symlink():
                raise ValidationError(f"legacy run contains a symlink: {path.relative_to(source)}")
        legacy = Workspace(source)
        manifest = legacy.load()
        if not str(manifest.get("tool_version", "")).startswith("0.4."):
            raise ValidationError("migration accepts only v0.4 workspaces")
        if manifest.get("evidence_version") != "2":
            raise ValidationError("migration requires the v0.4 evidence schema")
        for stage in manifest.get("datasets", {}):
            legacy.read(stage, fresh=False)
        source_digest = digest(manifest)
        self.runs.mkdir(parents=True, exist_ok=True)
        with self.lock:
            run_id = new_run_id()
            staging = self.runs / f".{run_id}.staging-{os.getpid()}"
            destination = self.runs / run_id
            if staging.exists() or destination.exists():
                raise ValidationError("migration staging collision")
            try:
                shutil.copytree(source, staging)
                migrated = Workspace(staging)
                copied = migrated.load()
                provenance = staging / "provenance" / "v0.4"
                provenance.mkdir(parents=True, exist_ok=True)
                archived: dict[str, Any] = {}
                for name in ("native-review.json", "completion.json", "verification.json"):
                    path = staging / name
                    if path.is_file():
                        value = json.loads(path.read_text(encoding="utf-8"))
                        archived[name] = {
                            "digest": digest(value),
                            "file": f"provenance/v0.4/{name}",
                        }
                        os.replace(path, provenance / name)
                old_review = copied.get("datasets", {}).pop("reviews", None)
                if old_review:
                    old_path = staging / old_review["file"]
                    archived_path = provenance / "reviews.json"
                    if old_path.is_file():
                        os.replace(old_path, archived_path)
                    archived["reviews"] = {
                        **deepcopy(old_review),
                        "file": "provenance/v0.4/reviews.json",
                    }
                native_directory = staging / "native-review"
                if native_directory.is_dir():
                    os.replace(native_directory, provenance / "native-review")
                    archived["native-review-artifacts"] = {
                        "file": "provenance/v0.4/native-review"
                    }
                copied.update(
                    run_id=run_id,
                    tool_version=__version__,
                    evidence_version="2",
                    task_engine=_empty_ledger(),
                    migration={
                        "schema_version": "1",
                        "legacy_path": str(source),
                        "legacy_manifest_digest": source_digest,
                        "imported_at": now(),
                        "archived": archived,
                        "fresh_audit_required": True,
                    },
                )
                migrated.save(copied)
                for stage in copied.get("datasets", {}):
                    migrated.read(stage, fresh=False)
                os.replace(staging, destination)
            except BaseException:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        return {
            "run_id": run_id,
            "state": "migrated",
            "legacy_manifest_digest": source_digest,
            "fresh_audit_required": True,
        }


def _empty_ledger() -> dict[str, Any]:
    return {
        "schema_version": TASK_ENGINE_VERSION,
        "state": "created",
        "tasks": {},
        "order": [],
        "authors": {},
        "revision": None,
        "events": [],
    }


class TaskEngine:
    """Own task routing, packets, submissions, receipts, and legal transitions."""

    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    @property
    def run_id(self) -> str:
        value = self.workspace.load().get("run_id")
        return _safe_id(value, RUN_ID_PREFIX)

    def _ledger(self, manifest: dict[str, Any]) -> dict[str, Any]:
        ledger = manifest.get("task_engine")
        if not isinstance(ledger, dict) or ledger.get("schema_version") != TASK_ENGINE_VERSION:
            raise ValidationError("run has no supported task ledger; migrate it first")
        return ledger

    def status(self) -> dict[str, Any]:
        manifest = self.workspace.load()
        ledger = self._ledger(manifest)
        self._validate_accepted_results(ledger)
        state = self._derive_state(manifest, ledger)
        tasks = [ledger["tasks"][task_id] for task_id in ledger["order"]]
        return {
            "run_id": self.run_id,
            "state": state,
            "protocol_digest": manifest["protocol_digest"],
            "workspace": self.workspace.status(),
            "tasks": {
                value: sum(task["state"] == value for task in tasks)
                for value in ("pending", "in_progress", "accepted", "superseded", "blocked")
            },
            "active": [
                _route_view(self.run_id, task)
                for task in tasks
                if task["state"] in {"pending", "in_progress"}
            ],
            "revision": ledger.get("revision"),
        }

    def route_next(self, actor: Actor) -> dict[str, Any]:
        actor.require("coordinator")
        with self.workspace.lock:
            return self._route()

    def _route(self) -> dict[str, Any]:
        """Return the active Task or create the next one; the caller holds the workspace lock."""
        manifest = self.workspace.load()
        ledger = self._ledger(manifest)
        self._validate_accepted_results(ledger)
        self._supersede_stale(manifest, ledger)
        active = next(
            (
                ledger["tasks"][task_id]
                for task_id in ledger["order"]
                if ledger["tasks"][task_id]["state"] in {"pending", "in_progress"}
            ),
            None,
        )
        if active:
            self.workspace.save(manifest)
            return _route_view(self.run_id, active)
        if ledger.get("halt") or any(
            task["state"] == "blocked" for task in ledger["tasks"].values()
        ):
            ledger["state"] = "blocked"
            self.workspace.save(manifest)
            return {"run_id": self.run_id, "task_id": None, "state": "blocked"}
        if self._materialize_empty_stages(manifest):
            manifest = self.workspace.load()
            ledger = self._ledger(manifest)
        spec = self._next_spec(manifest, ledger)
        if spec is None:
            manifest = self.workspace.load()
            ledger = self._ledger(manifest)
            state = self._derive_state(manifest, ledger)
            ledger["state"] = state
            self.workspace.save(manifest)
            return {"run_id": self.run_id, "task_id": None, "state": state}
        task = self._create_task(manifest, ledger, spec)
        self.workspace.save(manifest)
        return _route_view(self.run_id, task)

    def role_next(
        self,
        command: str,
        task_id: str,
        actor: Actor,
        *,
        claim_token: str | None = None,
    ) -> dict[str, Any]:
        expected_role = COMMAND_ROLES[command]
        actor.require(expected_role)
        _safe_id(task_id, TASK_ID_PREFIX)
        with self.workspace.lock:
            manifest = self.workspace.load()
            ledger = self._ledger(manifest)
            self._validate_accepted_results(ledger)
            task = ledger["tasks"].get(task_id)
            if not task or task.get("role") != expected_role:
                raise ValidationError("task does not belong to this specialist role")
            self._require_claim(manifest, task, actor, claim_token)
            if task["state"] == "accepted":
                return self._receipt_view(task)
            if task["state"] not in {"pending", "in_progress"}:
                raise ValidationError(f"task is {task['state']}; request a current task")
            if task["base_digests"] != current_digests(self.workspace):
                task["state"] = "superseded"
                task["superseded_at"] = now()
                self.workspace.save(manifest)
                raise ValidationError("task inputs are stale; ask Coordinator for the next task")
            pending = task["state"] == "pending"
            self._validate_task_file(task, "proposal_file", "proposal.json")
            self._validate_packet(task)
            proposal = self.workspace.store.read_json(task["proposal_file"])
            # A retried Task keeps the previous session's partial proposal edits.
            if pending and not task.get("attempts") and digest(proposal) != task["proposal_digest"]:
                raise ValidationError("task proposal template was modified before it was opened")
            if not pending and task.get("actor_profile") != actor.profile:
                raise ValidationError("task is active under a different profile")
            task["state"] = "in_progress"
            task["actor_profile"] = actor.profile
            task.setdefault("attempts", []).append(
                {"profile": actor.profile, "session_id": actor.session_id, "started_at": now()}
            )
            ledger["state"] = _state_for_role(task["role"])
            self.workspace.save(manifest)
            base = f"{hmr_command()} --actor {ROLE_PROFILES[task['role']]}"
            view = {
                "run_id": self.run_id,
                "task_id": task_id,
                "role": task["role"],
                "kind": task["kind"],
                "packet_path": str(self.workspace.path / task["packet_file"]),
                "proposal_path": str(self.workspace.path / task["proposal_file"]),
                **source_commands(base, self.run_id, task_id),
                "submit": f"{base} {command} submit "
                f"{self.run_id} {task_id} --from "
                f"{self.workspace.path / task['proposal_file']}",
            }
            if task["kind"] == "search":
                active_plan = task.get("search_plan") or proposal
                view["resume"] = bool(task.get("search_path"))
                view["search_mode"] = active_plan.get("mode")
            return view

    async def submit(
        self,
        command: str,
        task_id: str,
        proposal_path: Path,
        actor: Actor,
        *,
        claim_token: str | None = None,
    ) -> dict[str, Any]:
        expected_role = COMMAND_ROLES[command]
        actor.require(expected_role)
        _safe_id(task_id, TASK_ID_PREFIX)
        proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
        proposal_digest = digest(proposal)
        with self.workspace.lock:
            manifest = self.workspace.load()
            ledger = self._ledger(manifest)
            self._validate_accepted_results(ledger)
            task = ledger["tasks"].get(task_id)
            self._require_submitter(manifest, task, expected_role, actor, claim_token)
            replay = self._replay(task, proposal_digest)
            if replay is not None:
                return replay
            self._validate_packet(task)
            if task["base_digests"] != current_digests(self.workspace):
                task["state"] = "superseded"
                self.workspace.save(manifest)
                raise ValidationError("task inputs changed; stale submission rejected")
            if task["kind"] == "search":
                return self._submit_search_plan(manifest, ledger, task, proposal, proposal_digest)

        if task["kind"] == "fulltext":
            result = await self._submit_fulltext(task, proposal)
            with self.workspace.lock:
                manifest = self.workspace.load()
                current_task = self._ledger(manifest)["tasks"][task_id]
                self._require_submitter(
                    manifest, current_task, expected_role, actor, claim_token
                )
                self._validate_packet(current_task)
                return self._accept(task_id, actor, proposal_digest, result)

        with self.workspace.lock:
            manifest = self.workspace.load()
            ledger = self._ledger(manifest)
            task = ledger["tasks"][task_id]
            self._require_submitter(manifest, task, expected_role, actor, claim_token)
            if task["base_digests"] != current_digests(self.workspace):
                task["state"] = "superseded"
                self.workspace.save(manifest)
                raise ValidationError("task inputs changed; stale submission rejected")
            if task["kind"] == "synthesis":
                result = self._submit_synthesis(task, proposal)
                if not result.get("accepted"):
                    return result
            elif task["kind"] == "audit":
                result = audit.validate_task_result(
                    self.workspace, self._audit_task(task), proposal
                )
                for row in [*result["records"], *result["report_reviews"]]:
                    row["audit_task_id"] = task_id
            else:
                normalized, pruned = self._validate_batch_scope(task, proposal)
                result = submit_batch(self.workspace, normalized)
                if not result["accepted"]:
                    result["next"] = (
                        "Correct the listed fields in the proposal file, then run the same "
                        "submit command again."
                    )
                    return result
                if pruned:
                    result["pruned_scaffolds"] = pruned
            return self._accept(task_id, actor, proposal_digest, result)

    def _open_source(
        self, task_id: str, actor: Actor, claim_token: str | None
    ) -> tuple[dict[str, Any], list[str]]:
        """Guard every source read by active task, claim, packet, digests, and scope."""
        _safe_id(task_id, TASK_ID_PREFIX)
        manifest = self.workspace.load()
        task = self._ledger(manifest)["tasks"].get(task_id)
        if not task or task.get("role") != actor.role or task.get("state") != "in_progress":
            raise ValidationError("source access requires the actor's active task")
        self._require_claim(manifest, task, actor, claim_token)
        self._validate_packet(task)
        if task["base_digests"] != current_digests(self.workspace):
            raise ValidationError("task inputs changed; stale source access rejected")
        allowed = sorted(set(task.get("allowed_source_ids", [])))
        if digest(allowed) != task.get("allowed_sources_digest"):
            raise ValidationError("task source scope was modified")
        return task, allowed

    def source_show(
        self,
        task_id: str,
        source_id: str,
        page: int,
        actor: Actor,
        *,
        claim_token: str | None = None,
    ) -> dict[str, Any]:
        if page < 1:
            raise ValidationError("source page must be positive")
        _, allowed = self._open_source(task_id, actor, claim_token)
        if source_id not in allowed:
            raise ValidationError("source is outside this task's bounded corpus view")
        value = self._source_value(source_id)
        rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
        chunks = utf8_pages(rendered, SOURCE_PAGE_LIMIT)
        if page > len(chunks):
            raise ValidationError(f"source has {len(chunks)} page(s)")
        return {
            "run_id": self.run_id,
            "task_id": task_id,
            "source_id": source_id,
            "page": page,
            "pages": len(chunks),
            "source_digest": digest(value),
            "content": chunks[page - 1],
        }

    def source_find(
        self,
        task_id: str,
        query: str,
        actor: Actor,
        *,
        source_id: str | None = None,
        offset: int = 0,
        limit: int = 5,
        claim_token: str | None = None,
    ) -> dict[str, Any]:
        """Rank locators in the task's documents by query terms, with short snippets."""
        _, allowed = self._open_source(task_id, actor, claim_token)
        documents = self.workspace.source_index()
        if source_id is not None:
            if source_id not in allowed:
                raise ValidationError("source is outside this task's bounded corpus view")
            if source_id not in documents:
                raise ValidationError("source find searches documents; use source show instead")
            selected = [documents[source_id]]
        else:
            selected = [documents[item] for item in allowed if item in documents]
        if not selected:
            raise ValidationError("this task has no documents to search")
        return {
            "run_id": self.run_id,
            "task_id": task_id,
            **find_in_documents(selected, query, offset=offset, limit=limit),
            "next": "Read a hit with source_read using its document_id and locator.",
        }

    def source_read(
        self,
        task_id: str,
        source_id: str,
        locator: str,
        page: int,
        actor: Actor,
        *,
        claim_token: str | None = None,
    ) -> dict[str, Any]:
        """Return one located segment's exact text, paged, for verbatim quotes."""
        if page < 1:
            raise ValidationError("source page must be positive")
        _, allowed = self._open_source(task_id, actor, claim_token)
        if source_id not in allowed:
            raise ValidationError("source is outside this task's bounded corpus view")
        document = self.workspace.source_index().get(source_id)
        if document is None:
            raise ValidationError("source read reads documents; use source show instead")
        segments = document["segments"]
        index = next(
            (i for i, segment in enumerate(segments) if segment["locator"] == locator), None
        )
        if index is None:
            raise ValidationError(
                f"unknown locator {locator!r} in {source_id}; use source_find to list locators"
            )
        chunks = utf8_pages(segments[index]["text"], SOURCE_PAGE_LIMIT)
        if page > len(chunks):
            raise ValidationError(f"segment has {len(chunks)} page(s)")
        return {
            "run_id": self.run_id,
            "task_id": task_id,
            "document_id": source_id,
            "locator": locator,
            "previous_locator": segments[index - 1]["locator"] if index else None,
            "next_locator": segments[index + 1]["locator"] if index + 1 < len(segments) else None,
            "page": page,
            "pages": len(chunks),
            "text": chunks[page - 1],
        }

    def source_list(
        self,
        task_id: str,
        page: int,
        actor: Actor,
        *,
        claim_token: str | None = None,
    ) -> dict[str, Any]:
        if page < 1:
            raise ValidationError("source page must be positive")
        _, allowed = self._open_source(task_id, actor, claim_token)
        pages = max(1, (len(allowed) + SOURCE_IDS_PER_PAGE - 1) // SOURCE_IDS_PER_PAGE)
        if page > pages:
            raise ValidationError(f"source list has {pages} page(s)")
        start = (page - 1) * SOURCE_IDS_PER_PAGE
        return {
            "run_id": self.run_id,
            "task_id": task_id,
            "page": page,
            "pages": pages,
            "source_ids": allowed[start : start + SOURCE_IDS_PER_PAGE],
        }

    async def run_search(
        self,
        task_id: str,
        actor: Actor,
        *,
        confirm_all: str | None = None,
        claim_token: str | None = None,
    ) -> dict[str, Any]:
        actor.require("searcher")
        with self.workspace.lock:
            manifest = self.workspace.load()
            ledger = self._ledger(manifest)
            task = ledger["tasks"].get(task_id)
            self._require_submitter(manifest, task, "searcher", actor, claim_token)
            if not task.get("search_plan"):
                raise ValidationError("submit the bounded search plan before running retrieval")
            child = task.get("search_path")
            plan = deepcopy(task["search_plan"])
            plan_submission_digest = task["submission_digest"]
        credentials = Credentials.from_env()
        if not child:
            question = Question.from_dict(self.workspace.load()["protocol"]["question"])
            async with HttpSession() as session:
                if plan.get("mesh", True):
                    warnings = await MeshResolver(session, credentials).resolve_question(question)
                else:
                    warnings = ["MeSH resolution was explicitly skipped by the accepted task plan."]
            strategy = compile_strategy(
                question,
                mode=plan["mode"],
                limit_per_source=plan["limit_per_source"],
                sources=plan["sources"],
                variants=plan["variants"],
            )
            strategy.warnings.extend(warnings)
            child_path = self.workspace.path / "searches" / task_id
            store = RunStore(child_path)
            child_manifest = store.initialize(question, strategy, credentials)
            child_manifest["research_parent"] = os.path.relpath(
                self.workspace.path, child_path
            )
            store.write_manifest(child_manifest)
            with self.workspace.lock:
                manifest = self.workspace.load()
                ledger = self._ledger(manifest)
                task = ledger["tasks"][task_id]
                self._require_submitter(manifest, task, "searcher", actor, claim_token)
                if not task.get("search_path"):
                    # Reservation and task linkage share one fresh manifest commit.  Saving the
                    # manifest loaded before ``reserve`` used to erase the reservation immediately.
                    self.workspace.reserve_in(manifest, child_path, strategy)
                    task["search_path"] = str(child_path)
                    task["strategy_digest"] = strategy_digest(strategy)
                    self.workspace.save(manifest)
                child = task["search_path"]
        store = RunStore(Path(child))
        strategy, child_manifest = _load_run(store)
        if task.get("strategy_digest") not in {None, strategy_digest(strategy)}:
            raise ValidationError("stored child search differs from the task's frozen strategy")
        if strategy.mode == "review" and not store.read_json("approval.json", default=None):
            return {
                "run_id": self.run_id,
                "task_id": task_id,
                "state": "awaiting_strategy_approval",
                "strategy_digest": strategy_digest(strategy),
                "strategy": strategy.to_dict(),
            }
        async with HttpSession() as session:
            inspected = store.read_json("preflight.json", default=None)
            if inspected is None:
                inspected = await preflight(strategy, session, credentials)
                store.write_json("preflight.json", inspected)
                child_manifest["status"] = (
                    "preflight_ready" if inspected["ready"] else "preflight_failed"
                )
                store.write_manifest(child_manifest)
            _validate_preflight(strategy, inspected)
            if strategy.mode == "review" and not inspected["ready"]:
                raise ValidationError("review-prep preflight failed; revise source configuration")
            if strategy.mode == "quick":
                selected_core = CORE_BIOMEDICAL_SOURCES.intersection(strategy.strategies)
                if not selected_core:
                    raise ValidationError(
                        "report search requires PubMed or Europe PMC as a core biomedical index"
                    )
                available_core = {
                    source
                    for source in selected_core
                    if (inspected["sources"].get(source) or {}).get("status") == "available"
                }
                if not available_core:
                    raise ValidationError(
                        "core_source_unavailable: PubMed and Europe PMC are unavailable; "
                        "configure one core index or revise the protocol"
                    )
                if not any(
                    int((inspected["sources"].get(source) or {}).get("count") or 0) > 0
                    for source in available_core
                ):
                    raise ValidationError(
                        "zero_core_hits: the high-recall biomedical query returned no matches; "
                        "fork the Review with revised search components"
                    )
            if strategy.limit_per_source == "all":
                counts = {
                    source: int(item["count"])
                    for source, item in inspected["sources"].items()
                    if item["status"] == "available"
                }
                expected = confirmation_token(strategy, counts)
                if confirm_all != expected:
                    return {
                        "run_id": self.run_id,
                        "task_id": task_id,
                        "state": "awaiting_all_results_confirmation",
                        "confirmation_token": expected,
                        "expected_total": sum(counts.values()),
                    }
                approval = store.read_json("approval.json")
                approval["all_results"] = {
                    "strategy_digest": strategy_digest(strategy),
                    "preflight_digest": inspected["preflight_digest"],
                    "expected_total": sum(counts.values()),
                    "confirmed_at": now(),
                }
                store.write_json("approval.json", approval)
            summary = await execute_search(strategy, store, session, credentials, inspected)
        with self.workspace.lock:
            manifest = self.workspace.load()
            task = self._ledger(manifest)["tasks"][task_id]
            self._require_submitter(manifest, task, "searcher", actor, claim_token)
            self._validate_packet(task)
            if task["base_digests"] != current_digests(self.workspace):
                task["state"] = "superseded"
                self.workspace.save(manifest)
                raise ValidationError("task inputs changed; stale search result rejected")
            self.workspace.attach(store.path)
            result = {"summary": summary, "strategy_digest": strategy_digest(strategy)}
            return self._accept(task_id, actor, plan_submission_digest, result)

    async def execute_search_plan(
        self,
        task_id: str,
        actor: Actor,
        *,
        proposal_path: Path | None = None,
        confirm_all: str | None = None,
        claim_token: str | None = None,
    ) -> dict[str, Any]:
        """Submit a first plan, or resume a frozen plan, then run deterministic retrieval."""
        manifest = self.workspace.load()
        task = self._ledger(manifest)["tasks"].get(task_id)
        self._require_submitter(manifest, task, "searcher", actor, claim_token)
        if not task.get("search_plan"):
            if proposal_path is None:
                raise ValidationError("a first search execution requires --from PROPOSAL")
            await self.submit(
                "search",
                task_id,
                proposal_path,
                actor,
                claim_token=claim_token,
            )
        elif proposal_path is not None:
            await self.submit(
                "search",
                task_id,
                proposal_path,
                actor,
                claim_token=claim_token,
            )
        return await self.run_search(
            task_id,
            actor,
            confirm_all=confirm_all,
            claim_token=claim_token,
        )

    def approve_search(
        self,
        task_id: str,
        supplied_digest: str,
        actor: Actor,
        *,
        claim_token: str | None = None,
    ) -> dict[str, Any]:
        actor.require("searcher")
        manifest = self.workspace.load()
        task = self._ledger(manifest)["tasks"].get(task_id)
        self._require_submitter(manifest, task, "searcher", actor, claim_token)
        child = task.get("search_path")
        if not child:
            raise ValidationError("run search once to materialize its review strategy")
        store = RunStore(Path(child))
        strategy, child_manifest = _load_run(store)
        current = strategy_digest(strategy)
        if strategy.mode != "review" or supplied_digest != current:
            raise ValidationError("strategy approval digest does not match a review strategy")
        approval = store.read_json("approval.json", default=None)
        if approval is None:
            approval = {
                "schema_version": ARTIFACT_SCHEMA_VERSION,
                "strategy": {
                    "strategy_digest": current,
                    "approved_at": now(),
                    "selected_variants": {
                        source: item.selected_variant
                        for source, item in strategy.strategies.items()
                    },
                },
                "all_results": None,
            }
            store.write_json("approval.json", approval)
            child_manifest["status"] = "strategy_approved"
            store.write_manifest(child_manifest)
        return {"run_id": self.run_id, "task_id": task_id, "approval": approval}

    async def finalize(self, actor: Actor, *, offline: bool = False) -> dict[str, Any]:
        actor.require("coordinator")
        with self.workspace.lock:
            manifest = self.workspace.load()
            if self._derive_state(manifest, self._ledger(manifest)) != "ready":
                raise ValidationError("run is not ready for finalization")
        result = await finalize(self.workspace, offline=offline)
        with self.workspace.lock:
            if result.get("completed"):
                manifest = self.workspace.load()
                ledger = self._ledger(manifest)
                ledger["state"] = "finalized"
                self.workspace.save(manifest)
            return result

    def _adopt_outcome_contract(self, manifest: dict[str, Any]) -> None:
        """Enforce per-outcome dispositions on modern Runs that have not assessed anything yet."""
        if (
            manifest.get("evidence_version") != "2"
            or manifest.get("assessment_contract")
            or "extractions" in manifest["datasets"]
        ):
            return
        manifest["assessment_contract"] = OUTCOME_CONTRACT
        self.workspace.save(manifest)

    def _next_spec(self, manifest: dict[str, Any], ledger: dict[str, Any]) -> dict[str, Any] | None:
        self._adopt_outcome_contract(manifest)
        revision = ledger.get("revision")
        if revision:
            return self._correction_spec(revision)
        datasets = manifest["datasets"]
        records = self.workspace.index("records") if "records" in datasets else {}
        if "records" not in datasets:
            return self._search_spec(manifest)
        screening = (
            {row["record_id"]: row for row in self.workspace.rows("screening", fresh=False)}
            if "screening" in datasets
            else {}
        )
        unscreened = [record_id for record_id in records if record_id not in screening]
        if self.workspace.stale("screening", manifest):
            unscreened = list(records)
        if unscreened:
            return self._screening_spec(unscreened[0])
        included = [
            record_id for record_id, row in screening.items() if row["decision"] == "include"
        ]
        coverage = (
            {row["record_id"]: row for row in self.workspace.rows("coverage", fresh=False)}
            if "coverage" in datasets
            else {}
        )
        uncovered = [record_id for record_id in included if record_id not in coverage]
        if self.workspace.stale("coverage", manifest):
            uncovered = included
        if uncovered:
            return self._coverage_spec(uncovered[0])
        selected = [
            record_id for record_id, row in coverage.items() if row["selection"] == "selected"
        ]
        attempts = manifest["fulltext_attempts"]
        limit = manifest["protocol"]["fulltexts"]
        capacity = limit == "all" or len(attempts) < int(limit)
        contract = self.workspace.outcome_contract
        already_extracted = {
            row["record_id"]
            for row in self.workspace.rows("extractions", fresh=False)
            if "extractions" in datasets
        }
        if contract and "dispositions" in datasets:
            already_extracted |= {
                row["record_id"] for row in self.workspace.rows("dispositions", fresh=False)
            }
        no_text = [
            record_id
            for record_id in selected
            if record_id not in attempts and record_id not in already_extracted
        ]
        if no_text and capacity:
            return self._fulltext_spec(no_text[0])
        studies = self.workspace.rows("studies", fresh=False) if "studies" in datasets else []
        by_record = {record_id: row for row in studies for record_id in row["record_ids"]}
        unlinked = [record_id for record_id in selected if record_id not in by_record]
        if self.workspace.stale("studies", manifest):
            unlinked = selected
        if unlinked:
            return self._studies_spec(unlinked[0])
        extractions = (
            self.workspace.rows("extractions", fresh=False) if "extractions" in datasets else []
        )
        appraisals = (
            self.workspace.index("appraisals")
            if "appraisals" in datasets and not self.workspace.stale("appraisals", manifest)
            else {}
        )
        dispositions = (
            self.workspace.index("dispositions")
            if contract
            and "dispositions" in datasets
            and not self.workspace.stale("dispositions", manifest)
            else {}
        )

        def finished(record_id: str) -> bool:
            rows = [row for row in extractions if row["record_id"] == record_id]
            if contract and record_id not in dispositions:
                return False
            if not contract and not rows:
                return False
            return all(
                row["extraction_id"] in appraisals
                and appraisals[row["extraction_id"]].get("completion") != "pending"
                for row in rows
            )

        unfinished = [record_id for record_id in selected if not finished(record_id)]
        if any(
            self.workspace.stale(stage, manifest)
            for stage in ("extractions", "appraisals", "dispositions")
            if stage in datasets
        ):
            unfinished = selected
        if unfinished:
            return self._assessment_spec(unfinished[0], by_record[unfinished[0]])
        synthesis = (
            self.workspace.read("synthesis", fresh=False)
            if "synthesis" in datasets
            else {"findings": []}
        )
        covered = (
            set()
            if self.workspace.stale("synthesis", manifest)
            else {
                outcome
                for finding in synthesis.get("findings", [])
                for outcome in finding.get("protocol_outcomes", [])
            }
        )
        missing_outcomes = [
            outcome for outcome in manifest["protocol"]["outcomes"] if outcome not in covered
        ]
        if missing_outcomes:
            return self._synthesis_spec(missing_outcomes[0])
        if "reviews" in datasets and not self.workspace.stale("reviews", manifest):
            reviews = self.workspace.read("reviews")
            if all(
                row.get("status") == "pass"
                for row in [*reviews["records"], *reviews["report_reviews"]]
            ):
                return None
        return self._audit_spec(manifest, ledger)

    def _search_spec(self, manifest: dict[str, Any]) -> dict[str, Any]:
        protocol = manifest["protocol"]
        limit = protocol["records_per_source"]
        allocated = limit if limit == "all" else max(1, int(limit * 0.7))
        mode = "review" if protocol["mode"] == "review-prep" else "quick"
        default_proposal = {
            "schema_version": TASK_PACKET_VERSION,
            "base_digests": current_digests(self.workspace),
            "mode": mode,
            "limit_per_source": allocated,
            "sources": protocol["question"]["sources"],
            "variants": {},
            "mesh": True,
        }
        frozen = (manifest.get("automation") or {}).get("frozen_search_plan")
        if frozen:
            proposal = {
                "schema_version": TASK_PACKET_VERSION,
                "base_digests": current_digests(self.workspace),
                **deepcopy(frozen),
            }
            instructions = (
                "Replay the Review's accepted search plan exactly, then run refresh retrieval."
            )
        else:
            proposal = default_proposal
            instructions = (
                "Validate the structured question and bounded source plan, then run retrieval."
            )
        return {
            "kind": "search",
            "target_ids": ["initial-search"],
            "instructions": instructions,
            "proposal": proposal,
            "packet_data": {
                "question": protocol["question"],
                "search_rationale": protocol["search_rationale"],
                "remaining_source_budget": limit,
            },
            "allowed_source_ids": [],
        }

    def _screening_spec(self, record_id: str) -> dict[str, Any]:
        proposal = {
            "schema_version": "2",
            "base_digests": current_digests(self.workspace),
            "stages": {
                "screening": {
                    "schema_version": "2",
                    "records": [
                        {
                            "record_id": record_id,
                            "decision": "",
                            "basis": "title-abstract",
                            "reason": "",
                        }
                    ],
                }
            },
        }
        source = _source(self.workspace, record_id)
        return {
            "kind": "screening",
            "target_ids": [record_id],
            "instructions": "Screen this record against every eligibility criterion.",
            "proposal": proposal,
            "packet_data": {
                "eligibility": self.workspace.load()["protocol"]["eligibility"],
                "source": source,
            },
            "allowed_source_ids": [row["document_id"] for row in source["documents"]],
        }

    def _coverage_spec(self, record_id: str) -> dict[str, Any]:
        proposal = {
            "schema_version": "2",
            "base_digests": current_digests(self.workspace),
            "stages": {
                "coverage": {
                    "schema_version": "2",
                    "records": [
                        {
                            "record_id": record_id,
                            "selection": "",
                            "reason": "",
                            "protocol_outcomes": [],
                        }
                    ],
                }
            },
        }
        source = _source(self.workspace, record_id)
        return {
            "kind": "coverage",
            "target_ids": [record_id],
            "instructions": (
                "Choose and justify detailed assessment coverage without changing eligibility."
            ),
            "proposal": proposal,
            "packet_data": {
                "outcomes": self.workspace.load()["protocol"]["outcomes"],
                "source": source,
            },
            "allowed_source_ids": [row["document_id"] for row in source["documents"]],
        }

    def _fulltext_spec(self, record_id: str) -> dict[str, Any]:
        source = _source(self.workspace, record_id)
        return {
            "kind": "fulltext",
            "target_ids": [record_id],
            "instructions": "Acquire the selected full text or record a genuine access failure.",
            "proposal": {
                "schema_version": TASK_PACKET_VERSION,
                "base_digests": current_digests(self.workspace),
                "record_id": record_id,
                "action": "acquire",
                "pdf": None,
                "retry": False,
            },
            "packet_data": {"source": source},
            "allowed_source_ids": [row["document_id"] for row in source["documents"]],
        }

    def _studies_spec(
        self, record_id: str, study_id: str | None = None
    ) -> dict[str, Any]:
        source = _source(self.workspace, record_id)
        existing = self.workspace.rows("studies", fresh=False)
        current = next(
            (row for row in existing if row["study_id"] == study_id),
            None,
        )
        proposal = {
            "schema_version": "2",
            "base_digests": current_digests(self.workspace),
            "stages": {
                "studies": {
                    "schema_version": "2",
                    "records": [
                        deepcopy(current)
                        if current
                        else {
                            "study_id": "study-" + record_id,
                            "record_ids": [record_id],
                            "kind": "",
                            "basis": "",
                        }
                    ],
                }
            },
        }
        existing_record_ids = {
            linked_id for row in existing for linked_id in row["record_ids"]
        }
        return {
            "kind": "studies",
            "target_ids": [record_id],
            "instructions": "Link reports of the same study using explicit identity evidence.",
            "proposal": proposal,
            "packet_data": {
                "source": source,
                "existing_study_ids": [row["study_id"] for row in existing],
                # The linked groups themselves, so a merge decision needs no source lookup and can
                # be answered in one constrained call.
                "existing_studies": [
                    {
                        "study_id": row["study_id"],
                        "record_ids": list(row["record_ids"]),
                        "kind": row.get("kind", ""),
                    }
                    for row in existing
                ],
            },
            "allowed_source_ids": [
                *[row["document_id"] for row in source["documents"]],
                *[f"study:{row['study_id']}" for row in existing],
                *[f"{linked_id}:metadata" for linked_id in existing_record_ids],
            ],
        }

    def _assessment_spec(self, record_id: str, study: dict[str, Any]) -> dict[str, Any]:
        manifest = self.workspace.load()
        outcomes = manifest["protocol"]["outcomes"]
        contract = self.workspace.outcome_contract
        existing = [
            deepcopy(row)
            for row in self.workspace.rows("extractions", fresh=False)
            if row["record_id"] == record_id
        ]
        if contract:
            bound = {row.get("protocol_outcome") for row in existing}
            identifiers = {row["extraction_id"] for row in existing}
            for index, outcome in enumerate(outcomes, start=1):
                extraction_id = scaffold_extraction_id(record_id, index)
                if outcome not in bound and extraction_id not in identifiers:
                    existing.append(
                        _extraction(
                            self.workspace, record_id, study["study_id"], extraction_id, outcome
                        )
                    )
        elif not existing:
            existing = [
                _extraction(self.workspace, record_id, study["study_id"], "result-" + record_id)
            ]
        appraisals = {
            row["extraction_id"]: row
            for row in self.workspace.rows("appraisals", fresh=False)
        }
        method = _assessment_method(study)
        if contract:
            # One full study appraisal; each new outcome row copies it unless its bias differs.
            template_id = study_appraisal_id(record_id)
            proposed_appraisals = [
                deepcopy(appraisals[row["extraction_id"]])
                if row["extraction_id"] in appraisals
                else {"extraction_id": row["extraction_id"], "same_as": template_id}
                for row in existing
            ]
            if any("same_as" in row for row in proposed_appraisals):
                proposed_appraisals.insert(0, _appraisal(template_id, method))
        else:
            proposed_appraisals = [
                deepcopy(appraisals[row["extraction_id"]])
                if row["extraction_id"] in appraisals
                else _appraisal(row["extraction_id"], method)
                for row in existing
            ]
        source = _source(self.workspace, record_id)
        source_ids = [row["document_id"] for row in source["documents"]]
        coverage = deepcopy(self.workspace.index("coverage")[record_id])
        stages: dict[str, Any] = {
            "extractions": {"schema_version": "2", "records": existing},
            "appraisals": {"schema_version": "2", "records": proposed_appraisals},
        }
        packet_data: dict[str, Any] = {
            "source": source,
            "study": study,
            "coverage": coverage,
            "protocol_outcomes": outcomes,
            "field_rules": assessment_field_rules(contract),
            "methods": {
                name: {"version": version, "domains": domains.split()}
                for name, (version, domains) in METHODS.items()
            },
        }
        if contract:
            stored = {
                row["record_id"]: row
                for row in self.workspace.rows("dispositions", fresh=False)
            }
            disposition = deepcopy(stored.get(record_id)) or _disposition(
                record_id, study["study_id"], outcomes
            )
            stages["dispositions"] = {"schema_version": "2", "records": [disposition]}
            packet_data["outcome_checklist"] = self._outcome_checklist(
                record_id, outcomes, coverage, existing
            )
            instructions = (
                "Decide every protocol outcome in outcome_checklist for this record. For each "
                "outcome, search the assigned documents with source_find and read the best "
                "locator with source_read; results are often in table:N. If the record reports "
                "the outcome, fill that outcome's prefilled extraction row (protocol_outcome is "
                "already set) and its appraisal, copy the pair with a new extraction_id for each "
                "further estimand, and set the outcome's disposition status to extracted. "
                "Otherwise set status to not_reported or not_applicable with a rationale and the "
                "inspected_locations you read. Leave unused scaffold rows unchanged; hmr removes "
                "them. Submission is rejected while any outcome is undecided."
            )
        else:
            instructions = (
                "Extract every relevant reported estimand for the selected protocol outcomes. "
                "The initial extraction and appraisal rows are scaffolds, not a one-row cap: add "
                "one distinct extraction and matching appraisal for each needed estimand, then "
                "complete every design-appropriate appraisal."
            )
        return {
            "kind": "assessment",
            "target_ids": [record_id],
            "instructions": instructions,
            "proposal": {
                "schema_version": "2",
                "base_digests": current_digests(self.workspace),
                "stages": stages,
            },
            "packet_data": packet_data,
            "allowed_source_ids": source_ids,
        }

    def _outcome_checklist(
        self,
        record_id: str,
        outcomes: list[str],
        coverage: dict[str, Any],
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """One entry per protocol outcome, degraded deterministically to fit the packet."""
        hinted = set(coverage.get("protocol_outcomes") or [])
        checklist: list[dict[str, Any]] = []
        for per_outcome in (3, 1, 0):
            hits = (
                outcome_hits(self.workspace, record_id, outcomes, per_outcome=per_outcome)
                if per_outcome
                else {outcome: [] for outcome in outcomes}
            )
            checklist = [
                {
                    "index": index,
                    "protocol_outcome": outcome,
                    "selector_flagged": outcome in hinted,
                    "extraction_ids": [
                        row["extraction_id"]
                        for row in rows
                        if row.get("protocol_outcome") == outcome
                    ],
                    "likely_locations": hits[outcome],
                }
                for index, outcome in enumerate(outcomes, start=1)
            ]
            if len(json.dumps(checklist, ensure_ascii=False).encode()) <= CHECKLIST_LIMIT:
                break
        return checklist

    def _synthesis_spec(self, outcome: str) -> dict[str, Any]:
        manifest = self.workspace.load()
        protocol = manifest["protocol"]
        index = protocol["outcomes"].index(outcome) + 1
        related = [
            row
            for row in self.workspace.rows("extractions")
            if (row["protocol_outcome"] if "protocol_outcome" in row else row.get("outcome"))
            == outcome
        ]
        logical = [f"extraction:{row['extraction_id']}" for row in related]
        logical.extend(f"appraisal:{row['extraction_id']}" for row in related)
        documents = [row["source_location"]["document_id"] for row in related]
        decisions = []
        if self.workspace.outcome_contract and "dispositions" in manifest["datasets"]:
            for row in self.workspace.rows("dispositions"):
                for item in row["outcomes"]:
                    if item["protocol_outcome"] == outcome and item["status"] != "extracted":
                        decisions.append(
                            {
                                "record_id": row["record_id"],
                                "status": item["status"],
                                "rationale": item["rationale"],
                            }
                        )
        logical.extend(f"disposition:{item['record_id']}" for item in decisions)
        packet_data: dict[str, Any] = {}
        for text_limit, rationale_limit in SYNTHESIS_TEXT_LIMITS:
            packet_data = {
                "outcome": outcome,
                "field_rules": synthesis_field_rules(),
                # 600 means nothing was shortened, so the packet is self-contained and the finding
                # can be answered in one constrained call; a lower limit needs source reads.
                "text_limit": text_limit,
                "extractions": [_synthesis_row(row, text_limit) for row in related],
                "unreported_dispositions": [
                    {
                        "record_id": item["record_id"],
                        "status": item["status"],
                        **(
                            {"rationale": item["rationale"][:rationale_limit]}
                            if rationale_limit
                            else {}
                        ),
                    }
                    for item in decisions
                ],
            }
            if text_limit == 0 or len(json.dumps(packet_data, ensure_ascii=False).encode()) <= (
                TASK_PACKET_LIMIT - 4 * 1024
            ):
                break
        return {
            "kind": "synthesis",
            "target_ids": [outcome],
            "instructions": (
                "Synthesize one protocol outcome with explicit scope, weighting, "
                "uncertainty, and gaps. Read full extraction and appraisal rows with source_show "
                "when a packet summary is shortened."
            ),
            "proposal": {
                "schema_version": TASK_PACKET_VERSION,
                "base_digests": current_digests(self.workspace),
                "title": "",
                "limitations": [],
                "finding": _finding(outcome, index),
            },
            "packet_data": packet_data,
            "allowed_source_ids": list(dict.fromkeys([*logical, *documents])),
        }

    def _audit_spec(
        self, manifest: dict[str, Any], ledger: dict[str, Any]
    ) -> dict[str, Any] | None:
        candidate_digest, groups = audit.audit_groups(self.workspace)
        accepted: dict[tuple[str, str], dict[str, Any]] = {}
        for task_id in ledger["order"]:
            task = ledger["tasks"][task_id]
            if task.get("kind") == "audit" and task.get("state") == "accepted":
                accepted[(task.get("group_id"), task.get("group_digest"))] = task
        for group in groups:
            if (group["group_id"], group["group_digest"]) not in accepted:
                return {
                    "kind": "audit",
                    "target_ids": [row["target_id"] for row in group["targets"]],
                    "instructions": (
                        "Independently assess every frozen assertion and cite exact "
                        "source locations."
                    ),
                    "proposal": group["proposal"],
                    "packet_data": {
                        "candidate_digest": candidate_digest,
                        "audit_contract_version": audit.AUDIT_CONTRACT_VERSION,
                        "audit_group": {
                            key: value
                            for key, value in group.items()
                            if key not in {"proposal", "allowed_document_ids"}
                        },
                        "citation_contract": audit.citation_contract(self.workspace),
                    },
                    "allowed_source_ids": group["allowed_document_ids"],
                    "candidate_digest": candidate_digest,
                    "group_id": group["group_id"],
                    "group_digest": group["group_digest"],
                }
        rows: list[dict[str, Any]] = []
        report_rows: list[dict[str, Any]] = []
        revise_tasks: list[dict[str, Any]] = []
        for group in groups:
            task = accepted[(group["group_id"], group["group_digest"])]
            result = self.workspace.store.read_json(task["result_file"])
            rows.extend(result["records"])
            report_rows.extend(result["report_reviews"])
            if any(
                row.get("status") == "revise"
                for row in [*result["records"], *result["report_reviews"]]
            ):
                revise_tasks.append(task)
        if revise_tasks:
            # Correct one audit group at a time; the others keep their receipts until then.
            first = revise_tasks[0]
            counts = ledger.setdefault("correction_counts", {})
            if counts.get(first["group_id"], 0) >= MAX_CORRECTIONS_PER_GROUP:
                ledger["halt"] = {
                    "code": "audit_unresolved",
                    "group_id": first["group_id"],
                    "audit_task_id": first["task_id"],
                    "at": now(),
                }
                ledger["state"] = "blocked"
                ledger["events"].append(
                    {
                        "event": "run.halted",
                        "code": "audit_unresolved",
                        "task_id": first["task_id"],
                        "at": now(),
                    }
                )
                self.workspace.save(manifest)
                return None
            counts[first["group_id"]] = counts.get(first["group_id"], 0) + 1
            ledger["revision"] = {
                "audit_task_ids": [first["task_id"]],
                "group_id": first["group_id"],
                "candidate_digest": candidate_digest,
                "created_at": now(),
            }
            ledger["state"] = "revision_required"
            self.workspace.save(manifest)
            return self._correction_spec(ledger["revision"])
        self.workspace.put(
            "reviews",
            {
                "schema_version": "2",
                "records": rows,
                "report_reviews": report_rows,
                "audit_contract_version": audit.AUDIT_CONTRACT_VERSION,
            },
        )
        return None

    def _correction_spec(self, revision: dict[str, Any]) -> dict[str, Any]:
        group_id = revision["group_id"]
        if group_id.startswith("finding:"):
            finding_id = group_id.split(":", 1)[1]
            finding = next(
                row
                for row in self.workspace.read("synthesis")["findings"]
                if row["finding_id"] == finding_id
            )
            spec = self._synthesis_spec(finding["protocol_outcomes"][0])
        else:
            task_id = revision["audit_task_ids"][0]
            task = self._ledger(self.workspace.load())["tasks"][task_id]
            group = self._audit_task(task)["audit_group"]
            revised = set()
            if task.get("result_file"):
                result = self.workspace.store.read_json(task["result_file"], default=None) or {}
                revised = {
                    row.get("target_id")
                    for row in result.get("report_reviews", [])
                    if row.get("status") == "revise"
                }
            target = next(
                (item for item in group["targets"] if item.get("target_id") in revised),
                group["targets"][0],
            )
            kind = target["kind"]
            if kind == "screening":
                spec = self._screening_spec(target["entity_id"])
            elif kind == "coverage":
                spec = self._coverage_spec(target["entity_id"])
            elif kind == "studies":
                record_id = next(
                    row["record_ids"][0]
                    for row in self.workspace.rows("studies")
                    if row["study_id"] == target["entity_id"]
                )
                spec = self._studies_spec(record_id, target["entity_id"])
            elif kind in {"extractions", "appraisals"}:
                extraction = self.workspace.index("extractions")[target["entity_id"]]
                study = self.workspace.index("studies")[extraction["study_id"]]
                spec = self._assessment_spec(extraction["record_id"], study)
            elif kind == "dispositions":
                disposition = self.workspace.index("dispositions")[target["entity_id"]]
                study = self.workspace.index("studies")[disposition["study_id"]]
                spec = self._assessment_spec(target["entity_id"], study)
            else:
                outcome = self.workspace.load()["protocol"]["outcomes"][0]
                spec = self._synthesis_spec(outcome)
        spec["correction_for"] = revision["audit_task_ids"]
        return spec

    def _create_task(
        self, manifest: dict[str, Any], ledger: dict[str, Any], spec: dict[str, Any]
    ) -> dict[str, Any]:
        task_id = new_task_id()
        role = KIND_ROLES[spec["kind"]]
        allowed_sources = sorted(set(spec["allowed_source_ids"]))
        relative = f"tasks/{task_id}"
        proposal_file = f"{relative}/proposal.json"
        packet_file = f"{relative}/packet.json"
        proposal = deepcopy(spec["proposal"])
        packet = {
            "schema_version": TASK_PACKET_VERSION,
            "run_id": self.run_id,
            "task_id": task_id,
            "role": role,
            "kind": spec["kind"],
            "target_ids": spec["target_ids"],
            "instructions": spec["instructions"],
            "base_digests": current_digests(self.workspace),
            "proposal_path": str(self.workspace.path / proposal_file),
            "source_count": len(allowed_sources),
            "source_list": (
                f"{hmr_command()} --actor {ROLE_PROFILES[role]} source list {self.run_id} {task_id}"
            ),
            **spec["packet_data"],
        }
        raw = json.dumps(packet, ensure_ascii=False, sort_keys=True).encode()
        if len(raw) > TASK_PACKET_LIMIT:
            raise ValidationError(
                f"bounded task packet exceeds {TASK_PACKET_LIMIT} bytes; split its target group"
            )
        self.workspace.store.write_json(proposal_file, proposal)
        self.workspace.store.write_json(packet_file, packet)
        task = {
            "task_id": task_id,
            "role": role,
            "kind": spec["kind"],
            "target_ids": spec["target_ids"],
            "state": "pending",
            "base_digests": current_digests(self.workspace),
            "packet_file": packet_file,
            "packet_digest": digest(packet),
            "proposal_file": proposal_file,
            "proposal_digest": digest(proposal),
            "allowed_source_ids": allowed_sources,
            "allowed_sources_digest": digest(allowed_sources),
            "created_at": now(),
            "attempts": [],
        }
        for key in ("candidate_digest", "group_id", "group_digest", "correction_for"):
            if key in spec:
                task[key] = spec[key]
        ledger["tasks"][task_id] = task
        ledger["order"].append(task_id)
        ledger["state"] = _state_for_role(role)
        ledger["events"].append(
            {"event": "task.created", "task_id": task_id, "role": role, "at": now()}
        )
        return task

    def _validate_batch_scope(
        self, task: dict[str, Any], proposal: Any
    ) -> tuple[dict[str, Any], list[str]]:
        """Check exact task scope; normalize assessment scaffolds and outcome bindings."""
        if not isinstance(proposal, dict) or proposal.get("base_digests") != task["base_digests"]:
            raise ValidationError("proposal base_digests differ from the task")
        stages = proposal.get("stages")
        if not isinstance(stages, dict):
            raise ValidationError("proposal stages must be an object")
        target = set(task["target_ids"])
        if task["kind"] in {"screening", "coverage"}:
            stage = task["kind"]
            rows = (stages.get(stage) or {}).get("records")
            if (
                not isinstance(rows, list)
                or not all(isinstance(row, dict) for row in rows)
                or {row.get("record_id") for row in rows} != target
            ):
                raise ValidationError("proposal must contain exactly the assigned record")
        elif task["kind"] == "studies":
            rows = (stages.get("studies") or {}).get("records")
            if (
                not isinstance(rows, list)
                or len(rows) != 1
                or not isinstance(rows[0], dict)
                or not isinstance(rows[0].get("record_ids"), list)
                or not all(isinstance(value, str) for value in rows[0].get("record_ids", []))
            ):
                raise ValidationError("study proposal must contain one assigned study group")
            row = rows[0]
            record_ids = set(row.get("record_ids", []))
            if not target <= record_ids:
                raise ValidationError("study proposal must include the assigned record")
            existing = {
                item["study_id"]: item
                for item in self.workspace.rows("studies", fresh=False)
            }
            previous = existing.get(row.get("study_id"))
            if previous is None:
                if record_ids != target:
                    raise ValidationError("a new study may contain only the assigned record")
            elif set(previous["record_ids"]) - record_ids or record_ids - set(
                previous["record_ids"]
            ) - target:
                raise ValidationError("study update may only add the assigned record")
        elif task["kind"] == "assessment":
            return self._assessment_scope(task, proposal, stages, target)
        return proposal, []

    def _assessment_scope(
        self,
        task: dict[str, Any],
        proposal: dict[str, Any],
        stages: dict[str, Any],
        target: set[str],
    ) -> tuple[dict[str, Any], list[str]]:
        extractions = (stages.get("extractions") or {}).get("records")
        appraisals = (stages.get("appraisals") or {}).get("records")
        if (
            not isinstance(extractions, list)
            or not isinstance(appraisals, list)
            or not all(isinstance(row, dict) for row in [*extractions, *appraisals])
        ):
            raise ValidationError("assessment requires extraction and appraisal records")
        if not self.workspace.outcome_contract:
            if {row.get("record_id") for row in extractions} != target:
                raise ValidationError("extractions must cover only the assigned record")
            extraction_ids = {row.get("extraction_id") for row in extractions}
            if {row.get("extraction_id") for row in appraisals} != extraction_ids:
                raise ValidationError("every assigned extraction requires one appraisal")
            return proposal, []
        (record_id,) = tuple(target)
        dispositions = (stages.get("dispositions") or {}).get("records")
        if (
            not isinstance(dispositions, list)
            or len(dispositions) != 1
            or not isinstance(dispositions[0], dict)
            or dispositions[0].get("record_id") != record_id
        ):
            raise ValidationError(
                "assessment requires exactly one dispositions row for the assigned record"
            )
        outcomes = self.workspace.load()["protocol"]["outcomes"]
        scaffolds = {
            scaffold_extraction_id(record_id, index): outcome
            for index, outcome in enumerate(outcomes, start=1)
        }
        appraisals = self._expand_same_as(record_id, appraisals)
        pruned: list[str] = []
        kept: list[dict[str, Any]] = []
        for row in extractions:
            extraction_id = row.get("extraction_id")
            if extraction_id in scaffolds and row == _extraction(
                self.workspace,
                record_id,
                row.get("study_id"),
                extraction_id,
                scaffolds[extraction_id],
            ):
                pruned.append(extraction_id)
                continue
            kept.append(row)
        kept_appraisals = [row for row in appraisals if row.get("extraction_id") not in pruned]
        if any(row.get("record_id") != record_id for row in kept):
            raise ValidationError("extractions must cover only the assigned record")
        extraction_ids = [row.get("extraction_id") for row in kept]
        appraisal_ids = [row.get("extraction_id") for row in kept_appraisals]
        without_appraisal = sorted(str(value) for value in set(extraction_ids) - set(appraisal_ids))
        orphaned = sorted(str(value) for value in set(appraisal_ids) - set(extraction_ids))
        if without_appraisal or orphaned or len(set(appraisal_ids)) != len(appraisal_ids):
            detail = "every extraction needs exactly one appraisal with the same extraction_id"
            if without_appraisal:
                detail += f"; missing appraisal: {', '.join(without_appraisal)}"
            if orphaned:
                detail += f"; appraisal without extraction: {', '.join(orphaned)}"
            raise ValidationError(detail)
        problems: list[str] = [
            f"appraisal for {row.get('extraction_id')} is still pending; assess each domain or "
            "mark it unavailable with a missing_reason"
            for row in kept_appraisals
            if row.get("completion") == "pending"
        ]
        merged = {
            row["extraction_id"]: row
            for row in self.workspace.rows("extractions", fresh=False)
            if row["record_id"] == record_id
        }
        merged.update(
            {row["extraction_id"]: row for row in kept if isinstance(row.get("extraction_id"), str)}
        )
        bound: dict[str, list[str]] = {}
        for extraction_id, row in sorted(merged.items()):
            value = row.get("protocol_outcome")
            if value not in outcomes:
                problems.append(
                    f"extraction {extraction_id} needs protocol_outcome set to one of: "
                    + "; ".join(outcomes)
                )
                continue
            bound.setdefault(value, []).append(extraction_id)
        disposition = deepcopy(dispositions[0])
        items = disposition.get("outcomes")
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise ValidationError("dispositions.outcomes must be an array of objects")
        named = [item.get("protocol_outcome") for item in items]
        missing = [outcome for outcome in outcomes if outcome not in named]
        if missing:
            problems.append("add a dispositions entry for: " + "; ".join(missing))
        for item in items:
            outcome = item.get("protocol_outcome")
            if outcome not in outcomes:
                problems.append(f"remove the unknown dispositions entry {outcome!r}")
                continue
            if named.count(outcome) > 1:
                problems.append(f"decide outcome {outcome!r} only once")
                continue
            status = item.get("status")
            ids = bound.get(outcome, [])
            if status not in DISPOSITION_STATUSES:
                problems.append(
                    f"outcome {outcome!r} is undecided; set status to "
                    + ", ".join(DISPOSITION_STATUSES)
                )
                continue
            supplied = item.get("extraction_ids") or []
            if status == "extracted":
                if not ids:
                    problems.append(
                        f"outcome {outcome!r} is extracted but no filled extraction row has "
                        f"protocol_outcome {outcome!r}"
                    )
                elif supplied and sorted(supplied) != ids:
                    problems.append(
                        f"outcome {outcome!r} extraction_ids must be {', '.join(ids)} "
                        "(or leave the list empty for hmr to fill)"
                    )
                item["extraction_ids"] = ids
                continue
            if ids:
                problems.append(
                    f"outcome {outcome!r} is {status} but extraction rows are bound to it: "
                    f"{', '.join(ids)}; set status to extracted or change their protocol_outcome"
                )
            item["extraction_ids"] = []
            if not isinstance(item.get("rationale"), str) or not item["rationale"].strip():
                problems.append(f"outcome {outcome!r} needs a rationale for {status}")
            locations = item.get("inspected_locations")
            if not isinstance(locations, list) or not locations:
                problems.append(
                    f"outcome {outcome!r} needs inspected_locations: the document_id and "
                    "locator of each section or table you read"
                )
        if problems:
            raise ValidationError("assessment is incomplete: " + " | ".join(problems))
        normalized = deepcopy(proposal)
        normalized["stages"]["extractions"]["records"] = kept
        normalized["stages"]["appraisals"]["records"] = kept_appraisals
        normalized["stages"]["dispositions"]["records"] = [disposition]
        return normalized, pruned

    @staticmethod
    def _expand_same_as(
        record_id: str, appraisals: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Replace ``same_as`` rows with copies of full appraisals; drop the study template."""
        template_id = study_appraisal_id(record_id)
        full = {row.get("extraction_id"): row for row in appraisals if "same_as" not in row}
        problems = []
        expanded = []
        for row in appraisals:
            if "same_as" not in row:
                if row.get("extraction_id") != template_id:
                    expanded.append(row)
                continue
            source = full.get(row.get("same_as"))
            if source is None:
                problems.append(
                    f"appraisal {row.get('extraction_id')} same_as must name a full appraisal "
                    f"in this proposal, such as {template_id}"
                )
                continue
            copy = deepcopy(source)
            copy["extraction_id"] = row.get("extraction_id")
            expanded.append(copy)
        if problems:
            raise ValidationError("assessment is incomplete: " + " | ".join(problems))
        return expanded

    def _submit_synthesis(self, task: dict[str, Any], proposal: Any) -> dict[str, Any]:
        if not isinstance(proposal, dict) or proposal.get("schema_version") != TASK_PACKET_VERSION:
            raise ValidationError("synthesis proposal schema_version must be '1'")
        if proposal.get("base_digests") != task["base_digests"]:
            raise ValidationError("synthesis proposal base_digests differ")
        finding = proposal.get("finding")
        outcomes = finding.get("protocol_outcomes") if isinstance(finding, dict) else None
        if (
            not isinstance(outcomes, list)
            or not all(isinstance(outcome, str) for outcome in outcomes)
            or set(outcomes) != set(task["target_ids"])
        ):
            raise ValidationError("synthesis finding must cover exactly the assigned outcome")
        manifest = self.workspace.load()
        existing = (
            self.workspace.read("synthesis", fresh=False)
            if "synthesis" in manifest["datasets"]
            and not self.workspace.stale("synthesis", manifest)
            else {"title": "", "limitations": [], "findings": []}
        )
        findings = [
            row
            for row in existing.get("findings", [])
            if not set(row.get("protocol_outcomes", [])) & set(task["target_ids"])
        ]
        findings.append(finding)
        title = proposal.get("title") or existing.get("title")
        limitations = proposal.get("limitations") or existing.get("limitations")
        batch = {
            "schema_version": "2",
            "base_digests": task["base_digests"],
            "stages": {
                "synthesis": {
                    "schema_version": "2",
                    "title": title,
                    "limitations": limitations,
                    "findings": findings,
                }
            },
        }
        result = submit_batch(self.workspace, batch)
        if not result["accepted"]:
            return result
        return result

    async def _submit_fulltext(self, task: dict[str, Any], proposal: Any) -> dict[str, Any]:
        if not isinstance(proposal, dict) or proposal.get("schema_version") != TASK_PACKET_VERSION:
            raise ValidationError("fulltext proposal schema_version must be '1'")
        if proposal.get("base_digests") != task["base_digests"]:
            raise ValidationError("fulltext proposal base_digests differ")
        if (
            proposal.get("record_id") not in task["target_ids"]
            or proposal.get("action") != "acquire"
        ):
            raise ValidationError("fulltext proposal differs from its assigned target")
        pdf = proposal.get("pdf")
        pdf_path = Path(pdf).expanduser().resolve() if isinstance(pdf, str) and pdf else None
        return await fetch_fulltexts(
            self.workspace,
            [proposal["record_id"]],
            pdf=pdf_path,
            retry=proposal.get("retry") is True,
        )

    def _submit_search_plan(
        self,
        manifest: dict[str, Any],
        ledger: dict[str, Any],
        task: dict[str, Any],
        proposal: Any,
        proposal_digest: str,
    ) -> dict[str, Any]:
        if not isinstance(proposal, dict) or proposal.get("schema_version") != TASK_PACKET_VERSION:
            raise ValidationError("search proposal schema_version must be '1'")
        if proposal.get("base_digests") != task["base_digests"]:
            raise ValidationError("search proposal base_digests differ")
        protocol = manifest["protocol"]
        expected_mode = "review" if protocol["mode"] == "review-prep" else "quick"
        if proposal.get("mode") != expected_mode:
            raise ValidationError("search mode differs from the run protocol")
        sources = proposal.get("sources")
        allowed = set(protocol["question"]["sources"])
        if not isinstance(sources, list) or not sources or set(sources) - allowed:
            raise ValidationError("search sources must be a nonempty protocol subset")
        limit = proposal.get("limit_per_source")
        if limit != "all" and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
            raise ValidationError("limit_per_source must be positive or all")
        if limit == "all" and protocol["records_per_source"] != "all":
            raise ValidationError("all-results plan exceeds the run protocol")
        if (
            isinstance(limit, int)
            and protocol["records_per_source"] != "all"
            and limit > protocol["records_per_source"]
        ):
            raise ValidationError("search plan exceeds the per-source budget")
        variants = proposal.get("variants")
        if not isinstance(variants, dict) or set(variants) - set(sources):
            raise ValidationError("search variants must name selected sources")
        if any(value not in {"sensitivity", "precision"} for value in variants.values()):
            raise ValidationError("search variants must be sensitivity or precision")
        plan = {
            "mode": expected_mode,
            "limit_per_source": limit,
            "sources": list(dict.fromkeys(sources)),
            "variants": variants,
            "mesh": proposal.get("mesh") is not False,
        }
        if task.get("search_path") and digest(plan) != digest(task.get("search_plan")):
            raise ValidationError(
                "search plan is frozen after retrieval begins; resume it or fork the Review"
            )
        frozen = (manifest.get("automation") or {}).get("frozen_search_plan")
        if frozen is not None and digest(plan) != digest(frozen):
            raise ValidationError(
                "refresh search plan differs from the Review's accepted strategy; fork the Review"
            )
        task["search_plan"] = plan
        task["search_plan_digest"] = digest(plan)
        task["submission_digest"] = proposal_digest
        task["plan_recorded_at"] = now()
        self.workspace.save(manifest)
        return {
            "run_id": self.run_id,
            "task_id": task["task_id"],
            "accepted": True,
            "state": "plan_recorded",
            "plan_digest": digest(plan),
            "next": (
                f"{hmr_command()} --actor hmr-searcher search run {self.run_id} {task['task_id']}"
            ),
        }

    def _accept(
        self,
        task_id: str,
        actor: Actor,
        proposal_digest: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        manifest = self.workspace.load()
        ledger = self._ledger(manifest)
        task = ledger["tasks"][task_id]
        result_file = f"tasks/{task_id}/result-{digest(result)}.json"
        self.workspace.store.write_json(result_file, result)
        task.update(
            state="accepted",
            actor_profile=actor.profile,
            actor_session_id=actor.session_id,
            submission_digest=proposal_digest,
            result_file=result_file,
            result_digest=digest(result),
            accepted_at=now(),
        )
        if task.get("lease"):
            task["lease"]["state"] = "completed"
            task["lease"]["completed_at"] = task["accepted_at"]
        if task["role"] != "auditor":
            authors = ledger["authors"].setdefault(task["kind"], [])
            if actor.profile not in authors:
                authors.append(actor.profile)
        if task.get("correction_for"):
            ledger["revision"] = None
            for old_id in task["correction_for"]:
                old = ledger["tasks"].get(old_id)
                if old and old["state"] == "accepted":
                    old["state"] = "superseded"
                    old["superseded_at"] = now()
        ledger["events"].append({"event": "task.accepted", "task_id": task_id, "at": now()})
        ledger["state"] = self._derive_state(manifest, ledger)
        self.workspace.save(manifest)
        self._complete_claim_file(task)
        automation = manifest.get("automation") or {}
        # A refresh Cycle must be reconciled by the Coordinator tick before routing on.
        awaiting_refresh = automation.get("predecessor_run_id") and not automation.get(
            "refresh_reconciled"
        )
        if automation and not awaiting_refresh:
            try:
                routed = self._route()
            except ValidationError as exc:
                manifest = self.workspace.load()
                self._ledger(manifest)["events"].append(
                    {
                        "event": "task.route_deferred",
                        "task_id": task_id,
                        "error": str(exc)[:500],
                        "at": now(),
                    }
                )
                self.workspace.save(manifest)
            else:
                if routed.get("task_id") and routed["task_id"] != task_id:
                    manifest = self.workspace.load()
                    stored = self._ledger(manifest)["tasks"][task_id]
                    stored["continuation_task_id"] = routed["task_id"]
                    self.workspace.save(manifest)
        return self._receipt_view(self._ledger(self.workspace.load())["tasks"][task_id])

    def _complete_claim_file(self, task: dict[str, Any]) -> None:
        """Close the store-level claim record once its leased Task is accepted."""
        lease = task.get("lease") or {}
        claim_id = lease.get("claim_id")
        if not isinstance(claim_id, str) or not claim_id.startswith("claim-"):
            return
        path = self.workspace.path.parent.parent / "claims" / f"{claim_id}.json"
        if not path.is_file():
            return
        try:
            claim = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if claim.get("state") != "active" or claim.get("task_id") != task["task_id"]:
            return
        claim.update(state="completed", completed_at=task.get("accepted_at") or now())
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(claim, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)

    def task_automation(self, task_id: str) -> dict[str, Any]:
        """Return the bounded scheduling fields for one Task."""
        _safe_id(task_id, TASK_ID_PREFIX)
        manifest = self.workspace.load()
        task = self._ledger(manifest)["tasks"].get(task_id)
        if not task:
            raise ValidationError("unknown task")
        return {
            "task_id": task_id,
            "role": task["role"],
            "kind": task["kind"],
            "target_ids": list(task["target_ids"]),
            "state": task["state"],
            "created_at": task["created_at"],
            "available_at": task.get("available_at"),
            "automation_attempts": task.get("automation_attempts", 0),
            "lease": deepcopy(task.get("lease")),
            # A step reads these to pick the answering surface and to show what it would send.
            "correction_for": list(task.get("correction_for") or ()) or None,
            "packet_path": str(self.workspace.path / task["packet_file"]),
            "proposal_path": str(self.workspace.path / task["proposal_file"]),
        }

    def cancel_open_tasks(self, *, reason: str, at: str) -> dict[str, Any]:
        """Fail closed while preserving every accepted result and task artifact."""
        message = str(reason).strip()
        if not message:
            raise ValidationError("cancellation reason must be nonempty")
        cancelled: list[str] = []
        with self.workspace.lock:
            manifest = self.workspace.load()
            ledger = self._ledger(manifest)
            for task in ledger["tasks"].values():
                if task["state"] not in {"pending", "in_progress"}:
                    continue
                task["state"] = "blocked"
                task["blocked_at"] = at
                task["blocked_code"] = "operator_cancelled"
                task["blocked_reason"] = message
                lease = task.get("lease")
                if lease and lease.get("state") == "active":
                    lease.update(state="cancelled", cancelled_at=at)
                cancelled.append(task["task_id"])
                ledger["events"].append(
                    {
                        "event": "task.blocked",
                        "task_id": task["task_id"],
                        "code": "operator_cancelled",
                        "at": at,
                    }
                )
            if not cancelled:
                raise ValidationError("Review has no open task to cancel")
            ledger["state"] = "blocked"
            self.workspace.save(manifest)
        return {
            "run_id": self.run_id,
            "state": "blocked",
            "code": "operator_cancelled",
            "reason": message,
            "task_ids": cancelled,
        }

    def retry_blocked(self, *, reason: str, at: str) -> dict[str, Any]:
        """Return operator- or worker-blocked Tasks to the queue with fresh attempts."""
        message = str(reason).strip()
        if not message:
            raise ValidationError("retry reason must be nonempty")
        retried: list[str] = []
        with self.workspace.lock:
            manifest = self.workspace.load()
            ledger = self._ledger(manifest)
            for task in ledger["tasks"].values():
                if task["state"] != "blocked":
                    continue
                code = task.get("blocked_code") or (task.get("lease") or {}).get("code")
                task.setdefault("retries", []).append(
                    {
                        "at": at,
                        "reason": message,
                        "code": code,
                        "automation_attempts": task.get("automation_attempts", 0),
                    }
                )
                for key in (
                    "lease",
                    "blocked_at",
                    "blocked_code",
                    "blocked_reason",
                    "actor_profile",
                    "actor_session_id",
                ):
                    task.pop(key, None)
                task.update(state="pending", automation_attempts=0, available_at=at)
                retried.append(task["task_id"])
                ledger["events"].append(
                    {"event": "task.retried", "task_id": task["task_id"], "code": code, "at": at}
                )
            halt = ledger.pop("halt", None)
            if halt:
                ledger.setdefault("correction_counts", {}).pop(halt.get("group_id"), None)
                ledger["events"].append(
                    {"event": "run.halt_cleared", "code": halt.get("code"), "at": at}
                )
            if not retried and not halt:
                raise ValidationError("run has no blocked task or halt to retry")
            ledger["state"] = self._derive_state(manifest, ledger)
            self.workspace.save(manifest)
        return {
            "run_id": self.run_id,
            "retried_task_ids": retried,
            "halt_cleared": halt,
            "state": ledger["state"],
        }

    def lease_task(
        self,
        task_id: str,
        actor: Actor,
        *,
        claim_id: str,
        token: str,
        expires_at: str,
        at: str | None = None,
    ) -> dict[str, Any]:
        """Atomically grant one bounded worker lease for a managed Task."""
        _safe_id(task_id, TASK_ID_PREFIX)
        with self.workspace.lock:
            manifest = self.workspace.load()
            if not manifest.get("automation"):
                raise ValidationError("leases are only available for Review-managed Runs")
            ledger = self._ledger(manifest)
            task = ledger["tasks"].get(task_id)
            if not task or task.get("role") != actor.role:
                raise ValidationError("task does not belong to this specialist role")
            if task["state"] != "pending" or task.get("lease"):
                raise ValidationError("task is not available for claim")
            available = task.get("available_at")
            current = datetime.fromisoformat(at) if at else datetime.now(UTC)
            if available and current < datetime.fromisoformat(available):
                raise ValidationError("task retry is not available yet")
            attempt = int(task.get("automation_attempts", 0)) + 1
            if attempt > 3:
                raise ValidationError("task exhausted its bounded retry attempts")
            task["automation_attempts"] = attempt
            task["lease"] = {
                "claim_id": claim_id,
                "token_digest": digest(token),
                "actor_profile": actor.profile,
                "actor_session_id": actor.session_id,
                "expires_at": expires_at,
                "state": "active",
            }
            ledger["events"].append(
                {
                    "event": "task.leased",
                    "task_id": task_id,
                    "claim_id": claim_id,
                    "attempt": attempt,
                    "at": now(),
                }
            )
            self.workspace.save(manifest)
        return {"task_id": task_id, "claim_id": claim_id, "attempt": attempt}

    def renew_lease(
        self, task_id: str, claim_id: str, *, expires_at: str, at: str | None = None
    ) -> dict[str, Any]:
        """Extend an unexpired active lease while its host session is still working."""
        _safe_id(task_id, TASK_ID_PREFIX)
        current = datetime.fromisoformat(at) if at else datetime.now(UTC)
        with self.workspace.lock:
            manifest = self.workspace.load()
            task = self._ledger(manifest)["tasks"].get(task_id)
            lease = task.get("lease") if task else None
            if not task or not lease or lease.get("claim_id") != claim_id:
                raise ValidationError("claim no longer owns this task")
            if lease.get("state") != "active" or task.get("state") not in {
                "pending",
                "in_progress",
            }:
                raise ValidationError("claim lease is not active")
            if current >= datetime.fromisoformat(lease["expires_at"]):
                raise ValidationError("claim lease already expired")
            lease["expires_at"] = expires_at
            lease["renewals"] = int(lease.get("renewals", 0)) + 1
            self.workspace.save(manifest)
        return {"task_id": task_id, "claim_id": claim_id, "expires_at": expires_at}

    def fail_lease(
        self,
        task_id: str,
        claim_id: str,
        *,
        code: str,
        message: str,
        at: str | None = None,
    ) -> dict[str, Any]:
        """Release a failed lease, apply bounded backoff, or block the Task."""
        _safe_id(task_id, TASK_ID_PREFIX)
        failure_at = datetime.fromisoformat(at) if at else datetime.now(UTC)
        with self.workspace.lock:
            manifest = self.workspace.load()
            ledger = self._ledger(manifest)
            task = ledger["tasks"].get(task_id)
            lease = task.get("lease") if task else None
            if not task or not lease or lease.get("claim_id") != claim_id:
                raise ValidationError("claim no longer owns this task")
            if lease.get("state") != "active":
                raise ValidationError("claim lease is not active")
            attempt = int(task.get("automation_attempts", 0))
            lease.update(state="failed", failed_at=failure_at.isoformat(), code=code)
            task.setdefault("automation_failures", []).append(
                {
                    "claim_id": claim_id,
                    "attempt": attempt,
                    "code": code,
                    "message": message,
                    "at": failure_at.isoformat(),
                }
            )
            task.pop("actor_profile", None)
            task.pop("actor_session_id", None)
            if attempt >= 3:
                task["state"] = "blocked"
                task["blocked_at"] = failure_at.isoformat()
                blocked = True
                retry_at = None
            else:
                delay = (5, 30)[attempt - 1]
                retry = failure_at + timedelta(minutes=delay)
                task["state"] = "pending"
                task["available_at"] = retry.isoformat()
                task.pop("lease", None)
                blocked = False
                retry_at = retry.isoformat()
            ledger["events"].append(
                {
                    "event": "task.blocked" if blocked else "task.retry_scheduled",
                    "task_id": task_id,
                    "claim_id": claim_id,
                    "attempt": attempt,
                    "at": failure_at.isoformat(),
                }
            )
            self.workspace.save(manifest)
        return {
            "task_id": task_id,
            "attempt": attempt,
            "blocked": blocked,
            "retry_at": retry_at,
        }

    def release_lease(
        self, task_id: str, claim_id: str, *, reason: str, at: str
    ) -> dict[str, Any]:
        """Clear an interrupted claim's lease and reopen the Task without consuming an attempt."""
        with self.workspace.lock:
            manifest = self.workspace.load()
            ledger = self._ledger(manifest)
            task = ledger["tasks"].get(task_id)
            if not task:
                raise ValidationError("unknown task")
            lease = task.get("lease") or {}
            if lease.get("claim_id") != claim_id:
                raise ValidationError("claim does not hold this task")
            if task["state"] not in {"pending", "in_progress"}:
                raise ValidationError("only an unfinished task can be released")
            task.pop("lease", None)
            task["state"] = "pending"
            task["automation_attempts"] = max(0, int(task.get("automation_attempts", 1)) - 1)
            task.setdefault("releases", []).append(
                {"claim_id": claim_id, "reason": reason, "at": at}
            )
            ledger["events"].append(
                {"event": "task.released", "task_id": task_id, "reason": reason, "at": at}
            )
            self.workspace.save(manifest)
        return {"task_id": task_id, "state": "pending", "released": True, "reason": reason}

    def _require_submitter(
        self,
        manifest: dict[str, Any],
        task: dict[str, Any] | None,
        role: str,
        actor: Actor,
        claim_token: str | None,
    ) -> None:
        if not task or task.get("role") != role:
            raise ValidationError("task does not belong to this specialist role")
        self._require_claim(manifest, task, actor, claim_token)
        if task.get("state") == "accepted":
            return
        if task.get("state") != "in_progress":
            raise ValidationError("call the role's next command before submitting")
        if task.get("actor_profile") != actor.profile:
            raise ValidationError("task is active under a different profile")

    def _require_claim(
        self,
        manifest: dict[str, Any],
        task: dict[str, Any],
        actor: Actor,
        claim_token: str | None,
    ) -> None:
        if not manifest.get("automation"):
            return
        lease = task.get("lease")
        if not lease or lease.get("state") != "active":
            raise ValidationError("managed task requires an active claim")
        if not claim_token or digest(claim_token) != lease.get("token_digest"):
            raise ValidationError("claim token is missing, invalid, or replaced")
        if lease.get("actor_profile") != actor.profile:
            raise ValidationError("claim belongs to a different profile")
        if lease.get("actor_session_id") != actor.session_id:
            raise ValidationError("claim belongs to a different Hermes session")
        if datetime.now(UTC) >= datetime.fromisoformat(lease["expires_at"]):
            raise ValidationError("claim lease expired; late result rejected")

    def _replay(self, task: dict[str, Any], proposal_digest: str) -> dict[str, Any] | None:
        if task.get("state") != "accepted":
            return None
        if task.get("submission_digest") != proposal_digest:
            raise ValidationError("task is already accepted with a different submission")
        return self._receipt_view(task)

    def _receipt_view(self, task: dict[str, Any]) -> dict[str, Any]:
        result = {
            "run_id": self.run_id,
            "task_id": task["task_id"],
            "role": task["role"],
            "kind": task["kind"],
            "state": task["state"],
            "result_digest": task.get("result_digest"),
            "accepted_at": task.get("accepted_at"),
        }
        if task.get("continuation_task_id"):
            continuation = self._ledger(self.workspace.load())["tasks"].get(
                task["continuation_task_id"], {}
            )
            result["continuation"] = {
                "task_id": task["continuation_task_id"],
                "role": continuation.get("role"),
                "state": "pending",
            }
        return result

    def _audit_task(self, task: dict[str, Any]) -> dict[str, Any]:
        packet = self._validate_packet(task)
        group = {
            **packet["audit_group"],
            "allowed_document_ids": task.get("allowed_source_ids", []),
        }
        return {**task, "audit_group": group}

    def _materialize_empty_stages(self, manifest: dict[str, Any]) -> bool:
        """Record deterministic empty stages when selection leaves no evidence work."""
        datasets = manifest["datasets"]
        if "records" not in datasets or "screening" not in datasets:
            return False
        if self.workspace.stale("screening", manifest):
            return False
        records = set(self.workspace.index("records"))
        screening = self.workspace.index("screening")
        if set(screening) != records:
            return False
        changed = False
        included = {
            record_id
            for record_id, row in screening.items()
            if row["decision"] == "include"
        }
        if not included and "coverage" not in datasets:
            self.workspace.put("coverage", {"schema_version": "2", "records": []})
            changed = True
            datasets = self.workspace.load()["datasets"]
        if "coverage" not in datasets:
            return changed
        if self.workspace.stale("coverage", self.workspace.load()):
            return changed
        coverage = self.workspace.index("coverage")
        if set(coverage) != included:
            return changed
        selected = {
            record_id
            for record_id, row in coverage.items()
            if row["selection"] == "selected"
        }
        if selected:
            return changed
        stages = ["studies", "extractions", "appraisals"]
        if self.workspace.outcome_contract:
            stages.append("dispositions")
        for stage in stages:
            if stage not in self.workspace.load()["datasets"]:
                self.workspace.put(stage, {"schema_version": "2", "records": []})
                changed = True
        return changed

    def _validate_packet(self, task: dict[str, Any]) -> dict[str, Any]:
        self._validate_task_file(task, "packet_file", "packet.json")
        packet = self.workspace.store.read_json(task["packet_file"])
        if digest(packet) != task["packet_digest"]:
            raise ValidationError("task packet was modified")
        return packet

    def _validate_accepted_results(self, ledger: dict[str, Any]) -> None:
        for task_id in ledger["order"]:
            task = ledger["tasks"][task_id]
            if task["state"] != "accepted":
                continue
            self._validate_task_file(
                task, "result_file", f"result-{task.get('result_digest')}.json"
            )
            result = self.workspace.store.read_json(task.get("result_file", ""), default=None)
            if not isinstance(result, dict) or digest(result) != task.get("result_digest"):
                raise ValidationError(f"accepted task result was modified: {task_id}")

    def _validate_task_file(
        self,
        task: dict[str, Any],
        field: str,
        filename: str,
    ) -> None:
        value = task.get(field)
        expected_parent = f"tasks/{task.get('task_id')}"
        if not isinstance(value, str):
            raise ValidationError(f"task {field} is invalid")
        relative = Path(value)
        if (
            relative.is_absolute()
            or relative.parent.as_posix() != expected_parent
            or relative.name != filename
        ):
            raise ValidationError(f"task {field} escapes its immutable task directory")

    def _source_value(self, source_id: str) -> dict[str, Any]:
        documents = self.workspace.source_index()
        if source_id in documents:
            return documents[source_id]
        prefix, separator, identifier = source_id.partition(":")
        if not separator:
            raise ValidationError("unknown source identifier")
        if prefix == "extraction":
            return self.workspace.index("extractions")[identifier]
        if prefix == "appraisal":
            return self.workspace.index("appraisals")[identifier]
        if prefix == "study":
            return self.workspace.index("studies")[identifier]
        if prefix == "disposition":
            return self.workspace.index("dispositions")[identifier]
        raise ValidationError("unknown source identifier")

    def _supersede_stale(self, manifest: dict[str, Any], ledger: dict[str, Any]) -> None:
        current = current_digests(self.workspace)
        for task in ledger["tasks"].values():
            if task["state"] in {"pending", "in_progress"} and task["base_digests"] != current:
                task["state"] = "superseded"
                task["superseded_at"] = now()

    def _derive_state(self, manifest: dict[str, Any], ledger: dict[str, Any]) -> str:
        if (
            ledger.get("state") == "finalized"
            and (self.workspace.path / "completion.json").is_file()
        ):
            return "finalized"
        if ledger.get("halt"):
            return "blocked"
        if ledger.get("revision"):
            return "revision_required"
        if any(task["state"] == "blocked" for task in ledger["tasks"].values()):
            return "blocked"
        active = next(
            (
                task
                for task in ledger["tasks"].values()
                if task["state"] in {"pending", "in_progress"}
            ),
            None,
        )
        if active:
            return _state_for_role(active["role"])
        reviews = manifest["datasets"].get("reviews")
        if reviews and not self.workspace.stale("reviews", manifest):
            payload = self.workspace.read("reviews")
            if all(
                row.get("status") == "pass"
                for row in [*payload["records"], *payload["report_reviews"]]
            ):
                return "ready"
        if "records" not in manifest["datasets"]:
            return "created"
        return "awaiting_route"


def source_commands(base: str, run_id: str, task_id: str) -> dict[str, str]:
    """Exact bounded source-access commands; placeholders are uppercase words."""
    return {
        "source_list": f"{base} source list {run_id} {task_id}",
        "source_show": f"{base} source show {run_id} {task_id} SOURCE_ID --page N",
        "source_find": f"{base} source find {run_id} {task_id} SEARCH WORDS",
        "source_read": f"{base} source read {run_id} {task_id} DOCUMENT_ID LOCATOR",
    }


def _assessment_method(study: dict[str, Any]) -> str:
    if study["kind"] == "systematic-review":
        return "robis"
    if study["kind"] == "primary":
        return "rob2"
    return "descriptive"


def _synthesis_row(row: dict[str, Any], limit: int) -> dict[str, Any]:
    """Summarize one extraction for a synthesis packet; full rows stay readable as sources."""
    value = {
        key: row.get(key)
        for key in (
            "extraction_id",
            "record_id",
            "protocol_outcome",
            "population",
            "comparison",
            "outcome",
            "timepoint",
            "effect",
        )
    }
    if limit:
        value["result"] = str(row.get("result", ""))[:limit]
        location = dict(row.get("source_location") or {})
        location["quote"] = str(location.get("quote", ""))[:limit]
        value["source_location"] = location
    else:
        value["result_truncated"] = True
    return value


def _state_for_role(role: str) -> str:
    return {
        "searcher": "searching",
        "selector": "selecting",
        "extractor": "extracting",
        "synthesizer": "synthesizing",
        "auditor": "auditing",
        "coordinator": "created",
    }[role]


def _route_view(run_id: str, task: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "task_id": task["task_id"],
        "role": task["role"],
        "profile": ROLE_PROFILES[task["role"]],
        "state": task["state"],
    }


def utf8_pages(value: str, limit: int) -> list[str]:
    """Split text into independently valid UTF-8 pages bounded by bytes."""
    if not value:
        return [""]
    pages: list[str] = []
    current: list[str] = []
    size = 0
    for character in value:
        encoded_size = len(character.encode("utf-8"))
        if current and size + encoded_size > limit:
            pages.append("".join(current))
            current = []
            size = 0
        current.append(character)
        size += encoded_size
    if current:
        pages.append("".join(current))
    return pages
