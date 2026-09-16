from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hermes_medical_research.search.artifacts import (
    RunStore,
    confirmation_token,
    strategy_digest,
)
from hermes_medical_research.search.config import Credentials
from hermes_medical_research.search.http import HttpSession
from hermes_medical_research.search.models import Question
from hermes_medical_research.search.orchestrator import execute_search, preflight
from hermes_medical_research.search.providers import Page
from hermes_medical_research.search.query import compile_strategy
from hermes_medical_research.workspace import Workspace


def question() -> Question:
    return Question.from_dict(
        {
            "schema_version": "1",
            "framework": "PCC",
            "question": "LLMs in medical education",
            "components": {
                "population": {"text": "medical students"},
                "concept": {"text": "large language models", "synonyms": ["LLM"]},
            },
        }
    )


def article(source: str, source_id: str, rank: int) -> dict[str, object]:
    return {
        "source": source,
        "source_id": source_id,
        "source_rank": rank,
        "title": f"Large language models in medical students {source_id}",
        "abstract": "LLM education",
        "authors": [],
        "journal": None,
        "publication_date": "2025-01-01",
        "year": "2025",
        "doi": f"10.1/{source_id}",
        "pmid": None,
        "pmcid": None,
        "citation_count": 0,
        "url": None,
        "publication_types": [],
        "mesh_terms": [],
        "language": None,
    }


class FakeProvider:
    page_size = 1

    def __init__(self, source: str, *, fail: bool = False) -> None:
        self.source = source
        self.fail = fail

    async def count(self, _strategy):
        if self.fail:
            raise RuntimeError("not available")
        return 2

    async def fetch_page(self, _strategy, cursor, _page_size):
        position = int(cursor or 0)
        if position >= 2:
            return Page([], None, 2)
        return Page(
            [article(self.source, f"{self.source}-{position + 1}", position + 1)],
            position + 1 if position + 1 < 2 else None,
            2,
        )


@pytest.mark.asyncio
async def test_quick_mode_records_omitted_source(monkeypatch, tmp_path: Path) -> None:
    strategy = compile_strategy(
        question(),
        mode="quick",
        limit_per_source=2,
        sources=["pubmed", "openalex"],
    )

    def factory(source, _session, _credentials):
        return FakeProvider(source, fail=source == "openalex")

    monkeypatch.setattr("hermes_medical_research.search.orchestrator.provider_for", factory)
    credentials = Credentials()
    async with HttpSession(intervals={"ncbi": 0}) as session:
        checked = await preflight(strategy, session, credentials)
        assert checked["ready"] is False
        store = RunStore(tmp_path / "run")
        summary = await execute_search(strategy, store, session, credentials, checked)
    assert summary["records_before_deduplication"] == 2
    assert "openalex" in summary["source_failures"]
    manifest = store.read_json("manifest.json")
    assert manifest["status"] == "complete"
    assert manifest["sources"]["pubmed"]["truncated"] is False


@pytest.mark.asyncio
async def test_resume_uses_checkpoint(monkeypatch, tmp_path: Path) -> None:
    strategy = compile_strategy(
        question(), mode="review", limit_per_source=2, sources=["pubmed"]
    )
    monkeypatch.setattr(
        "hermes_medical_research.search.orchestrator.provider_for",
        lambda source, _session, _credentials: FakeProvider(source),
    )
    credentials = Credentials()
    store = RunStore(tmp_path / "run")
    manifest = store.initialize(strategy.question, strategy, credentials)
    store.append_source("pubmed", [article("pubmed", "pubmed-1", 1)])
    manifest["sources"]["pubmed"].update(
        status="running", cursor=1, retrieved=1, retained=1, reported_total=2
    )
    store.write_manifest(manifest)
    checked = {
        "ready": True,
        "sources": {"pubmed": {"status": "available", "count": 2, "error": None}},
    }
    async with HttpSession(intervals={"ncbi": 0}) as session:
        summary = await execute_search(strategy, store, session, credentials, checked)
    assert summary["records_before_deduplication"] == 2
    assert [item["source_rank"] for item in store.read_source("pubmed")] == [1, 2]
    assert store.read_json("manifest.json")["sources"]["pubmed"]["truncated"] is False


@pytest.mark.asyncio
async def test_bounded_search_records_provider_truncation(monkeypatch, tmp_path: Path) -> None:
    strategy = compile_strategy(
        question(), mode="quick", limit_per_source=1, sources=["pubmed"]
    )
    monkeypatch.setattr(
        "hermes_medical_research.search.orchestrator.provider_for",
        lambda source, _session, _credentials: FakeProvider(source),
    )
    store = RunStore(tmp_path / "bounded")
    checked = {
        "ready": True,
        "sources": {"pubmed": {"status": "available", "count": 2, "error": None}},
    }
    async with HttpSession(intervals={"ncbi": 0}) as session:
        await execute_search(strategy, store, session, Credentials(), checked)
    assert store.read_json("manifest.json")["sources"]["pubmed"]["truncated"] is True


def test_full_retrieval_confirmation_token_is_stable() -> None:
    strategy = compile_strategy(
        question(), mode="review", limit_per_source="all", sources=["pubmed"]
    )
    assert confirmation_token(strategy, {"pubmed": 42}) == confirmation_token(
        strategy, {"pubmed": 42}
    )
    assert confirmation_token(strategy, {"pubmed": 42}) != confirmation_token(
        strategy, {"pubmed": 43}
    )


def available_preflight(source: str = "pubmed", count: int = 2) -> dict[str, object]:
    detail = {"status": "available", "count": count, "error": None}
    return {"ready": True, "sources": {source: detail}}


def filtered_question() -> Question:
    return Question.from_dict(
        {
            "schema_version": "2",
            "framework": "PCC",
            "question": "LLMs in medical education",
            "components": {
                "population": {"groups": [{"label": "p", "text": "medical students"}]},
                "concept": {"groups": [{"label": "c", "text": "large language models"}]},
            },
            "filters": {"languages": ["english"]},
        }
    )


class LanguageProvider(FakeProvider):
    """Two records per source, in the language representation PubMed actually returns."""

    def __init__(self, source: str, language: str) -> None:
        super().__init__(source)
        self.language = language

    async def fetch_page(self, _strategy, cursor, _page_size):
        position = int(cursor or 0)
        if position >= 2:
            return Page([], None, 2)
        item = article(self.source, f"{self.source}-{position + 1}", position + 1)
        item["language"] = self.language
        return Page([item], position + 1 if position + 1 < 2 else None, 2)


@pytest.mark.asyncio
async def test_pubmed_language_records_survive_a_language_filter(
    monkeypatch, tmp_path: Path
) -> None:
    """PubMed reports "eng"; a filter written as "english" must not discard the whole source."""
    strategy = compile_strategy(
        filtered_question(), mode="quick", limit_per_source=2, sources=["pubmed"]
    )
    monkeypatch.setattr(
        "hermes_medical_research.search.orchestrator.provider_for",
        lambda source, _session, _credentials: LanguageProvider(source, "eng"),
    )
    store = RunStore(tmp_path / "run")
    checked = available_preflight()
    async with HttpSession(intervals={"ncbi": 0}) as session:
        summary = await execute_search(strategy, store, session, Credentials(), checked)
    assert summary["records_by_source"]["pubmed"] == 2
    assert summary["records_filtered_by_source"]["pubmed"] == 0


@pytest.mark.asyncio
async def test_filtered_out_records_are_counted(monkeypatch, tmp_path: Path) -> None:
    strategy = compile_strategy(
        filtered_question(), mode="quick", limit_per_source=2, sources=["pubmed"]
    )
    monkeypatch.setattr(
        "hermes_medical_research.search.orchestrator.provider_for",
        lambda source, _session, _credentials: LanguageProvider(source, "ger"),
    )
    store = RunStore(tmp_path / "run")
    checked = available_preflight()
    async with HttpSession(intervals={"ncbi": 0}) as session:
        summary = await execute_search(strategy, store, session, Credentials(), checked)
    # A source that retrieves records and retains none must say so rather than look empty.
    assert summary["records_by_source"]["pubmed"] == 0
    assert summary["records_filtered_by_source"]["pubmed"] == 2
    assert store.read_json("manifest.json")["sources"]["pubmed"]["filtered_out"] == 2


@pytest.mark.asyncio
async def test_ranking_is_anchored_to_the_strategy_not_the_wall_clock(
    monkeypatch, tmp_path: Path
) -> None:
    strategy = compile_strategy(
        question(), mode="quick", limit_per_source=2, sources=["pubmed"]
    )
    strategy.created_at = "2024-03-05T12:00:00+00:00"
    monkeypatch.setattr(
        "hermes_medical_research.search.orchestrator.provider_for",
        lambda source, _session, _credentials: FakeProvider(source),
    )
    store = RunStore(tmp_path / "run")
    checked = available_preflight()
    async with HttpSession(intervals={"ncbi": 0}) as session:
        summary = await execute_search(strategy, store, session, Credentials(), checked)
        first = (store.path / "ranked-results.jsonl").read_text(encoding="utf-8")
        # Re-running a completed run must reproduce the artifact byte for byte.
        await execute_search(strategy, store, session, Credentials(), checked)
        second = (store.path / "ranked-results.jsonl").read_text(encoding="utf-8")
    assert summary["ranked_as_of"] == "2024-03-05"
    assert all(
        json.loads(line)["ranking"]["ranked_as_of"] == "2024-03-05"
        for line in first.splitlines()
    )
    assert first == second


@pytest.mark.asyncio
async def test_child_resume_repairs_a_missing_parent_reservation(tmp_path: Path) -> None:
    parent = Workspace(tmp_path / "research")
    request = question().to_dict()
    request.update(
        sources=["europe-pmc"],
        eligibility={"include": ["Humans"], "exclude": ["Animal-only"]},
        outcomes=[],
        search_rationale="Synthetic crash-recovery fixture.",
    )
    parent.init(request, mode="report", records=2, fulltexts=1, language="en")
    stored_question = Question.from_dict(parent.load()["protocol"]["question"])
    strategy = compile_strategy(
        stored_question,
        mode="quick",
        limit_per_source=2,
        sources=["europe-pmc"],
    )
    child = RunStore(parent.path / "searches" / "interrupted")
    manifest = child.initialize(stored_question, strategy, Credentials())
    manifest["research_parent"] = os.path.relpath(parent.path, child.path)
    child.write_manifest(manifest)
    assert parent.load()["searches"] == {}
    checked = {
        "sources": {
            "europe-pmc": {
                "status": "unavailable",
                "count": None,
                "error": "synthetic outage",
            }
        }
    }
    async with HttpSession() as session:
        await execute_search(strategy, child, session, Credentials(), checked)
    reservation = parent.load()["searches"][strategy_digest(strategy)]
    assert reservation["status"] == "reserved"
    assert reservation["path"] == str(child.path.resolve())


@pytest.mark.asyncio
async def test_resume_discards_records_written_past_the_checkpoint(
    monkeypatch, tmp_path: Path
) -> None:
    """A crash between append_source and the manifest write must not duplicate records."""
    strategy = compile_strategy(
        question(), mode="review", limit_per_source=2, sources=["pubmed"]
    )
    monkeypatch.setattr(
        "hermes_medical_research.search.orchestrator.provider_for",
        lambda source, _session, _credentials: FakeProvider(source),
    )
    store = RunStore(tmp_path / "run")
    manifest = store.initialize(strategy.question, strategy, Credentials())
    # Simulate the crash window: the page is on disk, the checkpoint never advanced past zero.
    store.append_source("pubmed", [article("pubmed", "pubmed-1", 1)])
    manifest["sources"]["pubmed"].update(
        status="running", cursor=None, retrieved=0, retained=0, reported_total=2
    )
    store.write_manifest(manifest)
    checked = available_preflight()
    async with HttpSession(intervals={"ncbi": 0}) as session:
        summary = await execute_search(strategy, store, session, Credentials(), checked)
    assert [item["source_id"] for item in store.read_source("pubmed")] == [
        "pubmed-1",
        "pubmed-2",
    ]
    assert summary["records_by_source"]["pubmed"] == 2
