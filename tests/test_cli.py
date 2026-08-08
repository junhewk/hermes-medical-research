from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from hermes_medical_search.artifacts import RunStore
from hermes_medical_search.cli import (
    _parse_limit,
    _parse_sources,
    _search_command,
    _select_sources,
    _years_ago,
    build_parser,
)
from hermes_medical_search.config import Credentials
from hermes_medical_search.models import Question, ValidationError
from hermes_medical_search.query import compile_strategy


def test_cli_exposes_all_commands() -> None:
    help_text = build_parser().format_help()
    for command in ("doctor", "plan", "preflight", "search", "run"):
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
    store.write_json(
        "preflight.json",
        {
            "ready": False,
            "sources": {
                "pubmed": {"status": "unavailable", "count": None, "error": "missing"}
            },
        },
    )
    with pytest.raises(ValueError, match="review preflight failed"):
        await _search_command(tmp_path, confirm_all=None)


@pytest.mark.asyncio
async def test_all_search_requires_preflight_token(tmp_path: Path) -> None:
    strategy = compile_strategy(
        _question(), mode="review", limit_per_source="all", sources=["openalex"]
    )
    store = RunStore(tmp_path)
    store.initialize(strategy.question, strategy, Credentials())
    store.write_json(
        "preflight.json",
        {
            "ready": True,
            "sources": {
                "openalex": {"status": "available", "count": 42, "error": None}
            },
        },
    )
    with pytest.raises(ValueError, match="requires --confirm-all"):
        await _search_command(tmp_path, confirm_all=None)


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
