from __future__ import annotations

import httpx
import pytest

from hermes_medical_research.search.artifacts import RunStore, strategy_digest
from hermes_medical_research.search.biomedical import (
    ClinicalTrialsProvider,
    EuropePMCProvider,
    europe_record,
    trial_record,
)
from hermes_medical_research.search.config import Credentials
from hermes_medical_research.search.http import HttpSession
from hermes_medical_research.search.models import Question, ValidationError
from hermes_medical_research.search.orchestrator import execute_search
from hermes_medical_research.search.providers import Page
from hermes_medical_research.search.query import compile_strategy
from hermes_medical_research.search.ranking import deduplicate
from hermes_medical_research.workspace import Workspace


def question(framework="PICO", **components):
    return Question.from_dict(
        {
            "schema_version": "3",
            "framework": framework,
            "question": "Synthetic question",
            "components": {
                key: {"groups": [{"label": key, "text": value}]}
                for key, value in components.items()
            },
        }
    )


@pytest.mark.parametrize(
    "framework,components,selected",
    [
        (
            "PICO",
            {"population": "adults", "intervention": "exercise", "outcome": "blood pressure"},
            ["population", "intervention"],
        ),
        (
            "PECO",
            {"population": "adults", "exposure": "pollution", "outcome": "mortality"},
            ["population", "exposure"],
        ),
        (
            "PCC",
            {"population": "adults", "concept": "access", "context": "hospitals"},
            ["population", "concept"],
        ),
        (
            "DIAGNOSTIC",
            {"population": "adults", "index_test": "ultrasound", "target_condition": "cancer"},
            ["index_test", "target_condition"],
        ),
        (
            "PROGNOSIS",
            {"population": "adults", "prediction_model": "score", "outcome": "survival"},
            ["population", "prediction_model"],
        ),
    ],
)
def test_schema3_frameworks_have_sensitive_search_blocks(framework, components, selected):
    q = question(framework, **components)
    assert q.search_components == selected
    strategy = compile_strategy(
        q, mode="quick", limit_per_source=10, sources=["pubmed", "europe-pmc"]
    )
    assert not q.filters.from_date
    for name, value in components.items():
        if name in selected:
            assert value in strategy.strategies["pubmed"].query
        else:
            assert value not in strategy.strategies["pubmed"].query
    assert Question.from_dict(q.to_dict()).to_dict() == q.to_dict()
    assert strategy_digest(strategy) == strategy_digest(
        type(strategy).from_dict(strategy.to_dict())
    )


def test_registry_filters_rejected_and_registrations_not_deduplicated_with_papers():
    q = question(population="adults", intervention="exercise")
    q.filters.from_date = "2020-01-01"
    with pytest.raises(ValidationError, match="publication-date"):
        compile_strategy(q, mode="quick", limit_per_source=1, sources=["clinicaltrials"])
    trial = trial_record(
        {
            "protocolSection": {
                "identificationModule": {"nctId": "NCT00000001", "briefTitle": "Same title"},
                "referencesModule": {"references": [{"pmid": "123"}]},
            }
        }
    )
    paper = europe_record({"id": "123", "source": "MED", "title": "Same title"})
    assert trial["pmid"] is None and trial["related_pmids"] == ["123"]
    assert len(deduplicate([trial, paper])) == 2
    retracted = {**paper, "is_retracted": True, "source": "openalex"}
    assert deduplicate([paper, retracted])[0]["is_retracted"]


@pytest.mark.asyncio
async def test_europe_and_registry_pagination():
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.host == "clinicaltrials.gov":
            return httpx.Response(
                200,
                json={
                    "totalCount": 3,
                    "nextPageToken": "registry-next",
                    "studies": [
                        {
                            "protocolSection": {"identificationModule": {"nctId": "NCT00000001"}},
                            "hasResults": True,
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "hitCount": 3,
                "nextCursorMark": "epmc-next",
                "resultList": {
                    "result": [{"source": "MED", "id": "123", "abstractText": "An abstract"}]
                },
            },
        )

    q = question(population="adults", intervention="exercise")
    strategies = compile_strategy(
        q, mode="quick", limit_per_source=2, sources=["europe-pmc", "clinicaltrials"]
    )
    async with HttpSession(
        transport=httpx.MockTransport(handler), intervals={"europe-pmc": 0, "clinicaltrials": 0}
    ) as session:
        for source, provider in (
            ("europe-pmc", EuropePMCProvider),
            ("clinicaltrials", ClinicalTrialsProvider),
        ):
            active = provider(session, Credentials())
            strategy = strategies.strategies[source]
            assert await active.count(strategy) == 3
            first = await active.fetch_page(strategy, None, 2)
            assert first.next_cursor
            await active.fetch_page(strategy, first.next_cursor, 1)
        assert seen[-1].url.params["pageToken"] == "registry-next"
        assert seen[2].url.params["cursorMark"] == "epmc-next"


@pytest.mark.asyncio
async def test_citation_links_keep_traversal_provenance():
    def handler(request):
        assert "/MED/123/references/1/" in request.url.path
        return httpx.Response(
            200,
            json={
                "hitCount": 1,
                "referenceList": {
                    "reference": [
                        {"id": "456", "title": "Referenced paper", "authorString": "A Author"}
                    ]
                },
            },
        )

    q = question(population="adults", intervention="exercise")
    strategy = compile_strategy(
        q, mode="quick", limit_per_source=1, sources=["europe-pmc"]
    ).strategies["europe-pmc"]
    strategy.request_parameters = {"link_seed": "123", "link_direction": "references"}
    async with HttpSession(
        transport=httpx.MockTransport(handler), intervals={"europe-pmc": 0}
    ) as session:
        provider = EuropePMCProvider(session, Credentials())
        assert await provider.count(strategy) == 1
        page = await provider.fetch_page(strategy, None, 1)
        assert page.next_cursor is None
        assert page.records[0]["pmid"] == "456"
        assert page.records[0]["citation_chaining"]["link_seed"] == "123"


@pytest.mark.asyncio
async def test_bound_search_budget_counts_filtered_records(tmp_path, monkeypatch):
    q = question(population="adults", intervention="exercise")
    q.sources = ["europe-pmc"]
    # Client-side language filtering must not cause extra pages beyond the research cap.
    q.filters.languages = ["en"]
    parent = Workspace(tmp_path / "research")
    parent.init(
        {
            **q.to_dict(),
            "eligibility": {"include": ["Adults"], "exclude": []},
            "outcomes": ["An outcome"],
            "search_rationale": "Synthetic",
        },
        mode="report",
        records=2,
        fulltexts=1,
        language="en",
    )
    strategy = compile_strategy(q, mode="quick", limit_per_source=2, sources=["europe-pmc"])
    store = RunStore(tmp_path / "search")
    parent.reserve(store.path, strategy)
    manifest = store.initialize(q, strategy, Credentials())
    manifest["research_parent"] = "../research"
    store.write_manifest(manifest)

    class Provider:
        page_size = 2
        calls = 0

        async def fetch_page(self, *_args):
            self.calls += 1
            assert self.calls == 1
            return Page(
                [
                    {
                        "source": "europe-pmc",
                        "source_id": str(i),
                        "title": "French fixture",
                        "language": "fr",
                    }
                    for i in range(2)
                ],
                "next",
                10,
            )

    active = Provider()
    monkeypatch.setattr(
        "hermes_medical_research.search.orchestrator.provider_for", lambda *_: active
    )
    async with HttpSession(transport=httpx.MockTransport(lambda _: httpx.Response(500))) as session:
        await execute_search(
            strategy,
            store,
            session,
            Credentials(),
            {"sources": {"europe-pmc": {"status": "available", "count": 10}}},
        )
    parent.attach(store.path)
    assert parent.allocations()["europe-pmc"] == 2
    assert parent.status()["records"] == 0
    state = store.read_json("manifest.json")["sources"]["europe-pmc"]
    assert state["filtered_out"] == 2 and state["truncated"]
