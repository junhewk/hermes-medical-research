from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
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
            return source, {
                "status": "unavailable",
                "count": None,
                "error": credentials.redact(str(exc)),
            }

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
    if manifest.get("research_parent"):
        from hermes_medical_research.workspace import Workspace

        parent = Workspace(store.path / manifest["research_parent"])
        key = strategy_digest(strategy)
        with parent.lock:
            entry = parent.load()["searches"].get(key)
            if not entry:
                raise ValueError("research search has no matching budget reservation")
            if manifest["status"] != "complete":
                parent.reserve(store.path, strategy)
            else:
                parent.check_budget(entry["allocation"], excluding=key)
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
            message = credentials.redact(str(exc))
            state["status"] = "failed"
            state["error"] = message
            await persist()
            return source, message

    outcomes = await asyncio.gather(*(search_source(source) for source in strategy.strategies))
    failures = {source: error for source, error in outcomes if error}
    by_source = {source: store.read_source(source) for source in strategy.strategies}
    records = [record for source_records in by_source.values() for record in source_records]
    deduplicated = deduplicate(records)
    ranked_as_of = _ranking_date(strategy)
    ranked = rank_records(deduplicated, strategy.question, today=ranked_as_of)
    store.write_jsonl("results.jsonl", deduplicated)
    store.write_jsonl("ranked-results.jsonl", ranked)
    summary = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "completed_at": datetime.now(UTC).isoformat(),
        "mode": strategy.mode,
        "ranking_version": RANKING_VERSION,
        "ranked_as_of": ranked_as_of.isoformat(),
        "records_by_source": {source: len(values) for source, values in by_source.items()},
        "records_filtered_by_source": {
            source: int((manifest["sources"].get(source) or {}).get("filtered_out") or 0)
            for source in strategy.strategies
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


def _ranking_date(strategy: Strategy) -> date:
    """Anchor recency scoring to the run itself.

    `recency_score` is a function of "now", so ranking against the wall clock makes
    `ranked-results.jsonl` change every time a run is resumed or re-executed. The strategy's
    creation date is immutable and digest-bound, so ranking against it is reproducible.
    """
    try:
        return datetime.fromisoformat(strategy.created_at).date()
    except (TypeError, ValueError):
        return datetime.now(UTC).date()


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
    raw_budget = bool(store.read_json("manifest.json").get("research_parent"))
    retained = int(state.get("retained") or 0)
    retrieved = int(state.get("retrieved") or 0)
    filtered_out = int(state.get("filtered_out") or 0)
    cursor = state.get("cursor")
    discarded = store.truncate_source(source, retained)
    if discarded:
        state["resume_discarded_records"] = discarded
    # A resumed cursor has already been served once; it must count toward loop detection.
    seen_cursors: set[str] = {str(cursor)} if cursor is not None else set()
    while target is None or (retrieved if raw_budget else retained) < target:
        page_size = (
            provider.page_size
            if target is None
            else min(provider.page_size, max(target - (retrieved if raw_budget else retained), 1))
        )
        page = await provider.fetch_page(strategy.strategies[source], cursor, page_size)
        records = page.records
        for offset, record in enumerate(records, start=1):
            # Authoritative source_rank: only this layer knows the position across pages. Ranks
            # are the provider's native positions, so a client-side filter leaves gaps in the
            # retained set — `filtered_out` in the manifest accounts for them.
            record["source_rank"] = retrieved + offset
            record["retrieved_at"] = datetime.now(UTC).isoformat()
            record["query_variant"] = strategy.strategies[source].selected_variant
        filtered = [record for record in records if _passes_filters(record, strategy)]
        filtered_out += len(records) - len(filtered)
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
            filtered_out=filtered_out,
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
