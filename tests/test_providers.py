from __future__ import annotations

import httpx
import pytest

from hermes_medical_research.search.config import Credentials
from hermes_medical_research.search.http import HttpSession
from hermes_medical_research.search.models import Question
from hermes_medical_research.search.providers import (
    MeshResolver,
    NCBIProvider,
    OpenAlexProvider,
    ScopusProvider,
    SemanticScholarProvider,
)
from hermes_medical_research.search.query import compile_strategy


def strategy_for(source: str):
    question = Question.from_dict(
        {
            "schema_version": "1",
            "framework": "PICO",
            "question": "A question",
            "components": {
                "population": {"text": "adults"},
                "intervention": {"text": "treatment"},
            },
        }
    )
    return compile_strategy(
        question, mode="quick", limit_per_source=1, sources=[source]
    ).strategies[source]


@pytest.mark.asyncio
async def test_ncbi_count_fetch_and_mesh_resolution() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        database = request.url.params.get("db")
        if path.endswith("esearch.fcgi") and database == "mesh":
            return httpx.Response(200, json={"esearchresult": {"idlist": ["1"]}})
        if path.endswith("esummary.fcgi") and database == "mesh":
            # Shape copied from a live esummary response: ds_meshterms[0] is the descriptor and
            # the remainder are entry terms. efetch is NOT used here — it ignores retmode=xml
            # for db=mesh and returns a plain-text record.
            return httpx.Response(
                200,
                json={
                    "result": {
                        "uids": ["1"],
                        "1": {
                            "uid": "1",
                            "ds_meshterms": [
                                "Diabetes Mellitus, Type 2",
                                "Diabetes Mellitus, Type II",
                                "NIDDM",
                            ],
                        },
                    }
                },
            )
        if path.endswith("efetch.fcgi") and database == "mesh":
            raise AssertionError("MeSH resolution must not call efetch; it returns plain text")
        if path.endswith("esearch.fcgi") and request.url.params.get("retmax") == "0":
            return httpx.Response(200, json={"esearchresult": {"count": "1", "idlist": []}})
        if path.endswith("esearch.fcgi"):
            return httpx.Response(200, json={"esearchresult": {"count": "1", "idlist": ["123"]}})
        return httpx.Response(
            200,
            text=(
                "<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>123</PMID>"
                "<Article><ArticleTitle>Title</ArticleTitle></Article></MedlineCitation>"
                "</PubmedArticle></PubmedArticleSet>"
            ),
        )

    credentials = Credentials(ncbi_email="person@example.org")
    async with HttpSession(
        intervals={"ncbi": 0}, transport=httpx.MockTransport(handler)
    ) as session:
        provider = NCBIProvider(session, credentials, database="pubmed")
        strategy = strategy_for("pubmed")
        assert await provider.count(strategy) == 1
        page = await provider.fetch_page(strategy, None, 20)
        assert page.records[0]["pmid"] == "123"
        assert page.next_cursor is None
        resolver = MeshResolver(session, credentials)
        assert await resolver.resolve("type 2 diabetes") == "Diabetes Mellitus, Type 2"
        # An exact entry term resolves to its descriptor rather than being rejected...
        assert await resolver.resolve("NIDDM") == "Diabetes Mellitus, Type 2"
        # ...while a descriptor sharing no vocabulary with the candidate is rejected.
        assert await resolver.resolve("photosynthesis in ferns") is None


@pytest.mark.asyncio
async def test_ncbi_allows_bounded_anonymous_requests() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["tool"] == "hermes-medical-research"
        assert "email" not in request.url.params
        assert "api_key" not in request.url.params
        return httpx.Response(200, json={"esearchresult": {"count": "1", "idlist": []}})

    async with HttpSession(
        intervals={"ncbi": 0}, transport=httpx.MockTransport(handler)
    ) as session:
        provider = NCBIProvider(session, Credentials(), database="pubmed")
        assert await provider.count(strategy_for("pubmed")) == 1


@pytest.mark.asyncio
async def test_ncbi_accepts_pubmed_api_key_alias() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["api_key"] == "alias-key"
        return httpx.Response(200, json={"esearchresult": {"count": "1", "idlist": []}})

    credentials = Credentials.from_env({"PUBMED_API_KEY": "alias-key"})
    async with HttpSession(
        intervals={"ncbi": 0}, transport=httpx.MockTransport(handler)
    ) as session:
        provider = NCBIProvider(session, credentials, database="pubmed")
        assert await provider.count(strategy_for("pubmed")) == 1


@pytest.mark.asyncio
async def test_openalex_semantic_scholar_and_scopus_normalization() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.openalex.org":
            return httpx.Response(
                200,
                json={
                    "meta": {"count": 1, "next_cursor": None},
                    "results": [
                        {
                            "id": "https://openalex.org/W1",
                            "display_name": "OpenAlex title",
                            "publication_year": 2025,
                            "ids": {"doi": "https://doi.org/10.1/test"},
                            "authorships": [{"author": {"display_name": "A Author"}}],
                            "abstract_inverted_index": {"Abstract": [0], "text": [1]},
                            "cited_by_count": 4,
                        }
                    ],
                },
            )
        if request.url.host == "api.semanticscholar.org":
            assert request.headers["x-api-key"] == "s2-key"
            return httpx.Response(
                200,
                json={
                    "total": 1,
                    "next": 1,
                    "data": [
                        {
                            "paperId": "S1",
                            "title": "S2 title",
                            "externalIds": {"PubMed": "222"},
                            "authors": [],
                            "citationCount": 3,
                        }
                    ],
                },
            )
        assert request.headers["x-els-apikey"] == "scopus-key"
        return httpx.Response(
            200,
            json={
                "search-results": {
                    "opensearch:totalResults": "1",
                    "entry": [
                        {
                            "dc:identifier": "SCOPUS_ID:3",
                            "dc:title": "Scopus title",
                            "prism:doi": "10.2/test",
                            "citedby-count": "2",
                        }
                    ],
                }
            },
        )

    credentials = Credentials(
        semantic_scholar_api_key="s2-key", scopus_api_key="scopus-key"
    )
    async with HttpSession(
        intervals={"openalex": 0, "semantic-scholar": 0, "scopus": 0},
        transport=httpx.MockTransport(handler),
    ) as session:
        openalex = OpenAlexProvider(session, credentials)
        openalex_page = await openalex.fetch_page(strategy_for("openalex"), None, 10)
        assert openalex_page.records[0]["source_id"] == "W1"
        assert openalex_page.records[0]["abstract"] == "Abstract text"

        semantic = SemanticScholarProvider(session, credentials)
        semantic_page = await semantic.fetch_page(
            strategy_for("semantic-scholar"), None, 10
        )
        assert semantic_page.records[0]["pmid"] == "222"
        assert semantic_page.next_cursor == 1

        scopus = ScopusProvider(session, credentials)
        scopus_page = await scopus.fetch_page(strategy_for("scopus"), None, 10)
        assert scopus_page.records[0]["source_id"] == "3"
