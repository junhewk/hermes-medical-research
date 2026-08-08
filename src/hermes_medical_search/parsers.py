from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any


def xml_text(node: ET.Element | None) -> str | None:
    if node is None:
        return None
    value = "".join(node.itertext())
    cleaned = re.sub(r"\s+", " ", value).strip()
    return cleaned or None


def parse_pubmed_xml(xml: str, *, start_rank: int = 1) -> list[dict[str, Any]]:
    root = ET.fromstring(xml)
    records: list[dict[str, Any]] = []
    for index, article in enumerate(root.findall(".//PubmedArticle"), start=start_rank):
        medline = article.find("MedlineCitation")
        detail = medline.find("Article") if medline is not None else None
        ids = {
            str(node.attrib.get("IdType", "")).lower(): xml_text(node)
            for node in article.findall(".//ArticleIdList/ArticleId")
        }
        pmid = xml_text(medline.find("PMID")) if medline is not None else ids.get("pubmed")
        abstract_parts = [xml_text(node) for node in article.findall(".//Abstract/AbstractText")]
        publication_types = [
            value
            for node in article.findall(".//PublicationTypeList/PublicationType")
            if (value := xml_text(node))
        ]
        mesh_terms = [
            value
            for node in article.findall(".//MeshHeading/DescriptorName")
            if (value := xml_text(node))
        ]
        journal = xml_text(detail.find(".//Journal/Title")) if detail is not None else None
        publication_date = _pubmed_date(detail) if detail is not None else None
        records.append(
            {
                "source": "pubmed",
                "source_id": pmid or ids.get("doi") or f"pubmed-rank-{index}",
                "source_rank": index,
                "title": xml_text(detail.find("ArticleTitle")) if detail is not None else "",
                "abstract": " ".join(part for part in abstract_parts if part) or None,
                "authors": _pubmed_authors(detail),
                "journal": journal,
                "publication_date": publication_date,
                "year": publication_date[:4] if publication_date else None,
                "doi": _normalize_doi(ids.get("doi")),
                "pmid": pmid,
                "pmcid": _normalize_pmcid(ids.get("pmc")),
                "citation_count": 0,
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else None,
                "publication_types": publication_types,
                "mesh_terms": mesh_terms,
                "language": xml_text(medline.find(".//Language")) if medline is not None else None,
            }
        )
    return records


def parse_pmc_xml(xml: str, *, start_rank: int = 1) -> list[dict[str, Any]]:
    root = ET.fromstring(xml)
    records: list[dict[str, Any]] = []
    articles = [root] if root.tag.rsplit("}", 1)[-1] == "article" else root.findall(".//article")
    for index, article in enumerate(articles, start=start_rank):
        ids = {
            str(node.attrib.get("pub-id-type", "")).lower(): xml_text(node)
            for node in article.findall(".//article-id")
        }
        pmcid = _normalize_pmcid(ids.get("pmc") or ids.get("pmcid"))
        publication_date = _pmc_date(article)
        abstract_parts = [xml_text(node) for node in article.findall(".//abstract")]
        authors = []
        for contributor in article.findall(".//contrib[@contrib-type='author']"):
            collective = xml_text(contributor.find("collab"))
            surname = xml_text(contributor.find(".//surname"))
            given = xml_text(contributor.find(".//given-names"))
            name = collective or " ".join(part for part in (given, surname) if part)
            if name:
                authors.append(name)
        article_types = [
            value
            for node in article.findall(".//article-categories//subject")
            if (value := xml_text(node))
        ]
        records.append(
            {
                "source": "pmc",
                "source_id": pmcid or ids.get("pmid") or ids.get("doi") or f"pmc-rank-{index}",
                "source_rank": index,
                "title": xml_text(article.find(".//article-title")) or "",
                "abstract": " ".join(part for part in abstract_parts if part) or None,
                "authors": authors,
                "journal": xml_text(article.find(".//journal-title")),
                "publication_date": publication_date,
                "year": publication_date[:4] if publication_date else None,
                "doi": _normalize_doi(ids.get("doi")),
                "pmid": ids.get("pmid"),
                "pmcid": pmcid,
                "citation_count": 0,
                "url": f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/" if pmcid else None,
                "publication_types": article_types,
                "mesh_terms": [],
                "language": article.attrib.get("{http://www.w3.org/XML/1998/namespace}lang"),
            }
        )
    return records


def reconstruct_abstract(index: Any) -> str | None:
    if not isinstance(index, dict):
        return None
    positions: list[tuple[int, str]] = []
    for word, offsets in index.items():
        if not isinstance(offsets, list):
            continue
        for offset in offsets:
            if isinstance(offset, int):
                positions.append((offset, str(word)))
    if not positions:
        return None
    positions.sort()
    return " ".join(word for _, word in positions)


def normalize_external_id(value: Any, prefix: str) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    lowered = text.lower()
    if prefix.casefold() == "doi":
        text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.I)
    elif prefix.casefold() == "openalex":
        text = re.sub(r"^https?://openalex\.org/", "", text, flags=re.I)
    elif prefix.casefold() == "pubmed":
        text = re.sub(r"^https?://pubmed\.ncbi\.nlm\.nih\.gov/", "", text, flags=re.I)
    elif prefix.casefold() == "pmc":
        text = re.sub(
            r"^https?://pmc\.ncbi\.nlm\.nih\.gov/articles/", "", text, flags=re.I
        )
    lowered = text.lower()
    marker = f"{prefix.lower()}:"
    if marker in lowered:
        text = text[lowered.rfind(marker) + len(marker) :]
    text = text.strip().rstrip("/")
    if prefix.casefold() == "pmc" and text and not text.upper().startswith("PMC"):
        text = f"PMC{text}"
    if prefix.casefold() == "doi":
        text = text.lower()
    return text or None


def _pubmed_authors(detail: ET.Element | None) -> list[str]:
    if detail is None:
        return []
    authors: list[str] = []
    for author in detail.findall(".//AuthorList/Author"):
        collective = xml_text(author.find("CollectiveName"))
        surname = xml_text(author.find("LastName"))
        given = xml_text(author.find("ForeName")) or xml_text(author.find("Initials"))
        name = collective or " ".join(part for part in (given, surname) if part)
        if name:
            authors.append(name)
    return authors


def _pubmed_date(detail: ET.Element) -> str | None:
    node = detail.find(".//ArticleDate")
    if node is None:
        node = detail.find(".//JournalIssue/PubDate")
    if node is None:
        return None
    year = xml_text(node.find("Year"))
    month = _month(xml_text(node.find("Month")))
    day = xml_text(node.find("Day"))
    medline = xml_text(node.find("MedlineDate"))
    if year:
        return "-".join(part for part in (year, month, day) if part)
    match = re.search(r"\b(19|20)\d{2}\b", medline or "")
    return match.group(0) if match else None


def _pmc_date(article: ET.Element) -> str | None:
    preferred = article.find(".//pub-date[@pub-type='epub']")
    if preferred is None:
        preferred = article.find(".//pub-date")
    if preferred is None:
        return None
    year = xml_text(preferred.find("year"))
    month = _month(xml_text(preferred.find("month")))
    day = xml_text(preferred.find("day"))
    if day and day.isdigit():
        day = f"{int(day):02d}"
    return "-".join(part for part in (year, month, day) if part) if year else None


def _month(value: str | None) -> str | None:
    if not value:
        return None
    months = {
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "may": 5,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }
    try:
        number = int(value)
    except ValueError:
        number = months.get(value[:3].lower(), 0)
    return f"{number:02d}" if 1 <= number <= 12 else None


def _normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    return re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value.strip(), flags=re.I).lower()


def _normalize_pmcid(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip().upper()
    return cleaned if cleaned.startswith("PMC") else f"PMC{cleaned}"
