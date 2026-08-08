from __future__ import annotations

import math
import re
from copy import deepcopy
from datetime import UTC, date, datetime
from typing import Any

from .models import ConceptBlock, Question

RANKING_VERSION = "mdr-v1-generalized"
STOP_WORDS = {
    "about",
    "adult",
    "adults",
    "among",
    "and",
    "are",
    "care",
    "clinical",
    "effect",
    "effects",
    "for",
    "from",
    "health",
    "medical",
    "patient",
    "patients",
    "people",
    "study",
    "that",
    "the",
    "their",
    "this",
    "using",
    "versus",
    "with",
}


def deduplicate(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prepared = [_prepare(record) for record in records]
    parents = list(range(len(prepared)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[max(left_root, right_root)] = min(left_root, right_root)

    owners: dict[str, int] = {}
    for index, record in enumerate(prepared):
        for key in _strong_keys(record):
            if key in owners:
                union(index, owners[key])
            else:
                owners[key] = index

    titles: dict[str, list[int]] = {}
    for index, record in enumerate(prepared):
        title_key = _title_year_key(record)
        if title_key:
            titles.setdefault(title_key, []).append(index)
    for indices in titles.values():
        roots = list(dict.fromkeys(find(index) for index in indices))
        strong_roots = [
            root
            for root in roots
            if any(
                _strong_keys(prepared[index])
                for index in indices
                if find(index) == root
            )
        ]
        weak_roots = [root for root in roots if root not in strong_roots]
        if len(strong_roots) == 1:
            for root in weak_roots:
                union(strong_roots[0], root)
        elif not strong_roots and weak_roots:
            for root in weak_roots[1:]:
                union(weak_roots[0], root)

    grouped: dict[int, list[int]] = {}
    for index in range(len(prepared)):
        grouped.setdefault(find(index), []).append(index)
    canonical: list[dict[str, Any]] = []
    for _, indices in sorted(grouped.items(), key=lambda item: min(item[1])):
        merged = prepared[indices[0]]
        for index in indices[1:]:
            merged = _merge(merged, prepared[index])
        canonical.append(merged)
    for index, record in enumerate(canonical, start=1):
        record["canonical_id"] = _canonical_id(record, index)
        record["native_merge_order"] = index
    return canonical


def rank_records(
    records: list[dict[str, Any]], question: Question, *, today: date | None = None
) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    for record in records:
        evidence_level, evidence = evidence_score(record)
        citation = citation_score(record.get("citation_count"))
        half_life = 3 if question.framework == "PICO" else 5
        recency = recency_score(
            record.get("publication_date") or record.get("year"),
            half_life_years=half_life,
            today=today,
        )
        relevance = relevance_score(record, question)
        if question.framework == "PICO":
            base = evidence * (0.30 / 0.85) + citation * (0.15 / 0.85) + recency * (
                0.40 / 0.85
            )
            profile = "pico-clinical-no-journal-bonus"
        else:
            base = evidence * 0.40 + citation * 0.30 + recency * 0.30
            profile = "pcc-general"
        composite = base * 0.55 + relevance * 0.45
        item = deepcopy(record)
        item["ranking"] = {
            "version": RANKING_VERSION,
            "profile": profile,
            "evidence_category": evidence_level,
            "evidence_score": round(evidence, 6),
            "citation_score": round(citation, 6),
            "recency_score": round(recency, 6),
            "relevance_score": round(relevance, 6),
            "base_score": round(base, 6),
            "composite_score": round(composite, 6),
            "disclaimer": (
                "Prioritization heuristic only; not GRADE, risk-of-bias, evidence quality, "
                "or a systematic-review conclusion."
            ),
        }
        ranked.append(item)
    ranked.sort(
        key=lambda item: (
            -item["ranking"]["composite_score"],
            str(item.get("title") or "").casefold(),
            str(item.get("canonical_id") or ""),
        )
    )
    for position, item in enumerate(ranked, start=1):
        item["ranking"]["rank"] = position
    return ranked


def evidence_score(record: dict[str, Any]) -> tuple[str, float]:
    text = " ".join(
        [
            *[str(value) for value in record.get("publication_types") or []],
            str(record.get("title") or ""),
            str(record.get("abstract") or ""),
        ]
    ).casefold()
    patterns = (
        ("I", 1.0, ("systematic review", "meta-analysis", "meta analysis")),
        (
            "II",
            0.8,
            (
                "randomized controlled trial",
                "randomised controlled trial",
                "randomized trial",
                "randomised trial",
                "clinical trial",
            ),
        ),
        (
            "III",
            0.6,
            ("cohort", "case-control", "case control", "prospective", "retrospective"),
        ),
        ("IV", 0.4, ("case report", "case series", "cross-sectional", "cross sectional")),
        ("V", 0.2, ("editorial", "expert opinion", "commentary", "letter")),
    )
    for label, score, needles in patterns:
        if any(needle in text for needle in needles):
            return label, score
    return "unknown", 0.3


def citation_score(value: Any) -> float:
    try:
        count = int(value or 0)
    except (TypeError, ValueError):
        count = 0
    return min(math.log(count + 1) / math.log(1000), 1.0) if count > 0 else 0.0


def recency_score(
    value: Any, *, half_life_years: int, today: date | None = None
) -> float:
    parsed = _parse_date(value)
    if parsed is None:
        return 0.5
    current = today or datetime.now(UTC).date()
    years_old = max((current - parsed).days / 365.25, 0.0)
    return max(pow(0.5, years_old / half_life_years), 0.1)


def relevance_score(record: dict[str, Any], question: Question) -> float:
    haystack = " ".join(
        [
            str(record.get("title") or ""),
            str(record.get("abstract") or ""),
            str(record.get("journal") or ""),
            *[str(value) for value in record.get("mesh_terms") or []],
            *[str(value) for value in record.get("publication_types") or []],
        ]
    ).casefold()
    tokens = _tokens(haystack)
    all_terms = [term for block in question.components.values() for term in _block_terms(block)]
    query_score = _terms_score(all_terms, haystack, tokens)
    population = _terms_score(_block_terms(question.components["population"]), haystack, tokens)
    if question.framework == "PICO":
        focus = _terms_score(
            _block_terms(question.components["intervention"]), haystack, tokens
        )
        optional_terms = [
            term
            for name in ("comparison", "outcome")
            if name in question.components
            for term in _block_terms(question.components[name])
        ]
        optional = _terms_score(optional_terms, haystack, tokens) if optional_terms else 0.5
        relevance = focus * 0.50 + population * 0.20 + optional * 0.10 + query_score * 0.20
        if focus == 0.0:
            relevance = min(relevance, 0.28)
    else:
        focus = _terms_score(_block_terms(question.components["concept"]), haystack, tokens)
        context = (
            _terms_score(_block_terms(question.components["context"]), haystack, tokens)
            if "context" in question.components
            else 0.5
        )
        relevance = focus * 0.40 + context * 0.35 + population * 0.10 + query_score * 0.15
        if focus == 0.0:
            relevance = min(relevance, 0.28)
    if population == 0.0:
        relevance = min(relevance, 0.60)
    return max(0.0, min(relevance, 1.0))


def _prepare(record: dict[str, Any]) -> dict[str, Any]:
    item = deepcopy(record)
    item["doi"] = _doi(item.get("doi"))
    item["pmid"] = _digits(item.get("pmid"))
    item["pmcid"] = _pmcid(item.get("pmcid"))
    item["source_records"] = [
        {
            "source": item.get("source"),
            "source_id": item.get("source_id"),
            "source_rank": item.get("source_rank"),
            "url": item.get("url"),
        }
    ]
    item["sources"] = [item.get("source")]
    return item


def _merge(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(left)
    result["source_records"] = [*left.get("source_records", []), *right.get("source_records", [])]
    result["sources"] = list(dict.fromkeys([*left.get("sources", []), *right.get("sources", [])]))
    for name in ("doi", "pmid", "pmcid", "publication_date", "year", "journal", "url", "language"):
        if not result.get(name) and right.get(name):
            result[name] = right[name]
    for name in ("abstract", "title"):
        if len(str(right.get(name) or "")) > len(str(result.get(name) or "")):
            result[name] = right[name]
    if len(right.get("authors") or []) > len(result.get("authors") or []):
        result["authors"] = right["authors"]
    result["citation_count"] = max(
        int(result.get("citation_count") or 0), int(right.get("citation_count") or 0)
    )
    for name in ("publication_types", "mesh_terms"):
        result[name] = list(dict.fromkeys([*(result.get(name) or []), *(right.get(name) or [])]))
    return result


def _strong_keys(record: dict[str, Any]) -> list[str]:
    return [
        key
        for key in (
            f"doi:{record.get('doi')}" if record.get("doi") else None,
            f"pmid:{record.get('pmid')}" if record.get("pmid") else None,
            f"pmcid:{record.get('pmcid')}" if record.get("pmcid") else None,
        )
        if key
    ]


def _title_year_key(record: dict[str, Any]) -> str:
    title = re.sub(r"[^a-z0-9]+", " ", str(record.get("title") or "").casefold()).strip()
    return f"{title}:{record.get('year') or ''}" if title else ""


def _canonical_id(record: dict[str, Any], index: int) -> str:
    if record.get("doi"):
        return f"doi:{record['doi']}"
    if record.get("pmid"):
        return f"pmid:{record['pmid']}"
    if record.get("pmcid"):
        return f"pmcid:{record['pmcid']}"
    return f"record:{index}:{_title_year_key(record)}"


def _doi(value: Any) -> str | None:
    if not value:
        return None
    return re.sub(r"^https?://(?:dx\.)?doi\.org/", "", str(value).strip(), flags=re.I).lower()


def _digits(value: Any) -> str | None:
    match = re.search(r"\d+", str(value or ""))
    return match.group(0) if match else None


def _pmcid(value: Any) -> str | None:
    if not value:
        return None
    match = re.search(r"(?:PMC)?(\d+)", str(value), re.I)
    return f"PMC{match.group(1)}" if match else None


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", value.casefold())
        if len(token) >= 3 and token not in STOP_WORDS
    }


def _block_terms(block: ConceptBlock) -> list[str]:
    return list(dict.fromkeys([*block.free_terms(), *block.resolved_mesh]))


def _terms_score(terms: list[str], haystack: str, haystack_tokens: set[str]) -> float:
    if not terms:
        return 0.5
    scores: list[float] = []
    for term in terms:
        normalized = re.sub(r"\s+", " ", term.casefold()).strip()
        term_tokens = _tokens(normalized)
        if normalized and normalized in haystack:
            scores.append(1.0)
        elif term_tokens:
            scores.append(len(term_tokens & haystack_tokens) / len(term_tokens))
    return max(scores, default=0.0)


def _parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None
