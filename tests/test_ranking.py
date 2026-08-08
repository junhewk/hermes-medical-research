from __future__ import annotations

from datetime import date

from hermes_medical_search.models import Question
from hermes_medical_search.ranking import (
    citation_score,
    deduplicate,
    evidence_score,
    rank_records,
    recency_score,
    relevance_score,
)


def question(framework: str = "PICO") -> Question:
    components = (
        {
            "population": {"text": "adults with diabetes", "synonyms": ["T2DM"]},
            "intervention": {
                "text": "continuous glucose monitoring",
                "synonyms": ["CGM"],
            },
            "outcome": {"text": "glycated hemoglobin", "synonyms": ["HbA1c"]},
        }
        if framework == "PICO"
        else {
            "population": {"text": "medical students"},
            "concept": {"text": "large language models", "synonyms": ["LLM"]},
            "context": {"text": "communication training"},
        }
    )
    return Question.from_dict(
        {
            "schema_version": "1",
            "framework": framework,
            "question": "test question",
            "components": components,
        }
    )


def record(**updates: object) -> dict[str, object]:
    base: dict[str, object] = {
        "source": "pubmed",
        "source_id": "1",
        "source_rank": 1,
        "title": "Randomized trial of continuous glucose monitoring in adults with T2DM",
        "abstract": "CGM improved HbA1c.",
        "authors": ["A Author"],
        "journal": "Example Journal",
        "publication_date": "2024-01-01",
        "year": "2024",
        "doi": "10.1000/example",
        "pmid": "123",
        "pmcid": None,
        "citation_count": 10,
        "url": "https://example.org",
        "publication_types": ["Randomized Controlled Trial"],
        "mesh_terms": [],
        "language": "English",
    }
    base.update(updates)
    return base


def test_deduplicate_by_doi_preserves_source_records() -> None:
    other = record(
        source="openalex",
        source_id="W1",
        source_rank=3,
        pmid=None,
        abstract="A longer abstract about CGM and HbA1c in adults with diabetes.",
        citation_count=25,
    )
    merged = deduplicate([record(), other])
    assert len(merged) == 1
    assert merged[0]["sources"] == ["pubmed", "openalex"]
    assert len(merged[0]["source_records"]) == 2
    assert merged[0]["citation_count"] == 25


def test_deduplicate_uses_pmid_pmc_crosswalk_when_doi_missing() -> None:
    pubmed = record(doi=None, pmid="999", pmcid=None)
    pmc = record(source="pmc", source_id="PMC7", doi=None, pmid="999", pmcid="PMC7")
    assert len(deduplicate([pubmed, pmc])) == 1


def test_deduplicate_merges_transitive_identifier_bridge() -> None:
    by_doi = record(pmid=None, pmcid=None)
    by_pmid = record(doi=None, pmid="999", pmcid=None)
    bridge = record(source="pmc", source_id="PMC7", doi="10.1000/example", pmid="999")
    merged = deduplicate([by_doi, by_pmid, bridge])
    assert len(merged) == 1
    assert len(merged[0]["source_records"]) == 3


def test_title_fallback_does_not_merge_conflicting_strong_identifiers() -> None:
    first = record(doi="10.1/first", pmid=None)
    second = record(doi="10.1/second", pmid=None)
    assert len(deduplicate([first, second])) == 2


def test_scoring_components_and_no_journal_bonus() -> None:
    first = record(journal="New England Journal of Medicine")
    second = record(source_id="2", doi="10.1000/other", pmid="124", journal="Unknown Journal")
    ranked = rank_records([first, second], question(), today=date(2026, 1, 1))
    assert ranked[0]["ranking"]["version"] == "mdr-v2-grouped"
    assert ranked[0]["ranking"]["evidence_category"] == "II"
    assert ranked[0]["ranking"]["composite_score"] == ranked[1]["ranking"][
        "composite_score"
    ]
    assert "journal" not in ranked[0]["ranking"]


def test_component_formula_regressions() -> None:
    assert evidence_score(record(publication_types=["Systematic Review"])) == ("I", 1.0)
    assert round(citation_score(999), 6) == 1.0
    assert round(recency_score("2023-01-01", half_life_years=3, today=date(2026, 1, 1)), 3) == 0.5


def test_compound_concept_uses_weakest_required_group() -> None:
    compound = Question.from_dict(
        {
            "schema_version": "2",
            "framework": "PCC",
            "question": "LLMs for communication training in medical students",
            "components": {
                "population": {
                    "groups": [{"label": "learners", "text": "medical students"}]
                },
                "concept": {
                    "groups": [
                        {"label": "technology", "text": "large language models"},
                        {
                            "label": "training focus",
                            "text": "communication skills training",
                        },
                    ]
                },
            },
        }
    )
    partial = record(
        title="Large language models for medical students",
        abstract="Artificial intelligence in medical education.",
    )
    complete = record(
        title="Large language models for communication skills training",
        abstract="Communication skills training for medical students.",
    )
    assert relevance_score(complete, compound) > relevance_score(partial, compound)
    ranked = rank_records([partial, complete], compound, today=date(2026, 1, 1))
    assert ranked[0]["ranking"]["group_relevance"]["concept.training focus"] == 1.0
