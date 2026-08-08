"""Client-side retrieval filters.

These exercise `_passes_filters` against the language representation each provider actually
returns. PubMed emits ISO 639-2/B ("eng"), PMC and OpenAlex emit ISO 639-1 ("en"), and Semantic
Scholar and Scopus emit nothing at all. Comparing those directly against a user's "english"
silently discarded every PubMed record, so the round trip is pinned here per source.
"""

from __future__ import annotations

import pytest

from hermes_medical_search.models import Question, language_code, language_name
from hermes_medical_search.orchestrator import _passes_filters
from hermes_medical_search.query import compile_strategy


def strategy_with(**filters):
    question = Question.from_dict(
        {
            "schema_version": "2",
            "framework": "PICO",
            "question": "A question",
            "components": {
                "population": {"groups": [{"label": "p", "text": "adults"}]},
                "intervention": {"groups": [{"label": "i", "text": "aspirin"}]},
            },
            "filters": filters,
        }
    )
    return compile_strategy(question, mode="quick", limit_per_source=10, sources=["pubmed"])


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("english", "en"),
        ("English", "en"),
        ("ENGLISH", "en"),
        ("en", "en"),
        ("eng", "en"),  # PubMed
        ("ger", "de"),  # ISO 639-2/B
        ("deu", "de"),  # ISO 639-2/T
        ("german", "de"),
        ("fre", "fr"),
        ("fra", "fr"),
        ("chi", "zh"),
        ("zho", "zh"),
        ("dut", "nl"),
        ("kor", "ko"),
        ("und", ""),  # undetermined is missing metadata, not a failed match
        ("mul", ""),
        ("klingon", "klingon"),  # unknown values pass through rather than being coerced
    ],
)
def test_language_code_normalizes_every_dialect(value: str, expected: str) -> None:
    assert language_code(value) == expected


@pytest.mark.parametrize(
    ("source_language", "retained"),
    [
        ("eng", True),  # pubmed
        ("en", True),  # pmc, openalex
        ("English", True),
        (None, True),  # semantic-scholar, scopus: no metadata, so no filtering
        ("", True),
        ("und", True),  # undetermined must not be treated as a mismatch
        ("ger", False),
        ("fr", False),
    ],
)
def test_language_filter_matches_across_provider_dialects(
    source_language: str | None, retained: bool
) -> None:
    strategy = strategy_with(languages=["english"])
    record = {"language": source_language, "publication_types": []}
    assert _passes_filters(record, strategy) is retained


def test_language_filter_accepts_a_three_letter_request() -> None:
    strategy = strategy_with(languages=["kor"])
    assert _passes_filters({"language": "kor"}, strategy) is True
    assert _passes_filters({"language": "ko"}, strategy) is True
    assert _passes_filters({"language": "eng"}, strategy) is False


def test_no_language_filter_retains_every_record() -> None:
    strategy = strategy_with()
    for language in ("eng", "ger", None, "und"):
        assert _passes_filters({"language": language}, strategy) is True


@pytest.mark.parametrize(
    ("types", "retained"),
    [
        (["Randomized Controlled Trial"], True),
        (["Systematic Review", "Journal Article"], True),
        (["Journal Article"], False),
        ([], True),  # absent metadata is not a mismatch
    ],
)
def test_publication_type_filter(types: list[str], retained: bool) -> None:
    strategy = strategy_with(publication_types=["Randomized Controlled Trial", "Systematic Review"])
    assert _passes_filters({"publication_types": types}, strategy) is retained


def test_filters_compose() -> None:
    strategy = strategy_with(languages=["english"], publication_types=["Systematic Review"])
    assert _passes_filters(
        {"language": "eng", "publication_types": ["Systematic Review"]}, strategy
    ) is True
    assert _passes_filters(
        {"language": "ger", "publication_types": ["Systematic Review"]}, strategy
    ) is False
    assert _passes_filters(
        {"language": "eng", "publication_types": ["Editorial"]}, strategy
    ) is False


@pytest.mark.parametrize(
    ("supplied", "expected"),
    [
        ("english", "english"),
        ("English", "english"),
        ("eng", "english"),
        ("en", "english"),
        ("ger", "german"),
        ("de", "german"),
        ("kor", "korean"),
        ("ko", "korean"),
    ],
)
def test_language_name_expands_codes_for_pubmed(supplied: str, expected: str) -> None:
    assert language_name(supplied) == expected


@pytest.mark.parametrize("supplied", ["english", "English", "eng", "en"])
def test_pubmed_query_always_uses_the_full_language_name(supplied: str) -> None:
    """PubMed's [Language] matches full names only: "eng"[Language] returns zero results."""
    strategy = strategy_with(languages=[supplied])
    query = strategy.strategies["pubmed"].query
    assert '"english"[Language]' in query
    assert '"eng"[Language]' not in query
    assert '"en"[Language]' not in query


def test_a_language_code_survives_the_whole_round_trip() -> None:
    """A caller writing "eng" must both compile a matching query and retain PubMed's records."""
    strategy = strategy_with(languages=["eng"])
    assert '"english"[Language]' in strategy.strategies["pubmed"].query
    assert _passes_filters({"language": "eng"}, strategy) is True
