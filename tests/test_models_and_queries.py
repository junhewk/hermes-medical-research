from __future__ import annotations

import pytest

from hermes_medical_search.models import Question, Strategy, ValidationError
from hermes_medical_search.query import compile_strategy


def pico_question() -> Question:
    return Question.from_dict(
        {
            "schema_version": "1",
            "framework": "PICO",
            "question": "Does CGM improve HbA1c in adults with type 2 diabetes?",
            "components": {
                "population": {
                    "text": "adults with type 2 diabetes",
                    "synonyms": ["T2DM"],
                    "resolved_mesh": ["Diabetes Mellitus, Type 2"],
                },
                "intervention": {
                    "text": "continuous glucose monitoring",
                    "synonyms": ["CGM"],
                    "resolved_mesh": ["Continuous Glucose Monitoring"],
                },
                "comparison": {"text": "self-monitoring", "synonyms": ["SMBG"]},
                "outcome": {"text": "glycated hemoglobin", "synonyms": ["HbA1c"]},
            },
            "filters": {
                "from_date": "2020-01-01",
                "to_date": "2026-08-08",
                "languages": ["English"],
                "publication_types": ["Randomized Controlled Trial"],
            },
        }
    )


def test_question_validation_requires_framework_components() -> None:
    with pytest.raises(ValidationError, match="components.intervention"):
        Question.from_dict(
            {
                "schema_version": "1",
                "framework": "PICO",
                "question": "Question",
                "components": {"population": {"text": "adults"}},
            }
        )


def test_v1_question_is_upgraded_to_one_group_per_component() -> None:
    question = pico_question()
    assert question.schema_version == "2"
    assert question.migrated_from_schema == "1"
    assert question.components["population"].groups[0].label == "population"
    assert question.to_dict()["components"]["population"]["groups"][0]["text"] == (
        "adults with type 2 diabetes"
    )


def test_v2_rejects_empty_groups_and_duplicate_labels() -> None:
    base = {
        "schema_version": "2",
        "framework": "PCC",
        "question": "Question",
        "components": {
            "population": {"groups": []},
            "concept": {
                "groups": [{"label": "technology", "text": "large language models"}]
            },
        },
    }
    with pytest.raises(ValidationError, match="non-empty"):
        Question.from_dict(base)
    base["components"]["population"] = {
        "groups": [
            {"label": "Learners", "text": "medical students"},
            {"label": "learners", "text": "undergraduates"},
        ]
    }
    with pytest.raises(ValidationError, match="labels must be unique"):
        Question.from_dict(base)


def test_question_rejects_unknown_source_and_bad_date() -> None:
    payload = pico_question().to_dict()
    payload["sources"] = ["google-scholar"]
    with pytest.raises(ValidationError, match="unsupported sources"):
        Question.from_dict(payload)
    payload = pico_question().to_dict()
    payload["filters"]["from_date"] = "08/08/2026"
    with pytest.raises(ValidationError, match="YYYY-MM-DD"):
        Question.from_dict(payload)


def test_pico_query_golden_dialects() -> None:
    strategy = compile_strategy(
        pico_question(),
        mode="review",
        limit_per_source=100,
        sources=["pubmed", "pmc", "openalex", "semantic-scholar", "scopus"],
    )
    pubmed = strategy.strategies["pubmed"]
    assert '"Diabetes Mellitus, Type 2"[Mesh]' in pubmed.query
    assert '"continuous glucose monitoring"[tiab]' in pubmed.query
    assert "glycated hemoglobin" not in pubmed.query
    assert "glycated hemoglobin" in (pubmed.precision_query or "")
    assert '"2020-01-01"[Date - Publication]' in pubmed.query
    assert '"English"[Language]' in pubmed.query
    assert strategy.strategies["pmc"].request_parameters["db"] == "pmc"
    assert strategy.strategies["openalex"].request_parameters["filter"] == (
        "from_publication_date:2020-01-01,to_publication_date:2026-08-08,language:en"
    )
    assert strategy.strategies["semantic-scholar"].request_parameters["year"] == "2020-2026"
    assert strategy.strategies["semantic-scholar"].request_parameters["endpoint"] == "bulk"
    assert " + " in strategy.strategies["semantic-scholar"].query
    assert strategy.strategies["scopus"].query.startswith("TITLE-ABS-KEY(")
    assert "PUBYEAR AFT 2019" in strategy.strategies["scopus"].query
    assert "PUBYEAR BEF 2027" in strategy.strategies["scopus"].query


def test_precision_selection_and_round_trip() -> None:
    strategy = compile_strategy(
        pico_question(),
        mode="review",
        limit_per_source="all",
        sources=["pubmed"],
        precision=True,
    )
    assert strategy.strategies["pubmed"].selected_variant == "precision"
    assert strategy.strategies["pubmed"].selected_query == strategy.strategies[
        "pubmed"
    ].precision_query
    restored = Strategy.from_dict(strategy.to_dict())
    assert restored.to_dict() == strategy.to_dict()


def test_semantic_scholar_all_uses_bulk_boolean_dialect() -> None:
    strategy = compile_strategy(
        pico_question(),
        mode="review",
        limit_per_source="all",
        sources=["semantic-scholar"],
    )
    source = strategy.strategies["semantic-scholar"]
    assert source.request_parameters["endpoint"] == "bulk"
    assert " + " in source.query
    assert " | " in source.query


def test_pcc_default_excludes_context_but_preserves_precision_variant() -> None:
    question = Question.from_dict(
        {
            "schema_version": "2",
            "framework": "PCC",
            "question": "Use of LLMs in communication education",
            "components": {
                "population": {
                    "groups": [{"label": "learners", "text": "medical students"}]
                },
                "concept": {
                    "groups": [
                        {
                            "label": "technology",
                            "text": "large language models",
                            "synonyms": ["generative artificial intelligence"],
                            "resolved_mesh": ["Artificial Intelligence"],
                        },
                        {
                            "label": "training focus",
                            "text": "communication skills training",
                            "synonyms": ["clinical communication training"],
                        },
                    ]
                },
                "context": {
                    "groups": [
                        {
                            "label": "setting",
                            "text": "undergraduate medical education",
                        }
                    ]
                },
            },
        }
    )
    strategy = compile_strategy(
        question,
        mode="quick",
        limit_per_source=20,
        sources=["pubmed", "scopus"],
    )
    pubmed = strategy.strategies["pubmed"]
    assert '"large language models"[tiab]' in pubmed.query
    assert '"communication skills training"[tiab]' in pubmed.query
    assert "undergraduate medical education" not in pubmed.query
    assert "undergraduate medical education" in (pubmed.precision_query or "")
    scopus = strategy.strategies["scopus"]
    assert '"Artificial Intelligence"' in scopus.query
    assert "communication skills training" in scopus.query
    assert "undergraduate medical education" not in scopus.query


def test_review_supports_per_source_variants_and_selected_request_parameters() -> None:
    question = pico_question()
    strategy = compile_strategy(
        question,
        mode="review",
        limit_per_source=10,
        sources=["pubmed", "openalex"],
        variants={"openalex": "precision"},
    )
    assert strategy.strategies["pubmed"].selected_variant == "sensitivity"
    openalex = strategy.strategies["openalex"]
    assert openalex.selected_variant == "precision"
    assert openalex.request_parameters["search"] == openalex.precision_query


def test_quick_semantic_scholar_records_boolean_group_degradation() -> None:
    strategy = compile_strategy(
        pico_question(),
        mode="quick",
        limit_per_source=20,
        sources=["semantic-scholar"],
    )
    source = strategy.strategies["semantic-scholar"]
    assert source.request_parameters["endpoint"] == "relevance"
    assert any(item.feature == "boolean_groups" for item in source.degradations)
