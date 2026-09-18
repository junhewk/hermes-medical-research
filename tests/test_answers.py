"""Constrained answer shapes: the grammar-safe schemas, the cached prefix, and the mapping."""

from __future__ import annotations

import pytest

from hermes_medical_research import answers
from hermes_medical_research.search.models import ValidationError


def screening_packet(record_id: str = "r-1", title: str = "A trial of tutoring") -> dict:
    return {
        "eligibility": {"include": ["medical students"], "exclude": ["animal studies"]},
        "target_ids": [record_id],
        "source": {"record": {"record_id": record_id, "title": title, "abstract": "Text."}},
    }


def test_every_schema_uses_only_grammar_safe_keywords():
    for kind in answers.CALL_KINDS:
        answers.check_keywords(answers.RESULT_SCHEMAS[kind])


def test_no_payload_is_a_bare_string_or_enum_at_the_top_level():
    # llama.cpp compiles a real grammar only for non-string tool arguments, and a string enum
    # resolves to a string, so every payload has to be an object.
    for kind, schema in answers.RESULT_SCHEMAS.items():
        assert schema["type"] == "object", kind
        assert schema["additionalProperties"] is False, kind


def test_prompt_prefix_is_identical_across_records_and_holds_no_record_text():
    first, first_tail = answers.build_prompt("screening", screening_packet("r-1", "First title"))
    second, second_tail = answers.build_prompt("screening", screening_packet("r-2", "Second one"))

    assert first == second
    assert "First title" not in first and "Second one" not in second
    assert "First title" in first_tail and "Second one" in second_tail
    assert "medical students" in first


def test_a_retry_hint_lands_in_the_tail_so_the_cached_prefix_survives():
    prefix, tail = answers.build_prompt(
        "screening", screening_packet(), hint="result.decision must be one of include, exclude"
    )

    assert prefix == answers.build_prompt("screening", screening_packet())[0]
    assert "must be one of include, exclude" in tail


def test_the_contract_is_rendered_from_the_schema():
    text = answers.contract_text("screening")

    assert "decision (required): one of include, exclude, uncertain" in text
    assert "basis (optional)" in text


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"decision": "probably-include", "reason": "x"}, "must be one of"),
        ({"decision": "include"}, "reason is required"),
        ({"decision": "include", "reason": "x", "verdict": "y"}, "unknown field"),
        ("exclude - no comparator", "must be an object"),
    ],
)
def test_check_shape_rejects_off_schema_answers_with_an_actionable_message(payload, message):
    with pytest.raises(ValidationError) as error:
        answers.check_shape(payload, answers.RESULT_SCHEMAS["screening"])
    assert message in str(error.value)


def test_applying_a_screening_answer_never_touches_identity_or_digests():
    proposal = {
        "schema_version": "2",
        "base_digests": {"records": "abc"},
        "stages": {"screening": {"schema_version": "2", "records": [
            {"record_id": "r-1", "decision": "", "basis": "title-abstract", "reason": ""}
        ]}},
    }

    updated = answers.apply_result(
        "screening", proposal,
        {"decision": "exclude", "reason": "No AI intervention."},
        screening_packet(),
    )

    row = updated["stages"]["screening"]["records"][0]
    assert (row["record_id"], row["decision"], row["basis"]) == ("r-1", "exclude", "title-abstract")
    assert updated["base_digests"] == {"records": "abc"}
    assert proposal["stages"]["screening"]["records"][0]["decision"] == ""


def test_coverage_outcomes_are_checked_against_the_packet():
    proposal = {"stages": {"coverage": {"records": [
        {"record_id": "r-1", "selection": "", "reason": "", "protocol_outcomes": []}
    ]}}}
    packet = {"outcomes": ["diagnostic accuracy"]}

    with pytest.raises(ValidationError) as error:
        answers.apply_result("coverage", proposal, {
            "selection": "selected", "reason": "Reports accuracy.",
            "protocol_outcomes": ["invented outcome"],
        }, packet)

    assert "unknown outcome" in str(error.value)


def test_a_study_merge_must_name_a_study_the_packet_showed():
    proposal = {"stages": {"studies": {"records": [
        {"study_id": "study-r-2", "record_ids": ["r-2"], "kind": "", "basis": ""}
    ]}}}
    packet = {"existing_studies": [{"study_id": "study-r-1", "record_ids": ["r-1"],
                                    "kind": "primary"}]}

    merged = answers.apply_result("studies", proposal, {
        "kind": "primary", "basis": "Shared registration NCT1.", "same_study_id": "study-r-1",
    }, packet)
    assert merged["stages"]["studies"]["records"][0]["record_ids"] == ["r-1", "r-2"]

    with pytest.raises(ValidationError) as error:
        answers.apply_result("studies", proposal, {
            "kind": "primary", "basis": "Guessed.", "same_study_id": "study-nowhere",
        }, packet)
    assert "not an existing study" in str(error.value)


def test_an_empty_same_study_id_keeps_the_record_as_its_own_study():
    proposal = {"stages": {"studies": {"records": [
        {"study_id": "study-r-2", "record_ids": ["r-2"], "kind": "", "basis": ""}
    ]}}}

    updated = answers.apply_result("studies", proposal, {
        "kind": "primary", "basis": "No companion report named.", "same_study_id": "",
    }, {"existing_studies": []})

    assert updated["stages"]["studies"]["records"][0]["study_id"] == "study-r-2"


def test_a_finding_keeps_its_identity_and_cites_only_packet_extractions():
    proposal = {
        "title": "",
        "limitations": [],
        "finding": {
            "finding_id": "finding-1",
            "protocol_outcomes": ["knowledge"],
            "outcome": "knowledge",
            "population": "",
            "comparison": "",
            "timepoint": "",
            "claim_basis": "",
            "comparator_type": "",
            "conclusion": "",
            "evidence": [],
            "overlap": {"status": "unknown", "rationale": ""},
            "certainty": {"origin": "report_assessment", "framework": "GRADE-informed",
                          "rating": "not-assessable", "rationale": "", "starting_point": "",
                          "rating_explanation": "", "domains": {}},
        },
    }
    packet = {"extractions": [{"extraction_id": "result-1"}], "text_limit": 600}
    result = {
        "title": "Knowledge gains",
        "limitations": ["Two small trials."],
        "population": "Medical students",
        "comparison": "AI tutoring versus usual teaching",
        "timepoint": "post-test",
        "claim_basis": "comparative",
        "comparator_type": "active_other",
        "conclusion": "AI tutoring improved scores.",
        "evidence": [{"extraction_id": "result-1", "use": "direct", "relationship": "supports",
                      "weight_rationale": "Largest trial.", "alignment_rationale": "Same outcome.",
                      "claim_support_checked": True}],
        "overlap": {"status": "not_applicable", "rationale": "Distinct cohorts."},
        "certainty": {"framework": "GRADE-informed", "rating": "low", "rationale": "Small trials.",
                      "starting_point": "randomized trials",
                      "rating_explanation": "Downgraded for imprecision.",
                      "domains": {"risk_of_bias": "some concerns", "inconsistency": "none",
                                  "indirectness": "none", "imprecision": "serious",
                                  "publication_bias": "undetected"}},
    }

    updated = answers.apply_result("synthesis", proposal, result, packet)

    finding = updated["finding"]
    assert finding["finding_id"] == "finding-1"
    assert finding["protocol_outcomes"] == ["knowledge"]
    assert finding["certainty"]["origin"] == "report_assessment"
    assert finding["certainty"]["rating"] == "low"

    with pytest.raises(ValidationError) as error:
        answers.apply_result(
            "synthesis", proposal,
            {**result, "evidence": [{**result["evidence"][0], "extraction_id": "result-9"}]},
            packet,
        )
    assert "unknown extraction_id" in str(error.value)


def test_the_response_format_carries_the_schema_and_a_stable_digest():
    block = answers.response_format("screening")

    assert block["type"] == "json_object"
    assert block["schema"] is answers.RESULT_SCHEMAS["screening"]
    assert answers.schema_digest("screening") == answers.schema_digest("screening")
    assert answers.schema_digest("screening") != answers.schema_digest("coverage")


@pytest.mark.parametrize(
    "text",
    [
        '{"decision": "exclude", "reason": "No comparator."}',
        '```json\n{"decision": "exclude", "reason": "No comparator."}\n```',
        'Here is my answer: {"decision": "exclude", "reason": "No comparator."} Hope that helps.',
    ],
)
def test_an_answer_is_read_through_a_fence_or_a_preface(text):
    assert answers.parse_answer("screening", text)["decision"] == "exclude"


@pytest.mark.parametrize(
    "text, message",
    [
        ("exclude - no comparator", "not one JSON object"),
        ('{"decision": "probably", "reason": "x"}', "must be one of"),
        ('{"decision": "exclude"}', "reason is required"),
        ('{"decision": "exclude", "reason": "x"', "not one JSON object"),
    ],
)
def test_an_unusable_answer_raises_a_message_the_next_attempt_can_carry(text, message):
    with pytest.raises(ValidationError) as error:
        answers.parse_answer("screening", text)
    assert message in str(error.value)
