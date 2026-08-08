from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from . import __version__
from .models import CORE_SOURCES, Question, SourceStrategy, Strategy


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _quoted(value: str) -> str:
    return f'"{_escape(value)}"'


def _or_group(parts: list[str]) -> str:
    unique = list(dict.fromkeys(part for part in parts if part))
    if not unique:
        return ""
    return unique[0] if len(unique) == 1 else f"({' OR '.join(unique)})"


def _pubmed_block(question: Question, name: str) -> str:
    block = question.components[name]
    mesh = [f'{_quoted(term)}[Mesh]' for term in block.resolved_mesh]
    free = [f'{_quoted(term)}[tiab]' for term in block.free_terms()]
    return _or_group([*mesh, *free])


def _scopus_block(question: Question, name: str) -> str:
    block = question.components[name]
    return _or_group([_quoted(term) for term in block.free_terms()])


def _plain_block(question: Question, name: str) -> str:
    block = question.components[name]
    return _or_group([_quoted(term) for term in block.free_terms()])


def _s2_block(question: Question, name: str) -> str:
    block = question.components[name]
    terms = list(dict.fromkeys(_quoted(term) for term in block.free_terms()))
    if not terms:
        return ""
    return terms[0] if len(terms) == 1 else f"({' | '.join(terms)})"


def _blocks(question: Question) -> tuple[list[str], list[str]]:
    if question.framework == "PICO":
        return ["population", "intervention"], [
            key for key in ("comparison", "outcome") if key in question.components
        ]
    return ["population", "concept"], [
        key for key in ("context",) if key in question.components
    ]


def _date_clause(question: Question) -> str | None:
    start = question.filters.from_date
    end = question.filters.to_date
    if not start and not end:
        return None
    earliest = start or "1000/01/01"
    latest = end or "3000/12/31"
    return f'"{earliest}"[Date - Publication] : "{latest}"[Date - Publication]'


def _append_pubmed_filters(query: str, question: Question) -> str:
    filters: list[str] = []
    date_clause = _date_clause(question)
    if date_clause:
        filters.append(f"({date_clause})")
    if question.filters.languages:
        filters.append(
            _or_group([f'{_quoted(language)}[Language]' for language in question.filters.languages])
        )
    if question.filters.publication_types:
        filters.append(
            _or_group(
                [
                    f'{_quoted(publication_type)}[Publication Type]'
                    for publication_type in question.filters.publication_types
                ]
            )
        )
    return " AND ".join([query, *[part for part in filters if part]])


def _boolean_queries(question: Question, formatter: Any) -> tuple[str, str | None]:
    required, optional = _blocks(question)
    sensitivity = " AND ".join(formatter(question, name) for name in required)
    precision = None
    if optional:
        precision = " AND ".join(
            [sensitivity, *[formatter(question, name) for name in optional]]
        )
    return sensitivity, precision


def _plain_queries(question: Question) -> tuple[str, str | None]:
    required, optional = _blocks(question)
    sensitivity = " ".join(question.components[name].text for name in required)
    precision = " ".join(
        [sensitivity, *[question.components[name].text for name in optional]]
    ) if optional else None
    return sensitivity, precision


def compile_strategy(
    question: Question,
    *,
    mode: str,
    limit_per_source: int | str,
    sources: list[str],
    precision: bool = False,
) -> Strategy:
    if mode not in {"quick", "review"}:
        raise ValueError("mode must be quick or review")
    pubmed, pubmed_precision = _boolean_queries(question, _pubmed_block)
    pubmed = _append_pubmed_filters(pubmed, question)
    if pubmed_precision:
        pubmed_precision = _append_pubmed_filters(pubmed_precision, question)
    plain, plain_precision = _boolean_queries(question, _plain_block)
    s2, s2_precision = _boolean_queries(question, _s2_block)
    s2 = s2.replace(" AND ", " + ")
    if s2_precision:
        s2_precision = s2_precision.replace(" AND ", " + ")
    s2_plain, s2_plain_precision = _plain_queries(question)
    scopus, scopus_precision = _boolean_queries(question, _scopus_block)
    selected = "precision" if precision else "sensitivity"
    strategies: dict[str, SourceStrategy] = {}

    if "pubmed" in sources:
        strategies["pubmed"] = SourceStrategy(
            source="pubmed",
            query=pubmed,
            precision_query=pubmed_precision,
            selected_variant=selected,
            request_parameters={"db": "pubmed", "term": pubmed, "retmode": "json"},
        )
    if "pmc" in sources:
        strategies["pmc"] = SourceStrategy(
            source="pmc",
            query=pubmed,
            precision_query=pubmed_precision,
            selected_variant=selected,
            request_parameters={"db": "pmc", "term": pubmed, "retmode": "json"},
            warnings=["PMC is queried directly; this is not Europe PMC."],
        )
    if "openalex" in sources:
        parameters: dict[str, Any] = {"search": plain, "cursor": "*"}
        date_filters: list[str] = []
        if question.filters.from_date:
            date_filters.append(f"from_publication_date:{question.filters.from_date}")
        if question.filters.to_date:
            date_filters.append(f"to_publication_date:{question.filters.to_date}")
        if question.filters.languages:
            date_filters.append("language:" + "|".join(question.filters.languages))
        if date_filters:
            parameters["filter"] = ",".join(date_filters)
        strategies["openalex"] = SourceStrategy(
            source="openalex",
            query=plain,
            precision_query=plain_precision,
            selected_variant=selected,
            request_parameters=parameters,
            warnings=[
                "MeSH and PubMed field tags are unavailable; concept blocks are submitted "
                "as free text."
            ],
        )
    if "semantic-scholar" in sources:
        use_bulk = limit_per_source == "all" or int(limit_per_source) > 1000
        s2_query = s2 if use_bulk else s2_plain
        s2_query_precision = s2_precision if use_bulk else s2_plain_precision
        parameters = {"query": s2_query, "endpoint": "bulk" if use_bulk else "relevance"}
        if question.filters.from_date or question.filters.to_date:
            from_year = (question.filters.from_date or "").split("-")[0]
            to_year = (question.filters.to_date or "").split("-")[0]
            parameters["year"] = f"{from_year}-{to_year}"
        strategies["semantic-scholar"] = SourceStrategy(
            source="semantic-scholar",
            query=s2_query,
            precision_query=s2_query_precision,
            selected_variant=selected,
            request_parameters=parameters,
            warnings=[
                (
                    "MeSH and field tags are unavailable; concept groups use Semantic Scholar's "
                    "+/| bulk-search syntax and publication-date filters are reduced to years."
                    if use_bulk
                    else "MeSH, field tags, and Boolean operators are unavailable in Semantic "
                    "Scholar relevance search; core concepts are submitted as plain text and "
                    "publication-date filters are reduced to years."
                ),
                *(
                    ["Language and publication-type filters require post-retrieval filtering."]
                    if question.filters.languages or question.filters.publication_types
                    else []
                ),
            ],
        )
    if "scopus" in sources:
        scopus_query = f"TITLE-ABS-KEY({scopus})"
        scopus_precision_query = (
            f"TITLE-ABS-KEY({scopus_precision})" if scopus_precision else None
        )
        year_parts: list[str] = []
        if question.filters.from_date:
            year_parts.append(f"PUBYEAR AFT {int(question.filters.from_date[:4]) - 1}")
        if question.filters.to_date:
            year_parts.append(f"PUBYEAR BEF {int(question.filters.to_date[:4]) + 1}")
        if year_parts:
            suffix = " AND " + " AND ".join(year_parts)
            scopus_query += suffix
            if scopus_precision_query:
                scopus_precision_query += suffix
        strategies["scopus"] = SourceStrategy(
            source="scopus",
            query=scopus_query,
            precision_query=scopus_precision_query,
            selected_variant=selected,
            request_parameters={"query": scopus_query, "view": "STANDARD"},
            warnings=[
                "MeSH terms are translated to free text.",
                *(
                    ["Language and publication-type filters require post-retrieval filtering."]
                    if question.filters.languages or question.filters.publication_types
                    else []
                ),
            ],
        )
    warnings = [
        "Comparison/outcome/context blocks are retained as optional precision blocks and are "
        "not required by the default high-recall query."
    ]
    return Strategy(
        schema_version="1",
        tool_version=__version__,
        mode=mode,
        created_at=datetime.now(UTC).isoformat(),
        question=deepcopy(question),
        limit_per_source=limit_per_source,
        strategies=strategies,
        warnings=warnings,
    )


def default_sources(*, scopus_configured: bool) -> list[str]:
    return [*CORE_SOURCES, *(["scopus"] if scopus_configured else [])]
