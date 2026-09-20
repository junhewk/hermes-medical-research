"""Deterministic arithmetic checks over recorded evidence.

These decide, with no model call, the claims a machine can settle: whether a direction is
compatible with its own interval, whether an interval brackets its estimate, whether a value is
possible for its measure family, and whether a recorded sample size is traceable to the record's
own documents.  Measured on the 170-record production review the set runs in 61 ms over 64
extraction rows, and finds four rows claiming a direction their own confidence interval
contradicts -- the same class of defect the eight-hour model audit caught, by comparison operators.

There is deliberately no check that one record reports one sample size.  It looked obvious and it
is false: on the real review a pooled systematic review recorded 1592 and 205 participants for
knowledge and 726 and 110 for clinical skills, because a different meta-analysis pools a different
set of studies.  It flagged 36 of 64 rows and every one was correct work.

They report rather than reject.  An arithmetic flag is a finding about recorded evidence, and
existing Runs carry rows that violate these rules; turning them into submit-time rejections would
invalidate finished work rather than describe it.

A recorded number is one of three things, and the distinction is what makes or breaks a check:

* **transcribed** -- it must occur in the cited text, and a strict check is right;
* **derived** -- computed from stated numbers, such as a total summed from two arms or a
  percentage from a count over a denominator.  It will never occur verbatim, and a strict check
  calls correct work a defect.  Of about thirty flags raised by a first, naive version, nearly
  every one was a derived value: ``n = 297`` from "control, n = 150 ... experimental, n = 147".
* **interpretive** -- a label, a direction in words, a prose summary.  Arithmetic cannot settle it.

So the checks below that need no text at all are preferred, and the one that reads text accepts a
value that is *derivable* from what the text states.
"""

from __future__ import annotations

import re
from typing import Any

from .workspace import Workspace

#: A measure whose null value is one rather than zero.
RATIO_MEASURE = re.compile(r"\b(odds|risk|hazard|rate)\s+ratio\b|\b(or|rr|hr)\b", re.IGNORECASE)
#: A standardised effect; anything beyond this magnitude is a transcription slip, not a result.
STANDARDISED = re.compile(r"cohen|standardi[sz]ed|\bsmd\b", re.IGNORECASE)
STANDARDISED_LIMIT = 5.0
_NUMBER = re.compile(r"[-−]?\d+(?:\.\d+)?")
_TOLERANCE = 0.011


def numbers_in(text: str | None) -> list[float]:
    """Every number in ``text``, tolerating a unicode minus."""
    found = []
    for token in _NUMBER.findall(text or ""):
        try:
            found.append(float(token.replace("−", "-")))
        except ValueError:
            continue
    return found


def _close(left: float, right: float, tolerance: float = _TOLERANCE) -> bool:
    return abs(left - right) <= tolerance


def derivable(value: float | int | None, text: str | None) -> bool:
    """Whether ``value`` is stated in ``text`` or follows from numbers that are.

    Derivations are deliberately few and obvious -- a sum, a difference, and a percentage of one
    number by another -- because each extra rule buys a little coverage and a lot of false
    confidence.  Anything less obvious belongs to a reader, not to this function.
    """
    if value is None:
        return False
    target = float(value)
    present = numbers_in(text)
    if any(_close(item, target) or _close(abs(item), abs(target)) for item in present):
        return True
    for index, first in enumerate(present):
        for second in present[index + 1:]:
            if _close(first + second, target) or _close(abs(first - second), abs(target)):
                return True
            if second and _close(first / second * 100.0, target, tolerance=0.11):
                return True
            if first and _close(second / first * 100.0, target, tolerance=0.11):
                return True
    return False


def null_value(measure: str | None) -> float:
    """The value at which a measure shows no effect."""
    return 1.0 if RATIO_MEASURE.search(measure or "") else 0.0


def spans_null(effect: dict[str, Any]) -> bool:
    """Whether this effect's interval contains its own null, so no direction is supported."""
    low, high = effect.get("ci_low"), effect.get("ci_high")
    if low is None or high is None:
        return False
    return min(low, high) <= null_value(effect.get("measure")) <= max(low, high)


def _flag(check: str, row: dict[str, Any], detail: str) -> dict[str, Any]:
    return {
        "check": check,
        "extraction_id": row.get("extraction_id"),
        "record_id": row.get("record_id"),
        "protocol_outcome": row.get("protocol_outcome"),
        "detail": detail,
    }


def check_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every arithmetic flag these rows raise. No text and no model is consulted."""
    flags: list[dict[str, Any]] = []
    for row in rows:
        effect = row.get("effect") or {}
        low, high, value = effect.get("ci_low"), effect.get("ci_high"), effect.get("value")
        measure = effect.get("measure") or ""

        if row.get("favors") in {"intervention", "comparator"} and spans_null(effect):
            flags.append(_flag(
                "direction_without_interval", row,
                f"favors {row['favors']} while the interval {low} to {high} contains "
                f"{null_value(measure)}",
            ))
        if None not in (low, high, value) and not min(low, high) <= value <= max(low, high):
            flags.append(_flag(
                "interval_excludes_estimate", row,
                f"estimate {value} lies outside {low} to {high}",
            ))
        if value is not None:
            if RATIO_MEASURE.search(measure) and value <= 0:
                flags.append(_flag(
                    "implausible_value", row, f"{measure!r} cannot be {value}"))
            elif STANDARDISED.search(measure) and abs(value) > STANDARDISED_LIMIT:
                flags.append(_flag(
                    "implausible_value", row,
                    f"standardised effect {value} exceeds {STANDARDISED_LIMIT}",
                ))
        if effect.get("interval_type") in {"confidence", "credible"} and low is None \
                and high is None:
            flags.append(_flag(
                "interval_declared_without_bounds", row,
                f"interval_type {effect['interval_type']!r} with no bounds",
            ))

    return flags


def check_workspace(workspace: Workspace) -> list[dict[str, Any]]:
    """Arithmetic flags for a Run, plus the one check that reads the cited text."""
    rows = workspace.rows("extractions")
    flags = check_rows(rows)
    documents = workspace.source_index()
    for row in rows:
        size = row.get("sample_size")
        if size is None:
            continue
        record_id = row.get("record_id")
        text = " ".join(
            segment.get("text", "")
            for document in documents.values()
            if document.get("record_id") == record_id
            for segment in document.get("segments", [])
        )
        # Only a Run whose documents are stored can answer this, and a derived total counts: the
        # first version of this check called `n = 297` a defect when the abstract said
        # "control, n = 150 ... experimental, n = 147".
        if text and not derivable(size, text):
            flags.append(_flag(
                "sample_size_untraceable", row,
                f"{size} is neither stated nor derivable in this record's documents",
            ))
    return flags
