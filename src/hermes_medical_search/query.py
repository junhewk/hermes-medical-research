from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from . import __version__
from .models import (
    CORE_SOURCES,
    ConceptGroup,
    QueryDegradation,
    Question,
    SourceStrategy,
    Strategy,
    ValidationError,
    language_code,
    language_name,
)

GroupFormatter = Callable[[ConceptGroup], str]


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _quoted(value: str) -> str:
    return f'"{_escape(value)}"'


def _or_group(parts: list[str], *, operator: str = " OR ") -> str:
    unique = list(dict.fromkeys(part for part in parts if part))
    if not unique:
        return ""
    return unique[0] if len(unique) == 1 else f"({operator.join(unique)})"


def _pubmed_group(group: ConceptGroup) -> str:
    mesh = [f'{_quoted(term)}[Mesh]' for term in group.resolved_mesh]
    free = [f'{_quoted(term)}[tiab]' for term in group.free_terms()]
    return _or_group([*mesh, *free])


def _free_text_group(group: ConceptGroup) -> str:
    return _or_group([_quoted(term) for term in group.all_terms()])


def _s2_group(group: ConceptGroup) -> str:
    return _or_group(
        [_quoted(term) for term in group.all_terms()],
        operator=" | ",
    )


def _component(question: Question, name: str, formatter: GroupFormatter, *, joiner: str) -> str:
    return joiner.join(formatter(group) for group in question.components[name].groups)


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
    # PubMed accepts both YYYY/MM/DD and YYYY-MM-DD and normalizes them identically; the open
    # bounds use the same ISO form as user-supplied dates so one clause never mixes both.
    earliest = start or "1000-01-01"
    latest = end or "3000-12-31"
    return f'"{earliest}"[Date - Publication] : "{latest}"[Date - Publication]'


def _append_pubmed_filters(query: str, question: Question) -> str:
    filters: list[str] = []
    date_clause = _date_clause(question)
    if date_clause:
        filters.append(f"({date_clause})")
    if question.filters.languages:
        # PubMed matches [Language] on the full English name only; "eng"/"en" silently match
        # nothing, so a code supplied by the caller must be expanded here.
        filters.append(
            _or_group(
                [
                    f'{_quoted(language_name(language))}[Language]'
                    for language in question.filters.languages
                ]
            )
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


def _boolean_queries(
    question: Question,
    formatter: GroupFormatter,
    *,
    joiner: str = " AND ",
) -> tuple[str, str | None]:
    required, optional = _blocks(question)
    sensitivity = joiner.join(
        _component(question, name, formatter, joiner=joiner) for name in required
    )
    precision = None
    if optional:
        precision = joiner.join(
            [
                sensitivity,
                *[
                    _component(question, name, formatter, joiner=joiner)
                    for name in optional
                ],
            ]
        )
    return sensitivity, precision


def _plain_queries(question: Question) -> tuple[str, str | None]:
    required, optional = _blocks(question)

    def texts(names: list[str]) -> list[str]:
        return [
            group.text
            for name in names
            for group in question.components[name].groups
        ]

    sensitivity = " ".join(texts(required))
    precision = " ".join([sensitivity, *texts(optional)]) if optional else None
    return sensitivity, precision


def _selected(
    source: str,
    query: str,
    precision_query: str | None,
    variants: dict[str, str],
) -> tuple[str, str]:
    variant = variants.get(source, "sensitivity")
    if variant == "precision":
        if not precision_query:
            raise ValidationError(f"precision variant is unavailable for {source}")
        return variant, precision_query
    return variant, query


def _degradation(feature: str, reason: str, fallback: str) -> QueryDegradation:
    return QueryDegradation(feature=feature, reason=reason, fallback=fallback)


def compile_strategy(
    question: Question,
    *,
    mode: str,
    limit_per_source: int | str,
    sources: list[str],
    variants: dict[str, str] | None = None,
) -> Strategy:
    if mode not in {"quick", "review"}:
        raise ValueError("mode must be quick or review")
    selected_variants = dict(variants or {})
    unknown_variant_sources = sorted(set(selected_variants) - set(sources))
    if unknown_variant_sources:
        raise ValidationError(
            "variants were provided for unselected sources: "
            + ", ".join(unknown_variant_sources)
        )
    invalid_variants = sorted(
        f"{source}={variant}"
        for source, variant in selected_variants.items()
        if variant not in {"sensitivity", "precision"}
    )
    if invalid_variants:
        raise ValidationError("invalid source variants: " + ", ".join(invalid_variants))

    pubmed, pubmed_precision = _boolean_queries(question, _pubmed_group)
    pubmed = _append_pubmed_filters(pubmed, question)
    if pubmed_precision:
        pubmed_precision = _append_pubmed_filters(pubmed_precision, question)
    free_text, free_text_precision = _boolean_queries(question, _free_text_group)
    s2_bulk, s2_bulk_precision = _boolean_queries(
        question, _s2_group, joiner=" + "
    )
    s2_plain, s2_plain_precision = _plain_queries(question)
    strategies: dict[str, SourceStrategy] = {}

    if "pubmed" in sources:
        variant, active = _selected(
            "pubmed", pubmed, pubmed_precision, selected_variants
        )
        strategies["pubmed"] = SourceStrategy(
            source="pubmed",
            query=pubmed,
            precision_query=pubmed_precision,
            selected_variant=variant,
            request_parameters={"db": "pubmed", "term": active, "retmode": "json"},
        )
    if "pmc" in sources:
        variant, active = _selected("pmc", pubmed, pubmed_precision, selected_variants)
        strategies["pmc"] = SourceStrategy(
            source="pmc",
            query=pubmed,
            precision_query=pubmed_precision,
            selected_variant=variant,
            request_parameters={"db": "pmc", "term": active, "retmode": "json"},
            warnings=["PMC is queried directly; this is not Europe PMC."],
        )
    if "openalex" in sources:
        variant, active = _selected(
            "openalex", free_text, free_text_precision, selected_variants
        )
        parameters: dict[str, Any] = {"search": active, "cursor": "*"}
        date_filters: list[str] = []
        if question.filters.from_date:
            date_filters.append(f"from_publication_date:{question.filters.from_date}")
        if question.filters.to_date:
            date_filters.append(f"to_publication_date:{question.filters.to_date}")
        if question.filters.languages:
            date_filters.append(
                "language:" + "|".join(language_code(value) for value in question.filters.languages)
            )
        if date_filters:
            parameters["filter"] = ",".join(date_filters)
        strategies["openalex"] = SourceStrategy(
            source="openalex",
            query=free_text,
            precision_query=free_text_precision,
            selected_variant=variant,
            request_parameters=parameters,
            degradations=[
                _degradation(
                    "controlled_vocabulary",
                    "OpenAlex has no MeSH field.",
                    "Resolved MeSH headings are submitted as free-text alternatives.",
                ),
                _degradation(
                    "field_tags",
                    "OpenAlex has no PubMed title/abstract field tags.",
                    "Terms are searched across title, abstract, and indexed full text.",
                ),
            ],
        )
    if "semantic-scholar" in sources:
        use_bulk = mode == "review" or limit_per_source == "all" or int(limit_per_source) > 1000
        s2_query = s2_bulk if use_bulk else s2_plain
        s2_query_precision = s2_bulk_precision if use_bulk else s2_plain_precision
        variant, active = _selected(
            "semantic-scholar", s2_query, s2_query_precision, selected_variants
        )
        parameters = {
            "query": active,
            "endpoint": "bulk" if use_bulk else "relevance",
        }
        if question.filters.from_date or question.filters.to_date:
            from_year = (question.filters.from_date or "").split("-")[0]
            to_year = (question.filters.to_date or "").split("-")[0]
            parameters["year"] = f"{from_year}-{to_year}"
        degradations = [
            _degradation(
                "controlled_vocabulary",
                "Semantic Scholar has no MeSH field.",
                (
                    "Resolved MeSH headings are submitted as free-text alternatives."
                    if use_bulk
                    else "Quick relevance search retains canonical group text and omits "
                    "controlled-vocabulary alternatives that cannot be ORed safely."
                ),
            ),
            _degradation(
                "field_tags",
                "Semantic Scholar has no PubMed field tags.",
                "Terms are searched in title and abstract.",
            ),
        ]
        if not use_bulk:
            degradations.append(
                _degradation(
                    "boolean_groups",
                    "Semantic Scholar relevance search accepts plain text only.",
                    "Canonical text from every selected group is submitted without Boolean syntax.",
                )
            )
        if question.filters.from_date or question.filters.to_date:
            degradations.append(
                _degradation(
                    "date_precision",
                    "Semantic Scholar filters publication dates at year precision.",
                    "ISO date bounds are reduced to inclusive years.",
                )
            )
        if question.filters.languages:
            degradations.append(
                _degradation(
                    "language_filter",
                    "Semantic Scholar search does not expose a language filter.",
                    "Returned language metadata is filtered when available.",
                )
            )
        if question.filters.publication_types:
            degradations.append(
                _degradation(
                    "publication_type_filter",
                    "Semantic Scholar search does not expose a publication-type filter.",
                    "Returned publication types are filtered when available.",
                )
            )
        strategies["semantic-scholar"] = SourceStrategy(
            source="semantic-scholar",
            query=s2_query,
            precision_query=s2_query_precision,
            selected_variant=variant,
            request_parameters=parameters,
            degradations=degradations,
        )
    if "scopus" in sources:
        scopus_query = f"TITLE-ABS-KEY({free_text})"
        scopus_precision_query = (
            f"TITLE-ABS-KEY({free_text_precision})" if free_text_precision else None
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
        variant, active = _selected(
            "scopus", scopus_query, scopus_precision_query, selected_variants
        )
        degradations = [
            _degradation(
                "controlled_vocabulary",
                "Scopus has no MeSH field.",
                "Resolved MeSH headings are submitted as free-text alternatives.",
            )
        ]
        if question.filters.languages:
            degradations.append(
                _degradation(
                    "language_filter",
                    "This CLI does not compile Scopus language clauses.",
                    "Returned language metadata is filtered when available.",
                )
            )
        if question.filters.publication_types:
            degradations.append(
                _degradation(
                    "publication_type_filter",
                    "This CLI does not compile Scopus document-type clauses.",
                    "Returned publication types are filtered when available.",
                )
            )
        strategies["scopus"] = SourceStrategy(
            source="scopus",
            query=scopus_query,
            precision_query=scopus_precision_query,
            selected_variant=variant,
            request_parameters={"query": active, "view": "STANDARD"},
            degradations=degradations,
        )
    warnings = [
        "Comparison/outcome/context components are retained as optional precision additions "
        "and are not required by the default high-recall query."
    ]
    return Strategy(
        schema_version="2",
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
