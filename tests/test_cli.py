from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from hermes_medical_research.search.artifacts import (
    RunStore,
    confirmation_token,
    preflight_digest,
    strategy_digest,
)
from hermes_medical_research.search.cli import (
    _approve_command,
    _parse_limit,
    _parse_sources,
    _parse_variants,
    _preflight_command,
    _search_command,
    _select_sources,
    _years_ago,
    build_parser,
)
from hermes_medical_research.search.config import Credentials
from hermes_medical_research.search.models import Question, ValidationError
from hermes_medical_research.search.providers import Page
from hermes_medical_research.search.query import compile_strategy


def test_cli_exposes_all_commands() -> None:
    help_text = build_parser().format_help()
    for command in ("doctor", "plan", "approve", "preflight", "search", "run"):
        assert command in help_text


def test_review_requires_explicit_limit() -> None:
    with pytest.raises(ValidationError, match="requires"):
        _parse_limit(None, mode="review")
    assert _parse_limit("all", mode="review") == "all"
    assert _parse_limit(None, mode="quick") == 20
    with pytest.raises(ValidationError, match="only in review"):
        _parse_limit("all", mode="quick")


def test_source_parser() -> None:
    assert _parse_sources("pubmed,pmc,pubmed") == ["pubmed", "pmc"]
    with pytest.raises(ValidationError, match="unsupported"):
        _parse_sources("google-scholar")


def test_per_source_variant_parser() -> None:
    assert _parse_variants(
        ["pubmed=precision"], sources=["pubmed", "openalex"], precision=False
    ) == {"pubmed": "precision"}
    with pytest.raises(ValidationError, match="cannot be combined"):
        _parse_variants(
            ["pubmed=sensitivity"], sources=["pubmed"], precision=True
        )
    with pytest.raises(ValidationError, match="not selected"):
        _parse_variants(
            ["scopus=precision"], sources=["pubmed"], precision=False
        )


def test_scopus_is_automatic_when_configured_and_excludable() -> None:
    question = _question()
    credentials = Credentials(scopus_api_key="configured")
    assert _select_sources(question, credentials, None, None)[-1] == "scopus"
    assert "scopus" not in _select_sources(question, credentials, None, "scopus")


def test_three_year_default_handles_leap_day() -> None:
    assert _years_ago(date(2024, 2, 29), 3) == date(2021, 2, 28)


@pytest.mark.asyncio
async def test_review_search_stops_on_unavailable_source(tmp_path: Path) -> None:
    strategy = compile_strategy(
        _question(), mode="review", limit_per_source=10, sources=["pubmed"]
    )
    store = RunStore(tmp_path)
    store.initialize(strategy.question, strategy, Credentials())
    await _approve_command(tmp_path, strategy_digest(strategy))
    _write_preflight(store, strategy, ready=False)
    with pytest.raises(ValueError, match="review preflight failed"):
        await _search_command(tmp_path, confirm_all=None)


@pytest.mark.asyncio
async def test_all_search_requires_preflight_token(tmp_path: Path) -> None:
    strategy = compile_strategy(
        _question(), mode="review", limit_per_source="all", sources=["openalex"]
    )
    store = RunStore(tmp_path)
    store.initialize(strategy.question, strategy, Credentials())
    await _approve_command(tmp_path, strategy_digest(strategy))
    _write_preflight(store, strategy, ready=True)
    with pytest.raises(ValueError, match="requires --confirm-all"):
        await _search_command(tmp_path, confirm_all=None)


@pytest.mark.asyncio
async def test_review_preflight_and_search_require_digest_bound_approval(
    tmp_path: Path,
) -> None:
    strategy = compile_strategy(
        _question(), mode="review", limit_per_source=10, sources=["pubmed"]
    )
    store = RunStore(tmp_path)
    store.initialize(strategy.question, strategy, Credentials())
    with pytest.raises(ValueError, match="has not been approved"):
        await _preflight_command(tmp_path)
    with pytest.raises(ValueError, match="has not been approved"):
        await _search_command(tmp_path, confirm_all=None)
    with pytest.raises(ValueError, match="does not match"):
        await _approve_command(tmp_path, "wrong")
    await _approve_command(tmp_path, strategy_digest(strategy))
    assert store.read_json("manifest.json")["status"] == "strategy_approved"
    with pytest.raises(ValueError, match="explicit successful preflight"):
        await _search_command(tmp_path, confirm_all=None)


@pytest.mark.asyncio
async def test_all_confirmation_is_recorded_and_search_completes(
    monkeypatch, tmp_path: Path
) -> None:
    strategy = compile_strategy(
        _question(), mode="review", limit_per_source="all", sources=["openalex"]
    )
    store = RunStore(tmp_path)
    store.initialize(strategy.question, strategy, Credentials())
    await _approve_command(tmp_path, strategy_digest(strategy))
    checked = _preflight(strategy, ready=True)
    _write_preflight(store, strategy, ready=True)

    class OnePageProvider:
        page_size = 10

        async def fetch_page(self, _strategy, _cursor, _page_size):
            return Page(
                records=[
                    {
                        "source": "openalex",
                        "source_id": "W1",
                        "source_rank": 1,
                        "title": "Intervention in adults",
                        "abstract": "Intervention study",
                        "authors": [],
                        "journal": None,
                        "publication_date": "2025-01-01",
                        "year": "2025",
                        "doi": "10.1/example",
                        "pmid": None,
                        "pmcid": None,
                        "citation_count": 0,
                        "url": None,
                        "publication_types": [],
                        "mesh_terms": [],
                        "language": None,
                    }
                ],
                next_cursor=None,
                total=1,
            )

    monkeypatch.setattr(
        "hermes_medical_research.search.orchestrator.provider_for",
        lambda _source, _session, _credentials: OnePageProvider(),
    )
    token = confirmation_token(strategy, {"openalex": 42})
    assert await _search_command(tmp_path, confirm_all=token) == 0
    approval = store.read_json("approval.json")
    assert approval["all_results"]["expected_total"] == 42
    assert approval["all_results"]["preflight_digest"] == checked["preflight_digest"]
    assert store.read_json("manifest.json")["status"] == "complete"


def _preflight(strategy, *, ready: bool) -> dict[str, object]:
    source = next(iter(strategy.strategies))
    result: dict[str, object] = {
        "schema_version": "2",
        "created_at": "2026-08-08T00:00:00+00:00",
        "mode": strategy.mode,
        "limit_per_source": strategy.limit_per_source,
        "strategy_digest": strategy_digest(strategy),
        "sources": {
            source: {
                "status": "available" if ready else "unavailable",
                "count": 42 if ready else None,
                "error": None if ready else "missing",
            }
        },
        "ready": ready,
    }
    if strategy.limit_per_source == "all" and ready:
        result["confirmation_required"] = True
        result["expected_total"] = 42
    result["preflight_digest"] = preflight_digest(result)
    return result


def _write_preflight(store: RunStore, strategy, *, ready: bool) -> None:
    store.write_json("preflight.json", _preflight(strategy, ready=ready))
    manifest = store.read_json("manifest.json")
    manifest["status"] = "preflight_ready" if ready else "preflight_failed"
    store.write_manifest(manifest)


def _question() -> Question:
    return Question.from_dict(
        {
            "schema_version": "1",
            "framework": "PICO",
            "question": "Question",
            "components": {
                "population": {"text": "adults"},
                "intervention": {"text": "intervention"},
            },
        }
    )
