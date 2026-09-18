"""Durable Review, Cycle, lease, retry, and notification automation.

Hermes cron is deliberately an edge adapter.  This module owns the durable
queue and all state transitions so missed monitor invocations, restarted Bot
sessions, and late submissions cannot corrupt a Run.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from filelock import FileLock

from hermes_medical_research.search.artifacts import canonical_json
from hermes_medical_research.search.models import ValidationError
from hermes_medical_research.search.ranking import deduplicate

from .tasks import (
    COMMAND_ROLES,
    ROLE_PROFILES,
    Actor,
    RunCatalog,
    TaskEngine,
    hmr_command,
    source_commands,
)
from .workspace import digest

AUTOMATION_SCHEMA_VERSION = "1"
REVIEW_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
LEASE_MINUTES = 60
RETRY_MINUTES = (5, 30)
MAX_ATTEMPTS = 3
ROLES = tuple(COMMAND_ROLES.values())


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _at(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"invalid automation artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"automation artifact must be an object: {path}")
    return value


def _slug(value: str) -> str:
    if not isinstance(value, str) or not REVIEW_SLUG.fullmatch(value):
        raise ValidationError(
            "review name must be 1-64 lowercase letters, digits, or interior hyphens"
        )
    return value


def _timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValidationError(f"unknown IANA timezone: {value}") from exc
    return value


def _schedule(value: str) -> str:
    value = value.strip()
    if value == "once":
        return value
    fields = value.split()
    if len(fields) != 5:
        raise ValidationError("schedule must be 'once' or a five-field cron expression")
    if any(any(ch not in "0123456789*,-/" for ch in field) for field in fields):
        raise ValidationError("schedule contains unsupported cron characters")
    return value


def _record_identity(record: dict[str, Any]) -> str:
    rows = [record, *(record.get("source_records") or [])]
    for field in ("doi", "pmid", "pmcid"):
        values = []
        for row in rows:
            identifiers = row.get("identifiers") if isinstance(row, dict) else None
            value = row.get(field) if isinstance(row, dict) else None
            if not value and isinstance(identifiers, dict):
                value = identifiers.get(field)
            if value:
                normalized = str(value).strip().casefold()
                if field == "doi":
                    normalized = normalized.removeprefix("https://doi.org/").removeprefix("doi:")
                values.append(normalized)
        if values:
            return f"{field}:{sorted(values)[0]}"
    sources = sorted(
        (str(row.get("source", "")), str(row.get("source_id", "")))
        for row in rows
        if isinstance(row, dict) and row.get("source") and row.get("source_id")
    )
    if sources:
        return "source:" + canonical_json(sources)
    canonical = record.get("canonical_id")
    if canonical:
        return "canonical:" + str(canonical)
    raise ValidationError("record has no canonical DOI, PMID, PMCID, or source identity")


def _source_digest(record: dict[str, Any]) -> str:
    # Retrieval position and citation counts change between refreshes without changing what a
    # screener judges, so they must not invalidate an otherwise identical decision.
    volatile = {
        "record_id",
        "canonical_id",
        "native_merge_order",
        "retrieved_at",
        "rank",
        "source_rank",
        "citation_count",
        "score",
        "scores",
    }

    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items() if key not in volatile}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    return digest(clean(record))


class AutomationEngine:
    """Deep module behind the small ``hmr review`` and ``hmr work`` interfaces."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ):
        self.catalog = RunCatalog(root)
        self.root = self.catalog.root
        self.reviews = self.root / "reviews"
        self.claims = self.root / "claims"
        self.outbox = self.root / "outbox"
        self.clock = clock

    @property
    def lock(self) -> FileLock:
        return FileLock(str(self.root / ".automation.lock"), timeout=300)

    @staticmethod
    def _review_lock(path: Path) -> FileLock:
        return FileLock(str(path / ".review.lock"), timeout=300)

    def create_review(
        self,
        name: str,
        request: dict[str, Any],
        *,
        schedule: str,
        timezone: str,
        mode: str = "report",
        records: int | str | None = None,
        fulltexts: int | str | None = None,
        language: str = "en",
    ) -> dict[str, Any]:
        name = _slug(name)
        cadence = _schedule(schedule)
        zone = _timezone(timezone)
        path = self.reviews / name
        with self.lock:
            if path.exists():
                raise ValidationError(f"review already exists: {name}")
            created = self.catalog.create(
                request,
                mode=mode,
                records=records,
                fulltexts=fulltexts,
                language=language,
            )
            path.mkdir(parents=True)
            _write_json(path / "request.json", request)
            cycle = self._cycle(created["run_id"], 1, reason="initial", predecessor=None)
            review = {
                "schema_version": AUTOMATION_SCHEMA_VERSION,
                "name": name,
                "state": "active",
                "created_at": _at(self.clock()),
                "updated_at": _at(self.clock()),
                "protocol_digest": created["protocol_digest"],
                "request_digest": digest(request),
                "request_file": "request.json",
                "run_options": {
                    "mode": mode,
                    "records": records,
                    "fulltexts": fulltexts,
                    "language": language,
                },
                "schedule": {"expression": cadence, "timezone": zone, "job_id": None},
                "cycles": [cycle],
                "catch_up_pending": False,
                "event_count": 0,
                "event_head": None,
            }
            self._link_run(created["run_id"], name, cycle["cycle_id"], 1)
            self._event(path, review, "review.created", {"run_id": created["run_id"]})
            self._save_review(path, review)
        return self._review_view(review)

    def adopt_review(
        self,
        name: str,
        run_id: str,
        *,
        schedule: str,
        timezone: str,
    ) -> dict[str, Any]:
        name = _slug(name)
        cadence = _schedule(schedule)
        zone = _timezone(timezone)
        workspace = self.catalog.workspace(run_id)
        manifest = workspace.load()
        path = self.reviews / name
        with self.lock:
            if path.exists():
                raise ValidationError(f"review already exists: {name}")
            path.mkdir(parents=True)
            request = deepcopy(manifest["protocol"]["question"])
            # An adopted Run is authoritative.  The normalized protocol is retained for
            # status and provenance; recurring refresh requires a fork with a request file.
            _write_json(path / "request.json", request)
            cycle = self._cycle(run_id, 1, reason="adopted", predecessor=None)
            review = {
                "schema_version": AUTOMATION_SCHEMA_VERSION,
                "name": name,
                "state": "active",
                "created_at": _at(self.clock()),
                "updated_at": _at(self.clock()),
                "protocol_digest": manifest["protocol_digest"],
                "request_digest": digest(request),
                "request_file": "request.json",
                "run_options": None,
                "schedule": {"expression": cadence, "timezone": zone, "job_id": None},
                "cycles": [cycle],
                "catch_up_pending": False,
                "event_count": 0,
                "event_head": None,
            }
            self._link_run(run_id, name, cycle["cycle_id"], 1)
            self._event(path, review, "review.adopted", {"run_id": run_id})
            self._save_review(path, review)
        return self._review_view(review)

    def fork_review(
        self,
        old_name: str,
        new_name: str,
        request: dict[str, Any],
        *,
        schedule: str,
        timezone: str,
        mode: str = "report",
        records: int | str | None = None,
        fulltexts: int | str | None = None,
        language: str = "en",
    ) -> dict[str, Any]:
        old = self._load_review(_slug(old_name))
        created = self.create_review(
            new_name,
            request,
            schedule=schedule,
            timezone=timezone,
            mode=mode,
            records=records,
            fulltexts=fulltexts,
            language=language,
        )
        path, review = self._load_review(_slug(new_name), with_path=True)
        with self._review_lock(path):
            review = self._load_review(new_name)
            review["forked_from"] = {
                "review": old_name,
                "protocol_digest": old["protocol_digest"],
                "at": _at(self.clock()),
            }
            self._event(path, review, "review.forked", review["forked_from"])
            self._save_review(path, review)
        return {**created, "forked_from": old_name}

    def list_reviews(self) -> dict[str, Any]:
        values = []
        if self.reviews.is_dir():
            for path in sorted(self.reviews.iterdir()):
                if (path / "review.json").is_file():
                    values.append(self._review_view(self._load_review(path.name)))
        return {"reviews": values}

    def review_status(self, name: str) -> dict[str, Any]:
        review = self._load_review(_slug(name))
        result = self._review_view(review)
        result["cycles"] = deepcopy(review["cycles"])
        result["catch_up_pending"] = review["catch_up_pending"]
        result["event_head"] = review["event_head"]
        return result

    def set_paused(self, name: str, paused: bool) -> dict[str, Any]:
        """Pause stops new claims and abandonment blocking; in-flight leases may finish."""
        path, review = self._load_review(_slug(name), with_path=True)
        with self._review_lock(path):
            review = self._load_review(name)
            review["state"] = "paused" if paused else "active"
            if not paused:
                # Waiting time while paused must not count toward worker abandonment.
                review["resumed_at"] = _at(self.clock())
            self._event(path, review, "review.paused" if paused else "review.resumed", {})
            self._save_review(path, review)
        return self._review_view(review)

    def retry_review(self, name: str, actor: Actor, *, reason: str) -> dict[str, Any]:
        """Reopen the latest blocked Cycle in place, keeping every accepted result."""
        actor.require("coordinator")
        message = str(reason).strip()
        if not message:
            raise ValidationError("retry reason must be nonempty")
        name = _slug(name)
        with self.lock:
            path, review = self._load_review(name, with_path=True)
            with self._review_lock(path):
                review = self._load_review(name)
                if self._active_cycle(review):
                    raise ValidationError("Review already has an active Cycle")
                latest = review["cycles"][-1]
                if latest["status"] != "blocked":
                    raise ValidationError("only a blocked latest Cycle can be retried")
                at = _at(self.clock())
                engine = TaskEngine(self.catalog.workspace(latest["run_id"]), clock=self.clock)
                result = engine.retry_blocked(reason=message, at=at)
                latest.setdefault("retries", []).append(
                    {
                        "at": at,
                        "reason": message,
                        "previous_finished_at": latest.get("finished_at"),
                        "task_ids": result["retried_task_ids"],
                    }
                )
                latest["status"] = "active"
                latest.pop("finished_at", None)
                latest.pop("result_digest", None)
                review["resumed_at"] = at
                self._event(
                    path,
                    review,
                    "cycle.retried",
                    {"cycle_id": latest["cycle_id"], "reason": message, **result},
                )
                self._save_review(path, review)
        return {
            "name": name,
            "state": review["state"],
            "cycle": latest["number"],
            "cycle_state": "active",
            **result,
        }

    def cancel_review(self, name: str, actor: Actor, *, reason: str) -> dict[str, Any]:
        """Cancel one active Cycle without deleting its immutable artifacts."""
        actor.require("coordinator")
        message = str(reason).strip()
        if not message:
            raise ValidationError("cancellation reason must be nonempty")
        name = _slug(name)
        with self.lock:
            path, review = self._load_review(name, with_path=True)
            with self._review_lock(path):
                review = self._load_review(name)
                active = self._active_cycle(review)
                if active is None:
                    raise ValidationError("Review has no active Cycle to cancel")
                cancelled_at = _at(self.clock())
                engine = TaskEngine(self.catalog.workspace(active["run_id"]), clock=self.clock)
                result = engine.cancel_open_tasks(reason=message, at=cancelled_at)
                if self.claims.is_dir():
                    for claim_path in self.claims.glob("claim-*.json"):
                        claim = _read_json(claim_path)
                        if (
                            claim.get("run_id") == active["run_id"]
                            and claim.get("state") == "active"
                        ):
                            claim.update(state="cancelled", cancelled_at=cancelled_at)
                            _write_json(claim_path, claim)
                review["state"] = "paused"
                review["catch_up_pending"] = False
                self._event(
                    path,
                    review,
                    "review.cancelled",
                    {"cycle_id": active["cycle_id"], "reason": message},
                )
                self._finish_cycle(path, review, active, "blocked", result)
                self._save_review(path, review)
        return {
            "name": name,
            "state": "paused",
            "cycle": active["number"],
            "cycle_state": "blocked",
            "code": "operator_cancelled",
            "reason": message,
            "cancelled_tasks": result["task_ids"],
        }

    def trigger(self, name: str, *, scheduled: bool = False) -> dict[str, Any]:
        """Enqueue one Cycle, coalescing fires while another Cycle is active."""
        name = _slug(name)
        path, review = self._load_review(name, with_path=True)
        with self._review_lock(path):
            review = self._load_review(name)
            if scheduled and review["state"] == "paused":
                return {"name": name, "state": "paused", "enqueued": False}
            active = self._active_cycle(review)
            if active:
                review["catch_up_pending"] = True
                self._event(path, review, "cycle.coalesced", {"active": active["cycle_id"]})
                self._save_review(path, review)
                return {
                    "name": name,
                    "state": "coalesced",
                    "cycle": active["number"],
                    "enqueued": False,
                }
            cycle = self._new_cycle(path, review, reason="scheduled" if scheduled else "manual")
            self._save_review(path, review)
            return {"name": name, "state": "enqueued", "cycle": cycle["number"], "enqueued": True}

    async def tick(self) -> dict[str, Any]:
        """Advance routing, leases, finalization, and coalesced Cycles without inference."""
        routed = finalized = expired = blocked = cycles = 0
        with self.lock:
            names = [
                path.name
                for path in sorted(self.reviews.glob("*"))
                if (path / "review.json").is_file()
            ]
        for name in names:
            path, review = self._load_review(name, with_path=True)
            with self._review_lock(path):
                review = self._load_review(name)
                active = self._active_cycle(review)
                if not active:
                    continue
                engine = TaskEngine(self.catalog.workspace(active["run_id"]), clock=self.clock)
                expired += self._expire_leases(engine, path, review)
                status = engine.status()
                if status["state"] == "ready":
                    result = await engine.finalize(
                        Actor("hmr-coordinator", "automation-tick", "coordinator"),
                        offline=False,
                    )
                    if result.get("completed"):
                        finalized += 1
                        self._finish_cycle(path, review, active, "completed", result)
                        if review.get("catch_up_pending") and review["state"] == "active":
                            review["catch_up_pending"] = False
                            self._new_cycle(path, review, reason="coalesced")
                            cycles += 1
                        self._save_review(path, review)
                    continue
                if status["state"] in {"finalized", "blocked"}:
                    if status["state"] == "blocked":
                        blocked += 1
                        self._finish_cycle(path, review, active, "blocked", status)
                        self._save_review(path, review)
                    continue
                if not status["active"]:
                    refresh = self._reconcile_refresh(engine, path, review, active)
                    if refresh == "no_change":
                        if review.get("catch_up_pending") and review["state"] == "active":
                            review["catch_up_pending"] = False
                            self._new_cycle(path, review, reason="coalesced")
                            cycles += 1
                        self._save_review(path, review)
                        continue
                    routed_result = engine.route_next(
                        Actor("hmr-coordinator", "automation-tick", "coordinator")
                    )
                    if routed_result.get("task_id"):
                        routed += 1
                self._save_review(path, review)
        result = {
            "state": "ticked",
            "routed": routed,
            "finalized": finalized,
            "expired_leases": expired,
            "blocked": blocked,
            "cycles_started": cycles,
        }
        return result

    def claim(
        self, command: str, actor: Actor, *, review: str | None = None
    ) -> dict[str, Any]:
        role = COMMAND_ROLES[command]
        actor.require(role)
        with self.lock:
            eligible = self._eligible(role, mutate=True, review=review)
            if not eligible:
                return {"role": role, "state": "idle"}
            selected = eligible[0]
            engine = TaskEngine(self.catalog.workspace(selected["run_id"]), clock=self.clock)
            claim_id = "claim-" + uuid4().hex
            token = secrets.token_urlsafe(32)
            expires = self.clock() + timedelta(minutes=LEASE_MINUTES)
            lease = engine.lease_task(
                selected["task_id"],
                actor,
                claim_id=claim_id,
                token=token,
                expires_at=_at(expires),
                at=_at(self.clock()),
            )
            opened = engine.role_next(command, selected["task_id"], actor, claim_token=token)
            claim = {
                "schema_version": AUTOMATION_SCHEMA_VERSION,
                "claim_id": claim_id,
                "review": selected["review"],
                "cycle_id": selected["cycle_id"],
                "run_id": selected["run_id"],
                "task_id": selected["task_id"],
                "role": role,
                "actor_profile": actor.profile,
                "actor_session_id": actor.session_id,
                "token_digest": digest(token),
                "attempt": lease["attempt"],
                "state": "active",
                "claimed_at": _at(self.clock()),
                "expires_at": _at(expires),
            }
            _write_json(self.claims / f"{claim_id}.json", claim)
        executable = hmr_command()
        base = (
            f"{executable} --store {shlex.quote(str(self.root))} "
            f"--actor {ROLE_PROFILES[role]} --session-id {actor.session_id} "
            f"--claim-token {token}"
        )
        result = {
            "claim_id": claim_id,
            "claim_token": token,
            "expires_at": _at(expires),
            "attempt": lease["attempt"],
            **opened,
            **source_commands(base, selected["run_id"], selected["task_id"]),
            "submit": (
                f"{base} {command} submit {selected['run_id']} {selected['task_id']} "
                f"--from {opened['proposal_path']}"
            ),
            "fail": (
                f"{executable} --store {shlex.quote(str(self.root))} "
                f"--actor {ROLE_PROFILES[role]} --session-id {actor.session_id} "
                f"work fail {claim_id} "
                "--code CODE --message MESSAGE"
            ),
        }
        if command == "search":
            result["run"] = (
                f"{base} search run {selected['run_id']} {selected['task_id']}"
            )
            proposal = (
                ""
                if opened.get("resume")
                else f" --from {shlex.quote(opened['proposal_path'])}"
            )
            result["execute"] = (
                f"{base} search execute {selected['run_id']} {selected['task_id']}{proposal}"
            )
            if opened.get("search_mode") == "review":
                result["approve"] = (
                    f"{base} search approve {selected['run_id']} {selected['task_id']} "
                    "--strategy-digest DIGEST"
                )
        return result

    def renew(self, claim_id: str, actor: Actor) -> dict[str, Any]:
        """Extend the session-bound lease of a claim whose host session is still running."""
        path = self.claims / f"{claim_id}.json"
        with self.lock:
            claim = _read_json(path)
            if claim.get("claim_id") != claim_id or claim.get("state") != "active":
                raise ValidationError("claim is not active")
            if (
                claim.get("actor_profile") != actor.profile
                or claim.get("actor_session_id") != actor.session_id
            ):
                raise ValidationError("claim belongs to a different Hermes session")
            expires = _at(self.clock() + timedelta(minutes=LEASE_MINUTES))
            engine = TaskEngine(self.catalog.workspace(claim["run_id"]), clock=self.clock)
            result = engine.renew_lease(
                claim["task_id"], claim_id, expires_at=expires, at=_at(self.clock())
            )
            claim["expires_at"] = expires
            _write_json(path, claim)
        return result

    def next_available_at(self, role: str, review: str | None = None) -> str | None:
        """When this role's earliest backed-off Task becomes claimable, or None if there is none.

        A user-invoked step needs this because a failed Task is unavailable for five minutes and the
        router mints one Task at a time, so an idle claim does not mean the stage is finished. Cron
        never needed it: it simply ran again a minute later.
        """
        current = self.clock()
        waiting: list[str] = []
        if not self.reviews.is_dir():
            return None
        for review_dir in sorted(self.reviews.iterdir()):
            if not (review_dir / "review.json").is_file():
                continue
            if review is not None and review_dir.name != review:
                continue
            entry = self._load_review(review_dir.name)
            active = self._active_cycle(entry)
            if not active or active["status"] != "active" or entry["state"] == "paused":
                continue
            engine = TaskEngine(self.catalog.workspace(active["run_id"]), clock=self.clock)
            for task in engine.status()["active"]:
                if task["role"] != role:
                    continue
                detail = engine.task_automation(task["task_id"])
                if detail["state"] != "pending" or detail.get("lease"):
                    continue
                available = detail.get("available_at")
                if available and _parse(available) > current:
                    waiting.append(available)
        return min(waiting) if waiting else None

    def release(self, claim_id: str, actor: Actor, *, reason: str) -> dict[str, Any]:
        """Return an interrupted claim's Task to pending without spending one of its attempts.

        A killed step runner would otherwise strand the lease for the rest of its hour, and the
        stage would look idle because a leased Task is not claimable. This is not a semantic
        failure, so it must not count against the three bounded attempts. The caller must hold the
        step lock, which proves no live runner owns the claim.
        """
        path = self.claims / f"{claim_id}.json"
        with self.lock:
            claim = _read_json(path)
            if claim.get("claim_id") != claim_id or claim.get("state") != "active":
                raise ValidationError("claim is not active")
            if (
                claim.get("actor_profile") != actor.profile
                or claim.get("actor_session_id") != actor.session_id
            ):
                raise ValidationError("claim belongs to a different Hermes session")
            engine = TaskEngine(self.catalog.workspace(claim["run_id"]), clock=self.clock)
            result = engine.release_lease(
                claim["task_id"], claim_id, reason=reason, at=_at(self.clock())
            )
            claim.update(state="released", released_at=_at(self.clock()), reason=reason)
            _write_json(path, claim)
            review_path, review = self._load_review(claim["review"], with_path=True)
            with self._review_lock(review_path):
                review = self._load_review(claim["review"])
                self._event(
                    review_path,
                    review,
                    "task.released",
                    {"task_id": claim["task_id"], "claim_id": claim_id, "reason": reason},
                )
                self._save_review(review_path, review)
        return result

    def fail(self, claim_id: str, actor: Actor, *, code: str, message: str) -> dict[str, Any]:
        path = self.claims / f"{claim_id}.json"
        with self.lock:
            claim = _read_json(path)
            if claim.get("claim_id") != claim_id or claim.get("state") != "active":
                raise ValidationError("claim is not active")
            if claim.get("actor_profile") != actor.profile:
                raise ValidationError("claim belongs to a different profile")
            if claim.get("actor_session_id") != actor.session_id:
                raise ValidationError("claim belongs to a different Hermes session")
            review_path, review = self._load_review(claim["review"], with_path=True)
            with self._review_lock(review_path):
                review = self._load_review(claim["review"])
                engine = TaskEngine(self.catalog.workspace(claim["run_id"]), clock=self.clock)
                result = engine.fail_lease(
                    claim["task_id"],
                    claim_id,
                    code=code,
                    message=message,
                    at=_at(self.clock()),
                )
                claim.update(
                    state="failed", failed_at=_at(self.clock()), code=code, message=message
                )
                _write_json(path, claim)
                self._event(
                    review_path,
                    review,
                    "task.failed" if not result["blocked"] else "task.blocked",
                    {"claim_id": claim_id, "task_id": claim["task_id"], "code": code},
                )
                if result["blocked"]:
                    active = self._active_cycle(review)
                    if active:
                        self._finish_cycle(review_path, review, active, "blocked", result)
                self._save_review(review_path, review)
        return {"claim_id": claim_id, **result}

    def acknowledge(self, event_id: str) -> dict[str, Any]:
        path = self.outbox / f"{event_id}.json"
        with self.lock:
            event = _read_json(path)
            if event.get("event_id") != event_id:
                raise ValidationError("notification event identifier mismatch")
            event["state"] = "acknowledged"
            event["acknowledged_at"] = _at(self.clock())
            _write_json(path, event)
        return {"event_id": event_id, "state": "acknowledged"}

    def notification_probe(self) -> dict[str, Any]:
        pending = []
        if self.outbox.is_dir():
            for path in sorted(self.outbox.glob("event-*.json")):
                event = _read_json(path)
                if event.get("state") == "pending":
                    pending.append(event["event_id"])
        return {
            "state": "ready" if pending else "idle",
            "generation": digest(pending),
            "count": len(pending),
        }

    def _eligible(
        self, role: str, *, mutate: bool, review: str | None = None
    ) -> list[dict[str, Any]]:
        """Claimable Tasks for one role, newest last.

        ``review`` restricts the search to one Review, which is what a user-invoked step needs:
        ``/hmr-selector`` must never accept work into a Review the operator is not driving.
        """
        current = self.clock()
        values: list[dict[str, Any]] = []
        if not self.reviews.is_dir():
            return values
        for review_dir in sorted(self.reviews.iterdir()):
            if not (review_dir / "review.json").is_file():
                continue
            if review is not None and review_dir.name != review:
                continue
            entry = self._load_review(review_dir.name)
            active = self._active_cycle(entry)
            if not active or active["status"] != "active" or entry["state"] == "paused":
                continue
            engine = TaskEngine(self.catalog.workspace(active["run_id"]), clock=self.clock)
            status = engine.status()
            for task in status["active"]:
                if task["role"] != role:
                    continue
                detail = engine.task_automation(task["task_id"])
                if detail["state"] == "in_progress" and detail.get("lease"):
                    continue
                available_at = _parse(detail.get("available_at") or detail["created_at"])
                if current < available_at:
                    continue
                age = max(0, int((current - available_at).total_seconds()))
                wake = 0 if age < 300 else 1 if age < 2100 else 2
                values.append(
                    {
                        "review": entry["name"],
                        "cycle_id": active["cycle_id"],
                        "run_id": active["run_id"],
                        "task_id": task["task_id"],
                        "created_at": detail["created_at"],
                        "wake": wake,
                    }
                )
        return sorted(values, key=lambda item: (item["created_at"], item["run_id"]))

    def _expire_leases(
        self, engine: TaskEngine, review_path: Path, review: dict[str, Any]
    ) -> int:
        expired = 0
        for task in engine.status()["active"]:
            detail = engine.task_automation(task["task_id"])
            lease = detail.get("lease")
            if not lease or self.clock() < _parse(lease["expires_at"]):
                continue
            result = engine.fail_lease(
                task["task_id"],
                lease["claim_id"],
                code="lease_expired",
                message="worker did not finish within the 60-minute lease",
                at=_at(self.clock()),
            )
            claim_path = self.claims / f"{lease['claim_id']}.json"
            if claim_path.is_file():
                claim = _read_json(claim_path)
                claim.update(state="expired", expired_at=_at(self.clock()))
                _write_json(claim_path, claim)
            self._event(
                review_path,
                review,
                "task.blocked" if result["blocked"] else "task.lease_expired",
                {"task_id": task["task_id"], "claim_id": lease["claim_id"]},
            )
            expired += 1
        return expired

    def _new_cycle(
        self, path: Path, review: dict[str, Any], *, reason: str
    ) -> dict[str, Any]:
        options = review.get("run_options")
        if not isinstance(options, dict):
            raise ValidationError("adopted review must be forked before recurring refresh")
        request = _read_json(path / review["request_file"])
        if digest(request) != review["request_digest"]:
            raise ValidationError(
                "review request was modified; fork the Review for protocol changes"
            )
        created = self.catalog.create(request, **options)
        if created["protocol_digest"] != review["protocol_digest"]:
            raise ValidationError("recurring Cycle changed the immutable Review protocol")
        previous = review["cycles"][-1]
        number = len(review["cycles"]) + 1
        cycle = self._cycle(
            created["run_id"], number, reason=reason, predecessor=previous["run_id"]
        )
        frozen = self._accepted_search_plan(previous["run_id"])
        self._link_run(
            created["run_id"],
            review["name"],
            cycle["cycle_id"],
            number,
            predecessor=previous["run_id"],
            frozen_search_plan=frozen,
        )
        review["cycles"].append(cycle)
        self._event(path, review, "cycle.started", {"run_id": created["run_id"], "reason": reason})
        return cycle

    def _reconcile_refresh(
        self,
        engine: TaskEngine,
        review_path: Path,
        review: dict[str, Any],
        cycle: dict[str, Any],
    ) -> str | None:
        """Merge a refresh corpus and reuse only digest-identical screening rows."""
        workspace = engine.workspace
        manifest = workspace.load()
        automation = manifest.get("automation") or {}
        predecessor = automation.get("predecessor_run_id")
        if not predecessor or automation.get("refresh_reconciled"):
            return None
        if "records" not in manifest["datasets"]:
            return None
        ledger = manifest["task_engine"]
        search_tasks = [
            ledger["tasks"][task_id]
            for task_id in ledger["order"]
            if ledger["tasks"][task_id]["kind"] == "search"
        ]
        if not search_tasks or search_tasks[-1]["state"] != "accepted":
            return None
        previous = self.catalog.workspace(predecessor)
        previous_manifest = previous.load()
        if "records" not in previous_manifest["datasets"]:
            automation["refresh_reconciled"] = True
            workspace.save(manifest)
            return "changed"

        previous_records = previous.rows("records", fresh=False)
        current_records = workspace.rows("records", fresh=False)
        previous_by_identity = {_record_identity(row): row for row in previous_records}
        current_by_identity = {_record_identity(row): row for row in current_records}
        unchanged = {
            identity
            for identity in previous_by_identity.keys() & current_by_identity.keys()
            if _source_digest(previous_by_identity[identity])
            == _source_digest(current_by_identity[identity])
        }
        changed = set(previous_by_identity) != set(current_by_identity) or any(
            identity not in unchanged
            for identity in previous_by_identity.keys() & current_by_identity.keys()
        )
        if not changed and (previous.path / "completion.json").is_file():
            checkpoint = {
                "schema_version": AUTOMATION_SCHEMA_VERSION,
                "state": "no_change",
                "predecessor_run_id": predecessor,
                "predecessor_completion_digest": digest(
                    _read_json(previous.path / "completion.json")
                ),
                "records_digest": previous_manifest["datasets"]["records"]["digest"],
                "search_result_digest": search_tasks[-1]["result_digest"],
                "created_at": _at(self.clock()),
            }
            workspace.store.write_json(
                f"refresh/checkpoint-{digest(checkpoint)}.json", checkpoint
            )
            automation["refresh_reconciled"] = True
            automation["no_change_checkpoint"] = digest(checkpoint)
            workspace.save(manifest)
            self._finish_cycle(review_path, review, cycle, "no_change", checkpoint)
            return "no_change"

        current_documents = workspace.rows("documents", fresh=False)
        previous_documents = previous.rows("documents", fresh=False)
        merged = deduplicate([*deepcopy(previous_records), *deepcopy(current_records)])
        for record in merged:
            identity = record["canonical_id"]
            if identity.startswith("record:"):
                identity = canonical_json(
                    sorted(
                        (str(row.get("source")), str(row.get("source_id")))
                        for row in record["source_records"]
                    )
                )
            record["record_id"] = "r-" + hashlib.sha256(identity.encode()).hexdigest()[:20]
        merged_by_identity = {_record_identity(row): row for row in merged}
        workspace.put("records", {"schema_version": "1", "records": merged}, validate=False)

        documents: dict[str, dict[str, Any]] = {}
        for record, rows in (
            (previous_by_identity, previous_documents),
            (current_by_identity, current_documents),
        ):
            old_identity = {row["record_id"]: identity for identity, row in record.items()}
            for document in rows:
                identity = old_identity.get(document["record_id"])
                target = merged_by_identity.get(identity or "")
                if target is None or document.get("kind") == "fulltext":
                    continue
                copied = deepcopy(document)
                old_id = copied["record_id"]
                copied["record_id"] = target["record_id"]
                if copied["document_id"].startswith(old_id + ":"):
                    copied["document_id"] = (
                        target["record_id"] + copied["document_id"][len(old_id) :]
                    )
                documents[copied["document_id"]] = copied
        workspace.put(
            "documents",
            {"schema_version": "1", "records": list(documents.values())},
            validate=False,
        )

        reused = []
        if "screening" in previous_manifest["datasets"]:
            previous_screening = previous.index("screening")
            prior_identities = {
                row["record_id"]: identity for identity, row in previous_by_identity.items()
            }
            for old_id, row in previous_screening.items():
                identity = prior_identities.get(old_id)
                target = merged_by_identity.get(identity or "")
                if identity not in unchanged or target is None or row.get("basis") == "fulltext":
                    continue
                copied = deepcopy(row)
                copied["record_id"] = target["record_id"]
                reused.append(copied)
        if reused:
            workspace.put("screening", {"schema_version": "2", "records": reused})
        origin_tasks = {}
        previous_ledger = previous_manifest["task_engine"]
        for task_id in previous_ledger["order"]:
            task = previous_ledger["tasks"][task_id]
            if task["kind"] == "screening" and task["state"] == "accepted":
                for record_id in task["target_ids"]:
                    origin_tasks[record_id] = {
                        "task_id": task_id,
                        "result_digest": task["result_digest"],
                    }
        reused_origins = []
        destination_identities = {
            record["record_id"]: identity for identity, record in merged_by_identity.items()
        }
        for row in reused:
            # Map the destination row back through the stable source identity.
            destination_identity = destination_identities[row["record_id"]]
            old_record = previous_by_identity[destination_identity]
            origin = origin_tasks.get(old_record["record_id"])
            if origin:
                reused_origins.append(
                    {
                        "record_id": row["record_id"],
                        "origin_task_id": origin["task_id"],
                        "origin_result_digest": origin["result_digest"],
                    }
                )
        receipt = {
            "schema_version": AUTOMATION_SCHEMA_VERSION,
            "origin_run_id": predecessor,
            "destination_run_id": engine.run_id,
            "stage": "screening",
            "reused_record_ids": sorted(row["record_id"] for row in reused),
            "origins": sorted(reused_origins, key=lambda item: item["record_id"]),
            "unchanged_source_digests": {
                identity: _source_digest(previous_by_identity[identity])
                for identity in sorted(unchanged)
            },
            "created_at": _at(self.clock()),
        }
        receipt_path = f"reuse/screening-{digest(receipt)}.json"
        workspace.store.write_json(receipt_path, receipt)
        manifest = workspace.load()
        manifest["automation"]["refresh_reconciled"] = True
        manifest["automation"]["reuse_receipts"] = [receipt_path]
        workspace.save(manifest)
        self._event(
            review_path,
            review,
            "cycle.refresh_reconciled",
            {"cycle_id": cycle["cycle_id"], "screening_reused": len(reused)},
        )
        return "changed"

    def _accepted_search_plan(self, run_id: str) -> dict[str, Any] | None:
        manifest = self.catalog.workspace(run_id).load()
        ledger = manifest["task_engine"]
        for task_id in ledger["order"]:
            task = ledger["tasks"][task_id]
            if task["kind"] == "search" and task.get("search_plan"):
                return deepcopy(task["search_plan"])
        return None

    def _finish_cycle(
        self,
        path: Path,
        review: dict[str, Any],
        cycle: dict[str, Any],
        state: str,
        result: dict[str, Any],
    ) -> None:
        if cycle["status"] != "active":
            return
        cycle["status"] = state
        cycle["finished_at"] = _at(self.clock())
        cycle["result_digest"] = digest(result)
        event_type = (
            "cycle.blocked"
            if state == "blocked"
            else "cycle.no_change"
            if state == "no_change"
            else "cycle.completed"
        )
        event = self._event(path, review, event_type, {"cycle_id": cycle["cycle_id"]})
        self._notify(review, cycle, event_type, event["event_id"])

    def _notify(
        self, review: dict[str, Any], cycle: dict[str, Any], kind: str, source_event: str
    ) -> None:
        event_id = "event-" + uuid4().hex
        value = {
            "schema_version": AUTOMATION_SCHEMA_VERSION,
            "event_id": event_id,
            "state": "pending",
            "kind": kind,
            "review": review["name"],
            "cycle": cycle["number"],
            "run_id": cycle["run_id"],
            "source_event": source_event,
            "created_at": _at(self.clock()),
        }
        _write_json(self.outbox / f"{event_id}.json", value)

    def _cycle(
        self, run_id: str, number: int, *, reason: str, predecessor: str | None
    ) -> dict[str, Any]:
        return {
            "cycle_id": "cycle-" + uuid4().hex,
            "number": number,
            "run_id": run_id,
            "status": "active",
            "reason": reason,
            "predecessor_run_id": predecessor,
            "created_at": _at(self.clock()),
        }

    def _link_run(
        self,
        run_id: str,
        review: str,
        cycle_id: str,
        number: int,
        *,
        predecessor: str | None = None,
        frozen_search_plan: dict[str, Any] | None = None,
    ) -> None:
        workspace = self.catalog.workspace(run_id)
        with workspace.lock:
            manifest = workspace.load()
            if manifest.get("automation"):
                raise ValidationError("Run is already managed by a Review")
            manifest["automation"] = {
                "schema_version": AUTOMATION_SCHEMA_VERSION,
                "review": review,
                "cycle_id": cycle_id,
                "cycle": number,
                "predecessor_run_id": predecessor,
                "frozen_search_plan": frozen_search_plan,
            }
            workspace.save(manifest)

    def _event(
        self, path: Path, review: dict[str, Any], event: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        sequence = int(review.get("event_count", 0)) + 1
        body = {
            "schema_version": AUTOMATION_SCHEMA_VERSION,
            "event_id": "review-event-" + uuid4().hex,
            "sequence": sequence,
            "review": review["name"],
            "event": event,
            "at": _at(self.clock()),
            "previous": review.get("event_head"),
            "payload": payload,
        }
        checksum = digest(body)
        _write_json(path / "events" / f"{sequence:08d}-{checksum}.json", body)
        review["event_count"] = sequence
        review["event_head"] = checksum
        return body

    def _save_review(self, path: Path, review: dict[str, Any]) -> None:
        review["updated_at"] = _at(self.clock())
        _write_json(path / "review.json", review)

    def _load_review(
        self, name: str, *, with_path: bool = False
    ) -> dict[str, Any] | tuple[Path, dict[str, Any]]:
        path = (self.reviews / _slug(name)).resolve()
        if path.parent != self.reviews.resolve():
            raise ValidationError("review path escapes the artifact store")
        review = _read_json(path / "review.json")
        if review.get("schema_version") != AUTOMATION_SCHEMA_VERSION or review.get("name") != name:
            raise ValidationError("unsupported or mismatched Review manifest")
        return (path, review) if with_path else review

    @staticmethod
    def _active_cycle(review: dict[str, Any]) -> dict[str, Any] | None:
        return next(
            (cycle for cycle in reversed(review["cycles"]) if cycle["status"] == "active"),
            None,
        )

    @staticmethod
    def _review_view(review: dict[str, Any]) -> dict[str, Any]:
        active = AutomationEngine._active_cycle(review)
        latest = review["cycles"][-1]
        return {
            "name": review["name"],
            "state": review["state"],
            "schedule": deepcopy(review["schedule"]),
            "cycles": len(review["cycles"]),
            "active_cycle": active["number"] if active else None,
            "latest_cycle_state": latest["status"],
            "protocol_digest": review["protocol_digest"],
        }
