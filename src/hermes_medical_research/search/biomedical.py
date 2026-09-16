"""Europe PMC and ClinicalTrials.gov adapters for the search paging contract."""

from __future__ import annotations

import re
from typing import Any

from .http import SourceError
from .models import SourceStrategy
from .providers import Page, Provider

EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
CTG = "https://clinicaltrials.gov/api/v2/studies"


def _count(value: Any, provider: str) -> int:
    try:
        result = int(value)
    except (ValueError, TypeError) as exc:
        raise SourceError(f"{provider} returned an invalid total") from exc
    if result < 0:
        raise SourceError(f"{provider} returned a negative total")
    return result


def europe_record(item: dict[str, Any]) -> dict[str, Any]:
    journal = (item.get("journalInfo") or {}).get("journal") or {}
    authors = (item.get("authorList") or {}).get("author") or []
    pmid = str(item.get("pmid") or (item.get("id") if item.get("source") == "MED" else "") or "")
    return {
        "source": "europe-pmc",
        "source_id": f"{item.get('source', 'MED')}:{item.get('id', '')}",
        "title": item.get("title") or "",
        "abstract": item.get("abstractText"),
        "authors": [a.get("fullName") or a.get("collectiveName") or "" for a in authors]
        or ([item["authorString"]] if item.get("authorString") else []),
        "journal": journal.get("title") or item.get("journalTitle"),
        "year": str(item.get("pubYear") or "") or None,
        "publication_date": item.get("firstPublicationDate"),
        "doi": item.get("doi"),
        "pmid": pmid or None,
        "pmcid": item.get("pmcid"),
        "url": f"https://europepmc.org/article/{item.get('source', 'MED')}/{item.get('id', '')}",
        "citation_count": int(item.get("citedByCount") or 0),
        "publication_types": (item.get("pubTypeList") or {}).get("pubType") or [],
        "mesh_terms": [
            m.get("descriptorName", "")
            for m in (item.get("meshHeadingList") or {}).get("meshHeading") or []
        ],
        "language": item.get("language"),
        "record_kind": "publication",
        "is_retracted": str(item.get("isRetracted", "N")).upper() == "Y",
    }


class EuropePMCProvider(Provider):
    source = "europe-pmc"
    page_size = 100

    async def count(self, strategy: SourceStrategy) -> int:
        if strategy.request_parameters.get("link_seed"):
            data = await self._links(strategy, 1, 1)
            return _count(data.get("hitCount"), self.source)
        data = await self.session.json(
            self.source,
            f"{EPMC}/search",
            params={
                "query": strategy.selected_query,
                "format": "json",
                "pageSize": 1,
            },
        )
        return _count(data.get("hitCount"), self.source)

    async def fetch_page(
        self, strategy: SourceStrategy, cursor: str | int | None, page_size: int
    ) -> Page:
        if strategy.request_parameters.get("link_seed"):
            page = int(cursor or 1)
            data = await self._links(strategy, page, min(page_size, self.page_size))
            direction = strategy.request_parameters["link_direction"]
            outer, inner = (
                ("referenceList", "reference")
                if direction == "references"
                else ("citationList", "citation")
            )
            items = (data.get(outer) or {}).get(inner) or []
            total = _count(data.get("hitCount"), self.source)
            records = [europe_record({"source": "MED", **item}) for item in items]
            for record in records:
                record["citation_chaining"] = dict(strategy.request_parameters)
            return Page(records, page + 1 if items and page * page_size < total else None, total)
        data = await self.session.json(
            self.source,
            f"{EPMC}/search",
            params={
                "query": strategy.selected_query,
                "format": "json",
                "resultType": "core",
                "cursorMark": str(cursor or "*"),
                "pageSize": min(page_size, self.page_size),
            },
        )
        items = (data.get("resultList") or {}).get("result")
        if not isinstance(items, list):
            raise SourceError("europe-pmc omitted its result list")
        next_cursor = data.get("nextCursorMark")
        if next_cursor == str(cursor or "*") or not items:
            next_cursor = None
        return Page(
            [europe_record(item) for item in items],
            next_cursor,
            _count(data.get("hitCount"), self.source),
        )

    async def _links(self, strategy: SourceStrategy, page: int, size: int) -> dict[str, Any]:
        seed = str(strategy.request_parameters["link_seed"])
        direction = strategy.request_parameters.get("link_direction")
        if not seed.isdigit() or direction not in {"references", "citations"}:
            raise SourceError("invalid Europe PMC citation traversal")
        return await self.session.json(
            self.source, f"{EPMC}/MED/{seed}/{direction}/{page}/{size}/json"
        )


def trial_record(item: dict[str, Any]) -> dict[str, Any]:
    protocol = item.get("protocolSection") or {}
    identity = protocol.get("identificationModule") or {}
    status = protocol.get("statusModule") or {}
    descriptions = protocol.get("descriptionModule") or {}
    design = protocol.get("designModule") or {}
    refs = (protocol.get("referencesModule") or {}).get("references") or []
    nct = identity.get("nctId") or ""
    if not re.fullmatch(r"NCT\d{8}", nct):
        raise SourceError("clinicaltrials returned an invalid NCT identifier")
    return {
        "source": "clinicaltrials",
        "source_id": nct,
        "nct_id": nct,
        "record_kind": "registration",
        "trial_ids": [nct],
        "title": identity.get("officialTitle") or identity.get("briefTitle") or "",
        "abstract": descriptions.get("briefSummary"),
        "authors": [],
        "journal": None,
        "doi": None,
        "pmid": None,
        "pmcid": None,
        "publication_date": None,
        "year": None,
        "language": None,
        "publication_types": ["Trial registration"],
        "mesh_terms": [],
        "citation_count": 0,
        "url": f"https://clinicaltrials.gov/study/{nct}",
        "trial_status": status.get("overallStatus"),
        "study_type": design.get("studyType"),
        "results_posted": bool(item.get("hasResults")),
        "related_pmids": [str(ref["pmid"]) for ref in refs if ref.get("pmid")],
        "registry_data": item,
    }


class ClinicalTrialsProvider(Provider):
    source = "clinicaltrials"
    page_size = 100

    async def count(self, strategy: SourceStrategy) -> int:
        data = await self.session.json(
            self.source,
            CTG,
            params={
                "query.term": strategy.selected_query,
                "countTotal": "true",
                "pageSize": 1,
            },
        )
        return _count(data.get("totalCount"), self.source)

    async def fetch_page(
        self, strategy: SourceStrategy, cursor: str | int | None, page_size: int
    ) -> Page:
        params = {
            "query.term": strategy.selected_query,
            "countTotal": "true",
            "pageSize": min(page_size, self.page_size),
            "format": "json",
        }
        if cursor:
            params["pageToken"] = str(cursor)
        data = await self.session.json(self.source, CTG, params=params)
        items = data.get("studies")
        if not isinstance(items, list):
            raise SourceError("clinicaltrials omitted its study list")
        return Page(
            [trial_record(item) for item in items],
            data.get("nextPageToken"),
            _count(data.get("totalCount"), self.source),
        )
