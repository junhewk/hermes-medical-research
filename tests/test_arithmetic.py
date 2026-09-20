"""Arithmetic checks over recorded evidence, and the mutation suite that proves they bite.

A check that cannot fail passes everything, which is the code version of the audit answering
`supported` 188 times and `unsupported` never. So every check here is tested twice: it must accept
a clean row, and it must reject a row corrupted in the one field it claims to cover.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from hermes_medical_research import arithmetic


def row(**overrides):
    base = {
        "extraction_id": "result-r-1-o1",
        "record_id": "r-1",
        "protocol_outcome": "Knowledge score",
        "sample_size": 80,
        "favors": "intervention",
        "result": "The intervention group scored 4.2 points higher.",
        "effect": {
            "measure": "mean difference",
            "basis": "between_group",
            "value": 4.2,
            "ci_low": 1.1,
            "ci_high": 7.3,
            "units": "points",
            "interval_type": "confidence",
            "interval_level": 95,
        },
    }
    effect = overrides.pop("effect", None)
    merged = {**base, **overrides}
    if effect:
        merged["effect"] = {**base["effect"], **effect}
    return merged


def codes(rows):
    return {flag["check"] for flag in arithmetic.check_rows(rows)}


def test_a_clean_row_raises_nothing():
    assert arithmetic.check_rows([row()]) == []


def test_a_direction_cannot_be_claimed_when_the_interval_contains_the_null():
    """Measured on the real review: four recorded rows claim a direction their own CI contradicts.

    This is the same class of defect the eight-hour model audit found, decided here by two
    comparisons.
    """
    flagged = arithmetic.check_rows([row(effect={"ci_low": -2.7, "ci_high": 4.4})])

    assert [flag["check"] for flag in flagged] == ["direction_without_interval"]
    assert "-2.7" in flagged[0]["detail"] and "4.4" in flagged[0]["detail"]
    # Claiming no direction over the same interval is honest, so it passes.
    assert arithmetic.check_rows([row(favors="neither",
                                      effect={"ci_low": -2.7, "ci_high": 4.4})]) == []


def test_a_ratio_measure_uses_one_as_its_null():
    spanning = {"measure": "odds ratio", "value": 1.4, "ci_low": 0.8, "ci_high": 2.6}
    assert "direction_without_interval" in codes([row(effect=spanning)])
    # The same bounds are decisive for a difference, where the null is zero.
    clear = {"measure": "mean difference", "value": 1.4, "ci_low": 0.8, "ci_high": 2.6}
    assert "direction_without_interval" not in codes([row(effect=clear)])


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("ci_low", 5.0, "interval_excludes_estimate"),
        ("ci_high", 1.5, "interval_excludes_estimate"),
        ("interval_type", "confidence", None),
    ],
)
def test_an_interval_must_bracket_its_own_estimate(field, value, expected):
    mutated = row(effect={field: value})
    if expected is None:
        assert "interval_excludes_estimate" not in codes([mutated])
    else:
        assert expected in codes([mutated])


def test_a_value_impossible_for_its_measure_is_flagged():
    assert "implausible_value" in codes([row(effect={"measure": "odds ratio", "value": -0.4,
                                                     "ci_low": -2.0, "ci_high": 1.0})])
    assert "implausible_value" in codes([row(effect={"measure": "Cohen's d", "value": 47.0,
                                                     "ci_low": 40.0, "ci_high": 55.0})])
    assert "implausible_value" not in codes([row(effect={"measure": "Cohen's d", "value": 0.4})])


def test_a_declared_interval_needs_bounds():
    assert "interval_declared_without_bounds" in codes(
        [row(favors="neither", effect={"ci_low": None, "ci_high": None})]
    )
    assert "interval_declared_without_bounds" not in codes(
        [row(favors="neither", effect={"interval_type": "none", "ci_low": None, "ci_high": None})]
    )


def test_sample_size_may_differ_between_outcomes_of_one_record():
    """It looked like an obvious invariant and it is false.

    A pooled systematic review on the real review recorded 1592 and 205 participants for knowledge
    and 726 and 110 for clinical skills, because a different meta-analysis pools a different set of
    studies. Treating that as a conflict flagged 36 of 64 rows, every one of them correct work.
    """
    rows = [row(), row(extraction_id="result-r-1-o2", protocol_outcome="Skills", sample_size=94)]

    assert arithmetic.check_rows(rows) == []


class TestDerivable:
    """A derived number never occurs verbatim, and calling that a defect is the naive mistake.

    A first version of the sample-size check raised about thirty flags on the real review and
    nearly every one was derived: `n = 297` where the abstract said "control, n = 150 ...
    experimental, n = 147".
    """

    ABSTRACT = "Undergraduate students from the 2022 cohort (control, n = 150) and the 2023 " \
               "cohort (experimental, n = 147) participated."

    def test_a_stated_number_is_derivable(self):
        assert arithmetic.derivable(150, self.ABSTRACT)

    def test_a_total_summed_from_its_arms_is_derivable(self):
        assert arithmetic.derivable(297, self.ABSTRACT)

    def test_a_percentage_of_a_count_is_derivable(self):
        assert arithmetic.derivable(62.5, "Exceeds expectation in 25 of 40 cases.")

    def test_an_unrelated_number_is_not(self):
        assert not arithmetic.derivable(412, self.ABSTRACT)
        assert not arithmetic.derivable(None, self.ABSTRACT)

    def test_a_unicode_minus_still_reads_as_negative(self):
        assert arithmetic.derivable(-2.2, "The difference was −2.2 points.")


def test_every_check_rejects_a_row_corrupted_in_the_field_it_covers():
    """The mutation suite. A check that accepts its own mutant is not a check."""
    mutations = {
        "direction_without_interval": {"effect": {"ci_low": -9.0, "ci_high": 9.0}},
        "interval_excludes_estimate": {"effect": {"value": 99.0}},
        "implausible_value": {"effect": {"measure": "Cohen's d", "value": 47.0}},
        "interval_declared_without_bounds": {"effect": {"ci_low": None, "ci_high": None}},
    }
    clean = row()
    assert arithmetic.check_rows([clean]) == []

    for check, overrides in mutations.items():
        mutant = deepcopy(clean)
        mutant["effect"].update(overrides["effect"])
        assert check in codes([mutant]), f"{check} accepted its own mutant"
