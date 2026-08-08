from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from .artifacts import (
    ARTIFACT_SCHEMA_VERSION,
    RunStore,
    confirmation_token,
    preflight_digest,
    strategy_digest,
)
from .config import Credentials
from .http import HttpSession
from .models import Strategy, language_code
from .providers import Provider, provider_for
from .ranking import RANKING_VERSION, deduplicate, rank_records


async def preflight(
    strategy: Strategy,
    session: HttpSession,
    credentials: Credentials,
) -> dict[str, Any]:
    async def inspect(source: str) -> tuple[str, dict[str, Any]]:
        provider = provider_for(source, session, credentials)
        try:
            count = await provider.count(strategy.strategies[source])
            return source, {"status": "available", "count": count, "error": None}
        except Exception as exc:  # provider failures are serialized for mode policy
            return source, {"status": "unavailable", "count": None, "error": str(exc)}

    pairs = await asyncio.gather(*(inspect(source) for source in strategy.strategies))
    sources = dict(pairs)
    counts = {
        source: int(detail["count"])
        for source, detail in sources.items()
        if detail["status"] == "available"
    }
    result: dict[str, Any] = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "mode": strategy.mode,
        "limit_per_source": strategy.limit_per_source,
        "strategy_digest": strategy_digest(strategy),
        "sources": sources,
        "ready": all(detail["status"] == "available" for detail in sources.values()),
    }
    if strategy.limit_per_source == "all" and result["ready"]:
        result["confirmation_token"] = confirmation_token(strategy, counts)
        result["confirmation_required"] = True
        result["expected_total"] = sum(counts.values())
    result["preflight_digest"] = preflight_digest(result)
    return result


async def execute_search(
    strategy: Strategy,
    store: RunStore,
    session: HttpSession,
    credentials: Credentials,
    preflight_result: dict[str, Any],
) -> dict[str, Any]:
    manifest = store.initialize(strategy.question, strategy, credentials)
    manifest["status"] = "running"
    store.write_manifest(manifest)
    lock = asyncio.Lock()

    async def persist() -> None:
        async with lock:
            store.write_manifest(manifest)

    async def search_source(source: str) -> tuple[str, str | None]:
        source_preflight = preflight_result["sources"].get(source) or {}
        state = manifest["sources"][source]
        if source_preflight.get("status") != "available":
            state.update(status="omitted", error=source_preflight.get("error"))
            await persist()
            return source, str(source_preflight.get("error") or "source unavailable")
        if state.get("status") == "complete":
            return source, None
        provider = provider_for(source, session, credentials)
        state["reported_total"] = source_preflight.get("count")
        state["status"] = "running"
        await persist()
        try:
            await _retrieve_source(strategy, source, provider, store, state, persist)
            state["status"] = "complete"
            state["error"] = None
            await persist()
            return source, None
        except Exception as exc:
            state["status"] = "failed"
            state["error"] = str(exc)
            await persist()
            return source, str(exc)

    outcomes = await asyncio.gather(*(search_source(source) for source in strategy.strategies))
    failures = {source: error for source, error in outcomes if error}
    records = [
        record
        for source in strategy.strategies
        for record in store.read_source(source)
    ]
    deduplicated = deduplicate(records)
    ranked = rank_records(deduplicated, strategy.question)
    store.write_jsonl("results.jsonl", deduplicated)
    store.write_jsonl("ranked-results.jsonl", ranked)
    summary = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "completed_at": datetime.now(UTC).isoformat(),
        "mode": strategy.mode,
        "ranking_version": RANKING_VERSION,
        "records_by_source": {
            source: len(store.read_source(source)) for source in strategy.strategies
        },
        "records_before_deduplication": len(records),
        "records_after_deduplication": len(deduplicated),
        "source_failures": failures,
        "artifacts": {
            "native_results": "sources/<source>.jsonl",
            "deduplicated_results": "results.jsonl",
            "ranked_results": "ranked-results.jsonl",
        },
        "ranking_disclaimer": (
            "Prioritization heuristic only; not GRADE, risk-of-bias, evidence quality, "
            "or a systematic-review conclusion."
        ),
    }
    store.write_json("summary.json", summary)
    manifest["status"] = "failed" if failures and strategy.mode == "review" else "complete"
    manifest["source_failures"] = failures
    store.write_manifest(manifest)
    return summary


async def _retrieve_source(
    strategy: Strategy,
    source: str,
    provider: Provider,
    store: RunStore,
    state: dict[str, Any],
    persist: Any,
) -> None:
    limit = strategy.limit_per_source
    target = None if limit == "all" else int(limit)
    retained = int(state.get("retained") or 0)
    retrieved = int(state.get("retrieved") or 0)
    cursor = state.get("cursor")
    seen_cursors: set[str] = set()
    while target is None or retained < target:
        page_size = provider.page_size if target is None else min(
            provider.page_size, max(target - retained, 1)
        )
        page = await provider.fetch_page(strategy.strategies[source], cursor, page_size)
        records = page.records
        for offset, record in enumerate(records, start=1):
            record["source_rank"] = retrieved + offset
            record["retrieved_at"] = datetime.now(UTC).isoformat()
            record["query_variant"] = strategy.strategies[source].selected_variant
        filtered = [record for record in records if _passes_filters(record, strategy)]
        if target is not None:
            filtered = filtered[: target - retained]
        if filtered:
            store.append_source(source, filtered)
        retrieved += len(records)
        retained += len(filtered)
        state.update(
            cursor=page.next_cursor,
            retrieved=retrieved,
            retained=retained,
            reported_total=page.total if page.total is not None else state.get("reported_total"),
        )
        await persist()
        if not records or page.next_cursor is None:
            break
        cursor_key = str(page.next_cursor)
        if cursor_key in seen_cursors:
            raise RuntimeError(f"{source} returned a repeated pagination cursor")
        seen_cursors.add(cursor_key)
        cursor = page.next_cursor
    reported = state.get("reported_total")
    state["truncated"] = bool(reported is not None and retrieved < int(reported))


def _passes_filters(record: dict[str, Any], strategy: Strategy) -> bool:
    filters = strategy.question.filters
    language = language_code(str(record.get("language") or ""))
    if filters.languages and language:
        allowed = {language_code(value) for value in filters.languages}
        if language not in allowed:
            return False
    types = {str(value).casefold() for value in record.get("publication_types") or []}
    if filters.publication_types and types:
        requested = {value.casefold() for value in filters.publication_types}
        if not any(any(wanted in actual for actual in types) for wanted in requested):
            return False
    return True
