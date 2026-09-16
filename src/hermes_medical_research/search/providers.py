from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

from .config import Credentials
from .http import HttpSession, SourceError
from .models import Question, SourceStrategy
from .parsers import (
    normalize_external_id,
    parse_pmc_xml,
    parse_pubmed_xml,
    reconstruct_abstract,
)

NCBI_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
OPENALEX_URL = "https://api.openalex.org/works"
S2_BULK_URL = "https://api.semanticscholar.org/graph/v1/paper/search/bulk"
S2_RELEVANCE_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
SCOPUS_URL = "https://api.elsevier.com/content/search/scopus"
# Minimum Jaccard token overlap between a candidate and the descriptor esearch matched it to.
# Exact entry terms score 1.0 and genuine synonyms stay well above this; the floor exists to
# reject a descriptor that shares almost no vocabulary with what the user asked for.
MESH_MATCH_THRESHOLD = 0.34
# The Scopus STANDARD view rejects `start` beyond this, so unbounded retrieval must stop here
# rather than fail with an opaque HTTP error partway through a run.
SCOPUS_MAX_START = 5000


@dataclass(slots=True)
class Page:
    records: list[dict[str, Any]]
    next_cursor: str | int | None
    total: int | None


class Provider:
    source: str
    page_size: int = 100

    def __init__(self, session: HttpSession, credentials: Credentials) -> None:
        self.session = session
        self.credentials = credentials

    async def count(self, strategy: SourceStrategy) -> int:
        raise NotImplementedError

    async def fetch_page(
        self, strategy: SourceStrategy, cursor: str | int | None, page_size: int
    ) -> Page:
        raise NotImplementedError


class NCBIProvider(Provider):
    page_size = 200

    def __init__(self, session: HttpSession, credentials: Credentials, *, database: str) -> None:
        super().__init__(session, credentials)
        self.database = database
        self.source = database

    def _params(self, **values: Any) -> dict[str, Any]:
        if not self.credentials.ncbi_email:
            raise SourceError(f"{self.source} requires NCBI_EMAIL")
        params = {
            "db": self.database,
            "retmode": "json",
            "tool": "hermes-medical-research",
            "email": self.credentials.ncbi_email,
            **values,
        }
        if self.credentials.ncbi_api_key:
            params["api_key"] = self.credentials.ncbi_api_key
        return params

    async def count(self, strategy: SourceStrategy) -> int:
        data = await self.session.json(
            "ncbi",
            f"{NCBI_BASE}/esearch.fcgi",
            params=self._params(term=strategy.selected_query, retmax=0),
        )
        try:
            return int(data["esearchresult"]["count"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceError(f"{self.source} returned an invalid count response") from exc

    async def fetch_page(
        self, strategy: SourceStrategy, cursor: str | int | None, page_size: int
    ) -> Page:
        start = int(cursor or 0)
        size = min(page_size, self.page_size)
        data = await self.session.json(
            "ncbi",
            f"{NCBI_BASE}/esearch.fcgi",
            params=self._params(term=strategy.selected_query, retstart=start, retmax=size),
        )
        result = data.get("esearchresult") or {}
        ids = [str(value) for value in result.get("idlist") or []]
        try:
            total = int(result.get("count", 0))
        except (TypeError, ValueError):
            total = None
        if not ids:
            return Page(records=[], next_cursor=None, total=total)
        xml = await self.session.text(
            "ncbi",
            f"{NCBI_BASE}/efetch.fcgi",
            params=self._params(id=",".join(ids), retmode="xml"),
        )
        try:
            records = (
                parse_pubmed_xml(xml, start_rank=start + 1)
                if self.database == "pubmed"
                else parse_pmc_xml(xml, start_rank=start + 1)
            )
        except ET.ParseError as exc:
            raise SourceError(f"{self.source} returned invalid XML") from exc
        next_start = start + len(ids)
        return Page(
            records=records,
            next_cursor=next_start if total is None or next_start < total else None,
            total=total,
        )


class OpenAlexProvider(Provider):
    source = "openalex"
    page_size = 100

    def _params(self, strategy: SourceStrategy, *, cursor: str, per_page: int) -> dict[str, Any]:
        params = dict(strategy.request_parameters)
        params.update({"search": strategy.selected_query, "cursor": cursor, "per-page": per_page})
        if self.credentials.openalex_api_key:
            params["api_key"] = self.credentials.openalex_api_key
        elif self.credentials.ncbi_email:
            params["mailto"] = self.credentials.ncbi_email
        return params

    async def count(self, strategy: SourceStrategy) -> int:
        data = await self.session.json(
            self.source,
            OPENALEX_URL,
            params=self._params(strategy, cursor="*", per_page=1),
        )
        try:
            return int((data.get("meta") or {})["count"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceError("openalex returned an invalid count response") from exc

    async def fetch_page(
        self, strategy: SourceStrategy, cursor: str | int | None, page_size: int
    ) -> Page:
        token = str(cursor or "*")
        data = await self.session.json(
            self.source,
            OPENALEX_URL,
            params=self._params(strategy, cursor=token, per_page=min(page_size, self.page_size)),
        )
        meta = data.get("meta") or {}
        try:
            total = int(meta.get("count", 0))
        except (TypeError, ValueError):
            total = None
        results = data.get("results") or []
        records = [self._record(item) for item in results if isinstance(item, dict)]
        next_cursor = meta.get("next_cursor") if records else None
        return Page(records=records, next_cursor=next_cursor, total=total)

    @staticmethod
    def _record(item: dict[str, Any]) -> dict[str, Any]:
        ids = item.get("ids") or {}
        primary = item.get("primary_location") or {}
        source = primary.get("source") or {}
        authors = [
            str((entry.get("author") or {}).get("display_name"))
            for entry in item.get("authorships") or []
            if (entry.get("author") or {}).get("display_name")
        ]
        publication_date = item.get("publication_date")
        return {
            "source": "openalex",
            "source_id": normalize_external_id(item.get("id"), "openalex") or "",
            "title": item.get("display_name") or item.get("title") or "",
            "abstract": reconstruct_abstract(item.get("abstract_inverted_index")),
            "authors": authors,
            "journal": source.get("display_name"),
            "publication_date": publication_date,
            "year": str(item.get("publication_year")) if item.get("publication_year") else None,
            "doi": normalize_external_id(ids.get("doi") or item.get("doi"), "doi"),
            "pmid": normalize_external_id(ids.get("pmid"), "pubmed"),
            "pmcid": normalize_external_id(ids.get("pmcid"), "pmc"),
            "citation_count": int(item.get("cited_by_count") or 0),
            "url": primary.get("landing_page_url") or item.get("id"),
            "publication_types": [str(item.get("type"))] if item.get("type") else [],
            "mesh_terms": [],
            "language": item.get("language"),
        }


class SemanticScholarProvider(Provider):
    source = "semantic-scholar"
    page_size = 1000
    fields = (
        "paperId,title,abstract,authors,venue,year,publicationDate,externalIds,"
        "citationCount,url,publicationTypes,journal"
    )

    def _headers(self) -> dict[str, str]:
        return (
            {"x-api-key": self.credentials.semantic_scholar_api_key}
            if self.credentials.semantic_scholar_api_key
            else {}
        )

    def _params(
        self,
        strategy: SourceStrategy,
        *,
        limit: int,
        cursor: str | int | None = None,
    ) -> dict[str, Any]:
        params = dict(strategy.request_parameters)
        endpoint = params.pop("endpoint", "relevance")
        params.update({"query": strategy.selected_query, "limit": limit, "fields": self.fields})
        if cursor is not None:
            params["token" if endpoint == "bulk" else "offset"] = cursor
        return params

    @staticmethod
    def _url(strategy: SourceStrategy) -> str:
        return (
            S2_BULK_URL
            if strategy.request_parameters.get("endpoint") == "bulk"
            else S2_RELEVANCE_URL
        )

    async def count(self, strategy: SourceStrategy) -> int:
        data = await self.session.json(
            self.source,
            self._url(strategy),
            params=self._params(strategy, limit=1),
            headers=self._headers(),
        )
        try:
            return int(data.get("total", 0))
        except (TypeError, ValueError) as exc:
            raise SourceError("semantic-scholar returned an invalid count response") from exc

    async def fetch_page(
        self, strategy: SourceStrategy, cursor: str | int | None, page_size: int
    ) -> Page:
        bulk = strategy.request_parameters.get("endpoint") == "bulk"
        limit = min(page_size, self.page_size if bulk else 100)
        data = await self.session.json(
            self.source,
            self._url(strategy),
            params=self._params(strategy, limit=limit, cursor=cursor),
            headers=self._headers(),
        )
        try:
            total = int(data.get("total", 0))
        except (TypeError, ValueError):
            total = None
        records = [self._record(item) for item in data.get("data") or [] if isinstance(item, dict)]
        return Page(
            records=records,
            next_cursor=(data.get("token") if bulk else data.get("next")) if records else None,
            total=total,
        )

    @staticmethod
    def _record(item: dict[str, Any]) -> dict[str, Any]:
        ids = item.get("externalIds") or {}
        journal = item.get("journal") or {}
        return {
            "source": "semantic-scholar",
            "source_id": str(item.get("paperId") or ""),
            "title": item.get("title") or "",
            "abstract": item.get("abstract"),
            "authors": [
                str(author.get("name"))
                for author in item.get("authors") or []
                if isinstance(author, dict) and author.get("name")
            ],
            "journal": journal.get("name") or item.get("venue"),
            "publication_date": item.get("publicationDate"),
            "year": str(item.get("year")) if item.get("year") else None,
            "doi": normalize_external_id(ids.get("DOI"), "doi"),
            "pmid": normalize_external_id(ids.get("PubMed"), "pubmed"),
            "pmcid": normalize_external_id(ids.get("PubMedCentral"), "pmc"),
            "citation_count": int(item.get("citationCount") or 0),
            "url": item.get("url"),
            "publication_types": [str(value) for value in item.get("publicationTypes") or []],
            "mesh_terms": [],
            "language": None,
        }


class ScopusProvider(Provider):
    source = "scopus"
    page_size = 25

    def _headers(self) -> dict[str, str]:
        if not self.credentials.scopus_api_key:
            raise SourceError("scopus requires SCOPUS_API_KEY")
        headers = {"X-ELS-APIKey": self.credentials.scopus_api_key, "Accept": "application/json"}
        if self.credentials.scopus_insttoken:
            headers["X-ELS-Insttoken"] = self.credentials.scopus_insttoken
        return headers

    async def count(self, strategy: SourceStrategy) -> int:
        data = await self.session.json(
            self.source,
            SCOPUS_URL,
            params={"query": strategy.selected_query, "count": 1, "start": 0, "view": "STANDARD"},
            headers=self._headers(),
        )
        try:
            return int((data.get("search-results") or {})["opensearch:totalResults"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceError("scopus returned an invalid count response") from exc

    async def fetch_page(
        self, strategy: SourceStrategy, cursor: str | int | None, page_size: int
    ) -> Page:
        start = int(cursor or 0)
        if start >= SCOPUS_MAX_START:
            # Stop cleanly at the API's ceiling instead of letting it 400 mid-run. Retrieval ends
            # short of the reported total, which the manifest already records as `truncated`.
            return Page(records=[], next_cursor=None, total=None)
        count = min(page_size, self.page_size, SCOPUS_MAX_START - start)
        data = await self.session.json(
            self.source,
            SCOPUS_URL,
            params={
                "query": strategy.selected_query,
                "count": count,
                "start": start,
                "view": "STANDARD",
            },
            headers=self._headers(),
        )
        result = data.get("search-results") or {}
        try:
            total = int(result.get("opensearch:totalResults", 0))
        except (TypeError, ValueError):
            total = None
        entries = [item for item in result.get("entry") or [] if isinstance(item, dict)]
        records = [self._record(item) for item in entries]
        next_start = start + len(entries)
        exhausted = (
            not entries
            or next_start >= SCOPUS_MAX_START
            or (total is not None and next_start >= total)
        )
        return Page(
            records=records,
            next_cursor=None if exhausted else next_start,
            total=total,
        )

    @staticmethod
    def _record(item: dict[str, Any]) -> dict[str, Any]:
        links = item.get("link") or []
        url = next(
            (
                link.get("@href")
                for link in links
                if isinstance(link, dict) and link.get("@ref") == "scopus"
            ),
            None,
        )
        identifier = str(item.get("dc:identifier") or item.get("eid") or "")
        return {
            "source": "scopus",
            "source_id": identifier.removeprefix("SCOPUS_ID:"),
            "title": item.get("dc:title") or "",
            "abstract": item.get("dc:description"),
            "authors": [str(item.get("dc:creator"))] if item.get("dc:creator") else [],
            "journal": item.get("prism:publicationName"),
            "publication_date": item.get("prism:coverDate"),
            "year": str(item.get("prism:coverDate", ""))[:4] or None,
            "doi": normalize_external_id(item.get("prism:doi"), "doi"),
            "pmid": None,
            "pmcid": None,
            "citation_count": int(item.get("citedby-count") or 0),
            "url": url,
            "publication_types": [str(item.get("subtypeDescription"))]
            if item.get("subtypeDescription")
            else [],
            "mesh_terms": [],
            "language": None,
        }


class MeshResolver:
    def __init__(self, session: HttpSession, credentials: Credentials) -> None:
        self.session = session
        self.credentials = credentials

    async def resolve_question(self, question: Question) -> list[str]:
        warnings: list[str] = []
        if not self.credentials.ncbi_email:
            warnings.append("MeSH resolution skipped because NCBI_EMAIL is not configured.")
            return warnings
        for name, block in question.components.items():
            for group in block.groups:
                candidates = list(dict.fromkeys([*group.candidate_mesh, group.text]))[:6]
                for candidate in candidates:
                    try:
                        heading = await self.resolve(candidate)
                    except SourceError as exc:
                        warnings.append(
                            f"MeSH resolution failed for {name}/{group.label}/{candidate}: {exc}"
                        )
                        continue
                    if heading and heading.casefold() not in {
                        value.casefold() for value in group.resolved_mesh
                    }:
                        group.resolved_mesh.append(heading)
                        if candidate not in group.candidate_mesh:
                            # group.text is resolved too, so a heading can enter the PubMed query
                            # (and explode down the MeSH tree) that the user never proposed.
                            warnings.append(
                                f"MeSH heading {heading!r} was derived from the canonical text of "
                                f"{name}/{group.label} and added to the query; remove it by "
                                "re-planning with --no-mesh if it is too broad."
                            )
                    elif not heading and candidate in group.candidate_mesh:
                        warnings.append(
                            "Candidate MeSH heading was not validated for "
                            f"{name}/{group.label}: {candidate}"
                        )
        return warnings

    async def resolve(self, candidate: str) -> str | None:
        params = {
            "db": "mesh",
            "term": f'"{candidate}"[MeSH Terms]',
            "retmode": "json",
            "retmax": 3,
            "tool": "hermes-medical-research",
            "email": self.credentials.ncbi_email,
        }
        if self.credentials.ncbi_api_key:
            params["api_key"] = self.credentials.ncbi_api_key
        data = await self.session.json("ncbi", f"{NCBI_BASE}/esearch.fcgi", params=params)
        ids = list((data.get("esearchresult") or {}).get("idlist") or [])
        if not ids:
            return None
        summary_params = dict(params)
        summary_params.pop("term", None)
        summary_params.pop("retmax", None)
        summary_params["id"] = ",".join(str(value) for value in ids)
        # efetch ignores retmode=xml for db=mesh and returns a plain-text MeSH record, so headings
        # are read from esummary's JSON instead.
        summary = await self.session.json(
            "ncbi", f"{NCBI_BASE}/esummary.fcgi", params=summary_params
        )
        result = summary.get("result") or {}
        normalized = _tokens(candidate)
        best_heading: str | None = None
        best_score = 0.0
        for uid in result.get("uids") or []:
            entry = result.get(str(uid))
            if not isinstance(entry, dict):
                continue
            # ds_meshterms[0] is the descriptor; the rest are entry terms, so an exact synonym
            # such as "NIDDM" still resolves to "Diabetes Mellitus, Type 2".
            terms = [str(term) for term in entry.get("ds_meshterms") or [] if term]
            if not terms:
                continue
            score = max(_overlap(normalized, _tokens(term)) for term in terms)
            if score > best_score:
                best_heading, best_score = terms[0], score
        # esearch matches entry terms as well as descriptors, so the best hit can still be a
        # different concept. Require real token overlap rather than accepting any hit.
        return best_heading if best_score >= MESH_MATCH_THRESHOLD else None


def provider_for(source: str, session: HttpSession, credentials: Credentials) -> Provider:
    if source in {"europe-pmc", "clinicaltrials"}:
        from .biomedical import ClinicalTrialsProvider, EuropePMCProvider

        cls = EuropePMCProvider if source == "europe-pmc" else ClinicalTrialsProvider
        return cls(session, credentials)
    if source in {"pubmed", "pmc"}:
        return NCBIProvider(session, credentials, database=source)
    if source == "openalex":
        return OpenAlexProvider(session, credentials)
    if source == "semantic-scholar":
        return SemanticScholarProvider(session, credentials)
    if source == "scopus":
        return ScopusProvider(session, credentials)
    raise ValueError(f"unsupported source {source}")


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.casefold()))


def _overlap(left: set[str], right: set[str]) -> float:
    return len(left & right) / max(len(left | right), 1)
