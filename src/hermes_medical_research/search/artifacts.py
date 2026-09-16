from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from collections.abc import Iterable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hermes_medical_research import __version__

from .config import Credentials
from .models import Question, Strategy

ARTIFACT_SCHEMA_VERSION = "2"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def strategy_digest(strategy: Strategy) -> str:
    return hashlib.sha256(canonical_json(strategy.to_dict()).encode()).hexdigest()


def confirmation_token(strategy: Strategy, counts: dict[str, int]) -> str:
    payload = f"{strategy_digest(strategy)}:{canonical_json(counts)}"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def preflight_digest(value: dict[str, Any]) -> str:
    payload = {key: item for key, item in value.items() if key != "preflight_digest"}
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def run_directory(base: Path, question: Question) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return base / f"{timestamp}-{question_slug(question.question)}"


def question_slug(question: str) -> str:
    """Build a filesystem-safe, non-colliding directory suffix for a question.

    ASCII-only slugification collapses every question written in a non-Latin script to the same
    fallback, so Korean, Chinese, and Japanese runs would be distinguishable only by their
    second-resolution timestamp. Transliterable accents are folded to ASCII; anything left
    unrepresented falls back to a short digest of the question rather than a shared constant.
    """
    folded = unicodedata.normalize("NFKD", question).casefold()
    ascii_only = "".join(char for char in folded if not unicodedata.combining(char))
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only).strip("-")[:48].strip("-")
    if slug:
        return slug
    return "q-" + hashlib.sha256(question.strip().casefold().encode()).hexdigest()[:12]


class RunStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.sources_dir = path / "sources"

    def initialize(
        self, question: Question, strategy: Strategy, credentials: Credentials
    ) -> dict[str, Any]:
        existing = self.read_json("manifest.json", default=None)
        if existing:
            if existing.get("strategy_digest") != strategy_digest(strategy):
                raise ValueError("existing run directory belongs to a different strategy")
            return existing
        self.sources_dir.mkdir(parents=True, exist_ok=True)
        self.write_json("question.json", question.to_dict())
        self.write_json("strategy.json", strategy.to_dict())
        manifest = {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "tool_version": __version__,
            "created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
            "mode": strategy.mode,
            "status": ("awaiting_strategy_approval" if strategy.mode == "review" else "planned"),
            "strategy_digest": strategy_digest(strategy),
            "credentials": credentials.redacted(),
            "sources": {
                source: {
                    "status": "pending",
                    "cursor": None,
                    "retrieved": 0,
                    "retained": 0,
                    "filtered_out": 0,
                    "reported_total": None,
                    "error": None,
                    "truncated": False,
                }
                for source in strategy.strategies
            },
        }
        self.write_json("manifest.json", manifest)
        return manifest

    def write_manifest(self, manifest: dict[str, Any]) -> None:
        manifest["updated_at"] = datetime.now(UTC).isoformat()
        self.write_json("manifest.json", manifest)

    def write_json(self, relative: str, value: Any) -> None:
        target = self.path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        _atomic_write(target, text)

    def write_jsonl(self, relative: str, values: Iterable[dict[str, Any]]) -> None:
        target = self.path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        text = "".join(
            json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n" for value in values
        )
        _atomic_write(target, text)

    def append_source(self, source: str, values: Iterable[dict[str, Any]]) -> None:
        target = self.sources_dir / f"{source}.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            for value in values:
                handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def truncate_source(self, source: str, count: int) -> int:
        """Drop native records past the last persisted checkpoint.

        `append_source` writes before the manifest checkpoint is persisted, so a crash in that
        window leaves records on disk that the checkpoint does not account for. Resuming would
        refetch and re-append the same page, duplicating records and overshooting
        `limit_per_source`. Truncating to the checkpointed count makes resume idempotent.
        """
        target = self.sources_dir / f"{source}.jsonl"
        if not target.exists():
            return 0
        with target.open(encoding="utf-8") as handle:
            lines = [line for line in handle if line.strip()]
        if len(lines) <= count:
            return 0
        _atomic_write(target, "".join(lines[:count]))
        return len(lines) - count

    def read_source(self, source: str) -> list[dict[str, Any]]:
        target = self.sources_dir / f"{source}.jsonl"
        if not target.exists():
            return []
        records: list[dict[str, Any]] = []
        with target.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid JSONL at {target}:{line_number}") from exc
                if isinstance(value, dict):
                    records.append(value)
        return records

    def read_json(self, relative: str, *, default: Any = None) -> Any:
        target = self.path / relative
        if not target.exists():
            return default
        with target.open(encoding="utf-8") as handle:
            return json.load(handle)


def _atomic_write(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        raise
