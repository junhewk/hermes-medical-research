"""Turn a plain-language research request into a protocol the operator confirms.

The operator states the request in their own words. One constrained call turns it into a schema-3
request, the same normalizer a Run uses validates it, and the result is printed with its digest and
the exact command that creates the Review. Nothing is created until that command runs: a protocol
that validates can still be wrong, and only the person who asked can say.

A draft keeps the operator's words verbatim, so a rejected attempt can be revised with one added
sentence instead of retyped, and the generated request is written beside it for hand editing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import answers, hermes
from .automation import AutomationEngine
from .search.models import ValidationError
from .workspace import digest, normalize_protocol

ATTEMPTS = 2
DRAFT_PREFIX = "draft-"


def drafts_root(store: Path) -> Path:
    return Path(store) / "intake"


def draft_path(store: Path, draft_id: str) -> Path:
    if not draft_id.startswith(DRAFT_PREFIX):
        raise ValidationError(f"a draft id starts with {DRAFT_PREFIX}")
    return drafts_root(store) / f"{draft_id}.json"


def request_path(store: Path, draft_id: str) -> Path:
    return drafts_root(store) / draft_id / "request.json"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def read_draft(store: Path, draft_id: str) -> dict[str, Any]:
    try:
        return json.loads(draft_path(store, draft_id).read_text())
    except FileNotFoundError as exc:
        raise ValidationError(f"no intake draft {draft_id}") from exc


def list_drafts(store: Path) -> dict[str, Any]:
    root = drafts_root(store)
    drafts = []
    for path in sorted(root.glob(f"{DRAFT_PREFIX}*.json")) if root.is_dir() else []:
        draft = json.loads(path.read_text())
        drafts.append({
            "draft_id": draft["draft_id"],
            "state": draft["state"],
            "question": (draft.get("protocol") or {}).get("question", {}).get("question")
            or draft["text"][:80],
            "created_at": draft["created_at"],
            "confirmed_review": draft.get("confirmed_review"),
        })
    return {"drafts": drafts}


def propose(
    store: Path,
    text: str,
    *,
    hermes_home: Path | None = None,
    hermes_executable: Path | None = None,
    mode: str = "report",
    records: int | None = None,
    fulltexts: int | None = None,
    language: str = "en",
    clarifications: list[str] | None = None,
    draft_id: str | None = None,
    call: Any = None,
) -> dict[str, Any]:
    """Run one constrained call, validate it as a Run would, and store the draft."""
    words = (text or "").strip()
    if not words:
        raise ValidationError("say what the review should answer, in your own words")
    home = hermes.profile_home(hermes_home)
    executable = str(hermes_executable or hermes.hermes_executable())
    runner = call or hermes.invoke_call_session
    profile = "hmr-intake"
    prefix = "\n\n".join([
        answers.INSTRUCTIONS["intake"],
        answers.framework_text(),
        answers.contract_text("intake"),
    ])
    draft_id = draft_id or DRAFT_PREFIX + uuid4().hex[:12]
    asked = list(clarifications or [])
    errors: list[str] = []
    protocol: dict[str, Any] | None = None
    request: dict[str, Any] = {}
    hint: str | None = None
    attempts = 0
    while attempts < ATTEMPTS and protocol is None:
        attempts += 1
        tail = "Request, in the researcher's own words:\n" + words
        for clarification in asked:
            tail += f"\nThe researcher adds: {clarification}"
        if hint:
            tail += f"\n\nYour previous answer was rejected: {hint}\nAnswer again, corrected."
        completed = runner(executable, home, profile, f"{prefix}\n\n{tail}", "intake")
        try:
            # Pruned before the shape check: a model that answers every component key leaves
            # blocks whose empty label and text would fail the check for the wrong reason.
            answer = _prune(answers.read_object(completed.stdout or ""))
            answers.check_shape(answer, answers.INTAKE_SCHEMA)
            request = {**answer, "schema_version": "3"}
            protocol = normalize_protocol(
                request, mode=mode, records=records, fulltexts=fulltexts, language=language
            )
        except ValidationError as exc:
            hint = str(exc)
            errors = [hint]
            protocol = None
    draft = {
        "schema_version": "1",
        "draft_id": draft_id,
        "state": "valid" if protocol else "invalid",
        "text": words,
        "clarifications": asked,
        "attempts": attempts,
        "request_file": str(request_path(store, draft_id)),
        "protocol": protocol,
        "protocol_digest": digest(protocol) if protocol else None,
        "errors": errors,
        "created_at": _now(),
        "confirmed_review": None,
    }
    _write(draft_path(store, draft_id), draft)
    if request:
        _write(request_path(store, draft_id), request)
    return draft


def _prune(answer: dict[str, Any]) -> dict[str, Any]:
    """Drop component blocks the model left empty.

    The schema cannot know which components the chosen framework allows, so a model that answers
    every key produces a request the protocol validator refuses. An empty block carries no meaning,
    so removing it is not a reinterpretation; a component with real text is left in place and the
    validator still rejects it, with the reason the next attempt carries.
    """
    if not isinstance(answer, dict):
        raise ValidationError(
            "the answer was not one JSON object; return only the object the contract describes"
        )
    components = answer.get("components")
    if not isinstance(components, dict):
        return answer
    kept = {
        key: block
        for key, block in components.items()
        if isinstance(block, dict)
        and any(str(group.get("text") or "").strip() for group in block.get("groups") or [])
    }
    return {**answer, "components": kept}


def revise(store: Path, draft_id: str, clarification: str, **kwargs: Any) -> dict[str, Any]:
    """Add one sentence to a draft and ask again, keeping the original words intact."""
    draft = read_draft(store, draft_id)
    if draft.get("confirmed_review"):
        raise ValidationError(f"{draft_id} already created review {draft['confirmed_review']}")
    if not clarification.strip():
        raise ValidationError("say what to clarify")
    return propose(
        store,
        draft["text"],
        clarifications=[*draft.get("clarifications", []), clarification.strip()],
        draft_id=draft_id,
        **kwargs,
    )


def confirm(
    store: Path,
    draft_id: str,
    *,
    name: str,
    schedule: str = "once",
    timezone_name: str | None = None,
    mode: str = "report",
    records: int | None = None,
    fulltexts: int | None = None,
    language: str = "en",
) -> dict[str, Any]:
    """Create the Review from a validated draft, refusing one that changed since it was shown."""
    draft = read_draft(store, draft_id)
    if draft["state"] != "valid":
        raise ValidationError(
            f"{draft_id} did not validate; revise it or edit {draft['request_file']} by hand"
        )
    if draft.get("confirmed_review"):
        raise ValidationError(f"{draft_id} already created review {draft['confirmed_review']}")
    request = json.loads(Path(draft["request_file"]).read_text())
    protocol = normalize_protocol(
        request, mode=mode, records=records, fulltexts=fulltexts, language=language
    )
    if digest(protocol) != draft["protocol_digest"]:
        raise ValidationError(
            f"the request for {draft_id} changed after it was shown; run "
            f"`hmr review intake --show {draft_id}` and read it again"
        )
    automation = AutomationEngine(Path(store))
    created = automation.create_review(
        name,
        request,
        schedule=schedule,
        timezone=timezone_name or hermes.host_timezone(),
        mode=mode,
        records=records,
        fulltexts=fulltexts,
        language=language,
    )
    draft["confirmed_review"] = created["name"]
    _write(draft_path(store, draft_id), draft)
    from . import steps

    steps.set_active_review(Path(store), created["name"], actor="intake")
    return {**created, "draft_id": draft_id, "active_review": created["name"]}


def discard(store: Path, draft_id: str) -> dict[str, Any]:
    draft_path(store, draft_id).unlink(missing_ok=True)
    return {"draft_id": draft_id, "discarded": True}


def render(draft: dict[str, Any]) -> str:
    """What the operator reads before deciding, with the command that creates the Review."""
    if draft["state"] != "valid":
        lines = [
            f"intake {draft['draft_id']} could not be validated after {draft['attempts']} "
            "attempts.",
        ]
        lines += [f"  {error}" for error in draft["errors"]]
        lines += [
            "Your words are kept verbatim in the draft. Choose one:",
            f"  hmr review intake --revise {draft['draft_id']} \"one clarifying sentence\"",
            f"  edit {draft['request_file']} and run `hmr review create --request` with it",
        ]
        return "\n".join(lines)
    protocol = draft["protocol"]
    question = protocol["question"]
    lines = [
        f"intake {draft['draft_id']} · {question['framework']} · digest "
        f"{draft['protocol_digest'][:12]}",
        f"question: {question.get('question', '')}",
        "components: " + ", ".join(sorted(question.get("components") or {})),
        "sources: " + ", ".join(question.get("sources") or []),
        f"include ({len(protocol['eligibility']['include'])}):",
    ]
    lines += [f"  - {item}" for item in protocol["eligibility"]["include"]]
    lines.append(f"exclude ({len(protocol['eligibility']['exclude'])}):")
    lines += [f"  - {item}" for item in protocol["eligibility"]["exclude"]]
    lines.append("outcomes: " + ", ".join(protocol["outcomes"]))
    lines += [
        "",
        "Read it, then create the Review with:",
        f"  hmr review intake --confirm {draft['draft_id']} --name SLUG",
    ]
    return "\n".join(lines)
