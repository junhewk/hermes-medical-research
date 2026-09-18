"""Assessment recorded one protocol outcome at a time, instead of one hand-edited file."""

from __future__ import annotations

import pytest

from hermes_medical_research import assessment
from hermes_medical_research.search.models import ValidationError

OUTCOMES = ("Knowledge score", "Learner satisfaction")


def packet() -> dict:
    return {
        "outcome_checklist": [
            {"outcome": "Knowledge score", "index": 1, "extraction_id": "result-r-1-o1"},
            {"outcome": "Learner satisfaction", "index": 2, "extraction_id": "result-r-1-o2"},
        ],
    }


def proposal() -> dict:
    return {
        "schema_version": "2",
        "base_digests": {"records": "abc"},
        "stages": {
            "extractions": {"schema_version": "2", "records": [
                {"extraction_id": f"result-r-1-o{index}", "record_id": "r-1",
                 "study_id": "study-r-1", "protocol_outcome": name, "population": "",
                 "comparison": "", "outcome": "", "timepoint": "", "comparator_type": "",
                 "outcome_type": "benefit", "sample_size": None, "result": "",
                 "effect": {"measure": "", "basis": "", "value": None, "ci_low": None,
                            "ci_high": None, "units": "", "interval_type": "none",
                            "interval_level": None, "missing_reason": ""},
                 "favors": "uncertain", "direction_rationale": "",
                 "source_location": {"document_id": "", "locator": "", "quote": ""},
                 "support_checked": False, "support_rationale": ""}
                for index, name in enumerate(OUTCOMES, start=1)
            ]},
            "appraisals": {"schema_version": "2", "records": [
                {"extraction_id": "study-appraisal-r-1", "method": "rob2",
                 "method_version": "2019", "completion": "pending",
                 "overall_judgment": "not_assessable", "overall": "", "rationale": "",
                 "domains": {name: {"status": "pending", "judgment": "not_assessed",
                                    "rationale": "", "source_locations": [],
                                    "missing_reason": "", "assessment_basis": "",
                                    "inspected_locations": []}
                             for name in ("randomization", "deviations")}},
                {"extraction_id": "result-r-1-o1", "same_as": "study-appraisal-r-1"},
                {"extraction_id": "result-r-1-o2", "same_as": "study-appraisal-r-1"},
            ]},
            "dispositions": {"schema_version": "2", "records": [
                {"record_id": "r-1", "study_id": "study-r-1", "outcomes": [
                    {"protocol_outcome": name, "status": "", "rationale": "",
                     "extraction_ids": [], "inspected_locations": []}
                    for name in OUTCOMES
                ]}
            ]},
        },
    }


def appraisal_answer(**overrides) -> dict:
    answer = {
        "overall_judgment": "some_concerns",
        "overall": "Allocation was not concealed.",
        "rationale": "Randomization is described but concealment is not.",
        "domains": [
            {"name": "randomization", "status": "assessed", "judgment": "some_concerns",
             "rationale": "Sequence generation is described; concealment is not."},
            {"name": "deviations", "status": "assessed", "judgment": "low",
             "rationale": "Analysis followed the assigned groups."},
        ],
    }
    answer.update(overrides)
    return answer


def extracted_answer(name: str = "Knowledge score") -> dict:
    return {
        "protocol_outcome": name,
        "population": "Medical students",
        "comparison": "AI tutoring versus faculty tutoring",
        "outcome": "Post-test knowledge score",
        "timepoint": "post-test",
        "comparator_type": "active_other",
        "outcome_type": "benefit",
        "sample_size": 120,
        "result": "Mean score was 4.2 points higher with AI tutoring.",
        "effect": {"measure": "mean difference", "basis": "between_group", "value": 4.2,
                   "ci_low": 1.1, "ci_high": 7.3, "units": "points",
                   "interval_type": "confidence", "interval_level": 95},
        "favors": "intervention",
        "direction_rationale": "Higher scores are better.",
        "source_location": {"document_id": "r-1:fulltext", "locator": "table:2",
                            "quote": "Mean difference 4.2 (95% CI 1.1 to 7.3)"},
        "support_rationale": "Table 2 reports the between-group difference.",
    }


def test_the_appraisal_must_be_filled_before_anything_is_submitted():
    staged, remaining = assessment.record("study_appraisal", appraisal_answer(),
                                          proposal(), packet())

    template = staged["stages"]["appraisals"]["records"][0]
    assert template["completion"] == "complete"
    assert template["overall_judgment"] == "some_concerns"
    assert template["domains"]["randomization"]["judgment"] == "some_concerns"
    assert set(remaining) == set(OUTCOMES)


def test_completion_is_derived_rather_than_asserted():
    answer = appraisal_answer(domains=[
        {"name": "randomization", "status": "assessed", "judgment": "low",
         "rationale": "Computer generated."},
        {"name": "deviations", "status": "unavailable", "judgment": "low",
         "rationale": "Methods do not say.", "missing_reason": "not_reported",
         "assessment_basis": "Read the methods section.",
         "inspected_locations": [{"document_id": "r-1:fulltext", "locator": "methods"}]},
    ], overall_judgment="unclear")

    staged, _ = assessment.record("study_appraisal", answer, proposal(), packet())

    domains = staged["stages"]["appraisals"]["records"][0]["domains"]
    assert staged["stages"]["appraisals"]["records"][0]["completion"] == "limited"
    # An unavailable domain cannot assert a judgment, so the answer's word is dropped.
    assert domains["deviations"]["judgment"] == "not_assessed"
    assert domains["deviations"]["missing_reason"] == "not_reported"
    assert domains["deviations"]["inspected_locations"][0]["locator"] == "methods"


def test_every_domain_needs_a_status_and_no_invented_domain_is_accepted():
    with pytest.raises(ValidationError) as error:
        assessment.record("study_appraisal", appraisal_answer(domains=[
            {"name": "randomization", "status": "assessed", "judgment": "low",
             "rationale": "Computer generated."},
        ]), proposal(), packet())
    assert "every domain needs a status" in str(error.value)

    with pytest.raises(ValidationError) as error:
        assessment.record("study_appraisal", appraisal_answer(domains=[
            {"name": "invented", "status": "assessed", "judgment": "low", "rationale": "x"},
        ]), proposal(), packet())
    assert "not a domain of this appraisal method" in str(error.value)


def test_one_extracted_outcome_fills_its_own_row_and_nothing_else():
    staged, remaining = assessment.record("outcome_extracted", extracted_answer(),
                                          proposal(), packet())

    rows = {row["extraction_id"]: row for row in staged["stages"]["extractions"]["records"]}
    filled = rows["result-r-1-o1"]
    assert filled["protocol_outcome"] == "Knowledge score"
    assert filled["effect"]["value"] == 4.2
    assert filled["support_checked"] is True
    assert rows["result-r-1-o2"]["result"] == ""
    decided = staged["stages"]["dispositions"]["records"][0]["outcomes"]
    assert decided[0]["status"] == "extracted"
    assert decided[0]["extraction_ids"] == ["result-r-1-o1"]
    assert decided[1]["status"] == ""
    assert "Learner satisfaction" in remaining


def test_an_unreported_outcome_records_its_rationale_and_inspected_locations():
    staged, remaining = assessment.record("outcome_missing", {
        "protocol_outcome": "Learner satisfaction",
        "status": "not_reported",
        "rationale": "Searches for satisfaction return no hits in the assigned documents.",
        "inspected_locations": [{"document_id": "r-1:fulltext", "locator": "results"}],
    }, proposal(), packet())

    decided = staged["stages"]["dispositions"]["records"][0]["outcomes"][1]
    assert decided["status"] == "not_reported"
    assert decided["extraction_ids"] == []
    assert decided["inspected_locations"] == [{"document_id": "r-1:fulltext",
                                               "locator": "results"}]
    assert "Knowledge score" in remaining


def test_an_outcome_the_protocol_does_not_have_is_refused_by_name():
    with pytest.raises(ValidationError) as error:
        assessment.record("outcome_missing", {
            "protocol_outcome": "Invented outcome", "status": "not_reported",
            "rationale": "Not there.", "inspected_locations": [],
        }, proposal(), packet())

    assert "not one of this record's protocol outcomes" in str(error.value)


def test_nothing_is_left_to_record_once_every_outcome_and_the_appraisal_are_decided():
    staged, _ = assessment.record("study_appraisal", appraisal_answer(), proposal(), packet())
    staged, _ = assessment.record("outcome_extracted", extracted_answer(), staged, packet())
    staged, remaining = assessment.record("outcome_missing", {
        "protocol_outcome": "Learner satisfaction",
        "status": "not_applicable",
        "rationale": "The design cannot measure satisfaction.",
        "inspected_locations": [{"document_id": "r-1:fulltext", "locator": "methods"}],
    }, staged, packet())

    assert remaining == []


def test_an_off_schema_answer_is_refused_before_the_proposal_is_touched():
    original = proposal()
    with pytest.raises(ValidationError) as error:
        assessment.record("outcome_extracted", {**extracted_answer(), "favors": "maybe"},
                          original, packet())

    assert "must be one of" in str(error.value)
    assert original["stages"]["extractions"]["records"][0]["result"] == ""


def test_a_task_without_a_checklist_is_refused():
    with pytest.raises(ValidationError) as error:
        assessment.record("outcome_extracted", extracted_answer(), proposal(), {})

    assert "no outcome checklist" in str(error.value)
