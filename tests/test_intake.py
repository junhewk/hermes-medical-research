"""Plain-language intake: one constrained call, the same validation, and a confirmation gate."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_medical_research import answers, intake, steps
from hermes_medical_research.automation import AutomationEngine
from hermes_medical_research.search.models import FRAMEWORKS, ValidationError
from hermes_medical_research.workspace import digest, normalize_protocol

REQUEST = {
    "framework": "PICO",
    "question": "Does supervised aerobic exercise lower systolic blood pressure in adults?",
    "components": {
        "population": {"groups": [{"label": "adults with hypertension",
                                   "text": "adults with hypertension",
                                   "synonyms": ["essential hypertension"],
                                   "candidate_mesh": ["Hypertension"]}]},
        "intervention": {"groups": [{"label": "aerobic exercise",
                                     "text": "supervised aerobic exercise",
                                     "synonyms": ["endurance training"],
                                     "candidate_mesh": ["Exercise"]}]},
    },
    "search_components": ["population", "intervention"],
    "eligibility": {"include": ["Adults with hypertension"], "exclude": ["Animal studies"]},
    "outcomes": ["Systolic blood pressure"],
    "search_rationale": "Population and intervention carry the recall.",
}


def answering(payload: dict | str, *, record: list[str] | None = None):
    text = payload if isinstance(payload, str) else json.dumps(payload)

    def call(_executable, _home, profile, prompt, kind):
        assert profile == "hmr-intake" and kind == "intake"
        if record is not None:
            record.append(prompt)
        return subprocess.CompletedProcess([], 0, text, "")

    return call


def test_the_intake_schema_speaks_the_request_vocabulary():
    answers.check_keywords(answers.INTAKE_SCHEMA)
    schema = answers.INTAKE_SCHEMA["properties"]
    assert set(schema["framework"]["enum"]) == set(FRAMEWORKS)
    union = {key for required, optional in FRAMEWORKS.values() for key in (*required, *optional)}
    assert set(answers.COMPONENT_KEYS) == union
    assert set(schema["components"]["properties"]) == union


def test_the_prompt_names_each_frameworks_own_components(tmp_path):
    record: list[str] = []
    intake.propose(tmp_path, "Any question at all.", call=answering(REQUEST, record=record))

    prompt = record[0]
    # The schema must allow every framework's keys, so the prompt is what scopes them.
    assert "PICO: population, intervention" in prompt
    assert "DIAGNOSTIC: population, index_test, target_condition" in prompt
    assert "Any question at all." in prompt


def test_a_valid_draft_is_shown_with_its_digest_and_creates_nothing(tmp_path):
    draft = intake.propose(tmp_path, "Does exercise lower blood pressure?",
                           call=answering(REQUEST))

    assert draft["state"] == "valid"
    expected = normalize_protocol(
        {**REQUEST, "schema_version": "3"}, mode="report", records=None, fulltexts=None,
        language="en",
    )
    assert draft["protocol_digest"] == digest(expected)
    assert AutomationEngine(tmp_path).list_reviews()["reviews"] == []
    text = intake.render(draft)
    assert "PICO" in text and "Systolic blood pressure" in text
    assert f"--confirm {draft['draft_id']} --name SLUG" in text
    assert json.loads(Path(draft["request_file"]).read_text())["schema_version"] == "3"


def test_empty_component_blocks_are_dropped_before_validation(tmp_path):
    """One schema serves five frameworks, so a model can answer keys its framework forbids."""
    padded = {
        **REQUEST,
        "components": {
            **REQUEST["components"],
            "index_test": {"groups": [{"label": "", "text": "  ", "synonyms": [],
                                       "candidate_mesh": []}]},
            "concept": {"groups": []},
        },
    }

    draft = intake.propose(tmp_path, "Does exercise lower blood pressure?",
                           call=answering(padded))

    assert draft["state"] == "valid"
    assert set(draft["protocol"]["question"]["components"]) == {"population", "intervention"}


def test_a_component_with_real_text_the_framework_forbids_is_refused(tmp_path):
    wrong = {
        **REQUEST,
        "components": {
            **REQUEST["components"],
            "index_test": {"groups": [{"label": "a test", "text": "an index test",
                                       "synonyms": [], "candidate_mesh": []}]},
        },
    }

    draft = intake.propose(tmp_path, "Does exercise lower blood pressure?",
                           call=answering(wrong))

    assert draft["state"] == "invalid"
    assert "index_test" in draft["errors"][0]


def test_an_unusable_answer_keeps_the_words_and_prints_the_recovery_commands(tmp_path):
    draft = intake.propose(tmp_path, "Does exercise lower blood pressure?",
                           call=answering("I would need more detail to answer that."))

    assert draft["state"] == "invalid"
    assert draft["attempts"] == intake.ATTEMPTS
    assert draft["text"] == "Does exercise lower blood pressure?"
    text = intake.render(draft)
    assert "could not be validated" in text
    assert f"--revise {draft['draft_id']}" in text
    assert "review create --request" in text


def test_a_revision_keeps_the_original_words_and_adds_the_clarification(tmp_path):
    record: list[str] = []
    first = intake.propose(tmp_path, "Compare exercise with usual care.",
                           call=answering("not json"))

    revised = intake.revise(
        tmp_path, first["draft_id"], "Adults with hypertension only.",
        call=answering(REQUEST, record=record),
    )

    assert revised["draft_id"] == first["draft_id"]
    assert revised["state"] == "valid"
    assert revised["clarifications"] == ["Adults with hypertension only."]
    assert "Compare exercise with usual care." in record[0]
    assert "The researcher adds: Adults with hypertension only." in record[0]


def test_confirm_creates_the_review_and_points_the_steps_at_it(tmp_path):
    draft = intake.propose(tmp_path, "Does exercise lower blood pressure?",
                           call=answering(REQUEST))

    created = intake.confirm(tmp_path, draft["draft_id"], name="exercise-bp",
                             timezone_name="Asia/Seoul")

    assert created["name"] == "exercise-bp"
    assert steps.active_review(tmp_path) == "exercise-bp"
    assert [item["name"] for item in AutomationEngine(tmp_path).list_reviews()["reviews"]] == [
        "exercise-bp"
    ]
    with pytest.raises(ValidationError) as error:
        intake.confirm(tmp_path, draft["draft_id"], name="second-try",
                       timezone_name="Asia/Seoul")
    assert "already created review" in str(error.value)


def test_confirm_refuses_a_request_edited_after_it_was_shown(tmp_path):
    draft = intake.propose(tmp_path, "Does exercise lower blood pressure?",
                           call=answering(REQUEST))
    path = Path(draft["request_file"])
    edited = json.loads(path.read_text())
    edited["eligibility"]["include"] = ["Anyone at all"]
    path.write_text(json.dumps(edited))

    with pytest.raises(ValidationError) as error:
        intake.confirm(tmp_path, draft["draft_id"], name="exercise-bp",
                       timezone_name="Asia/Seoul")

    assert "changed after it was shown" in str(error.value)
    assert AutomationEngine(tmp_path).list_reviews()["reviews"] == []


def test_an_invalid_draft_cannot_be_confirmed(tmp_path):
    draft = intake.propose(tmp_path, "Something vague.", call=answering("prose"))

    with pytest.raises(ValidationError) as error:
        intake.confirm(tmp_path, draft["draft_id"], name="vague", timezone_name="Asia/Seoul")

    assert "did not validate" in str(error.value)


def test_drafts_are_listed_and_discarded(tmp_path):
    draft = intake.propose(tmp_path, "Does exercise lower blood pressure?",
                           call=answering(REQUEST))

    listed = intake.list_drafts(tmp_path)["drafts"]
    assert [item["draft_id"] for item in listed] == [draft["draft_id"]]
    assert listed[0]["state"] == "valid"

    intake.discard(tmp_path, draft["draft_id"])
    assert intake.list_drafts(tmp_path)["drafts"] == []
    with pytest.raises(ValidationError):
        intake.read_draft(tmp_path, draft["draft_id"])


def test_empty_words_are_refused_before_any_model_call(tmp_path):
    with pytest.raises(ValidationError):
        intake.propose(tmp_path, "   ",
                       call=lambda *a, **k: pytest.fail("no call for an empty request"))
