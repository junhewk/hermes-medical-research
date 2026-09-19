"""Constrained answer shapes for the task kinds a model can decide in one call.

Each shape is both the request's ``response_format`` schema, which the model server compiles into a
grammar, and the contract the prompt states in words, rendered from the same schema so the two
cannot drift.  A session that answers this way has no tools at all: a schema and a tool list cannot
be combined on this gateway, and the same schema in a tool's ``parameters`` costs extra model turns
because Hermes proxies a tool behind its own meta-tools.  The typed submit tools in ``mcp_server``
carry the identical shapes for the roles that must keep their tools.

Schemas buy *shape* only.  Every value, identifier, digest and cross-field rule is still checked by
``validation`` and ``evidence`` when the mapped proposal reaches ``TaskEngine.submit``; these
schemas are deliberately protocol-independent, because a profile's ``extra_body`` is fixed at
bootstrap time and cannot know a Review's outcomes or record ids.

Types: ``object``, ``array``, ``string``, ``boolean``, ``integer`` and ``number``.  Keyword subset:
``type``, ``properties``, ``required``, ``additionalProperties``, ``enum``,
``items``, ``minItems``, ``maxItems``, ``minLength`` and ``maxLength``.  That is what llama.cpp's
converter handles predictably, and what :func:`check_shape` implements, so prose, grammar and the
local check cannot drift apart.
"""

from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256
from typing import Any

from .evidence import BASES, COMPARATORS
from .search.models import FRAMEWORKS, SOURCES
from .validation import GRADE_DOMAINS, RELATIONSHIPS, ValidationError

ALLOWED_KEYWORDS = frozenset(
    {"type", "properties", "required", "additionalProperties", "enum", "items",
     "minItems", "maxItems", "minLength", "maxLength"}
)

CLAIM_BASES = ("comparative", "within_group", "association", "diagnostic_accuracy", "ranking",
               "context", "gap")
CERTAINTY_RATINGS = ("high", "moderate", "low", "very-low", "not-assessable", "not-applicable")
OVERLAP_STATUSES = ("mapped", "not_applicable", "suspected", "unknown")
STUDY_KINDS = ("primary", "systematic-review", "guideline", "other")

_TEXT = {"type": "string"}
# A reason is one sentence.  Without a ceiling the model writes paragraphs, and on a local model
# that is the difference between seconds and minutes per record: one measured screening answer ran
# 111 seconds unbounded.
_REASON = {"type": "string", "minLength": 1, "maxLength": 300}
_SENTENCE = {"type": "string", "minLength": 1, "maxLength": 600}


def _object(required: list[str], properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


_CONTRIBUTION = _object(
    ["extraction_id", "use", "relationship", "weight_rationale", "alignment_rationale",
     "claim_support_checked"],
    {
        "extraction_id": _TEXT,
        "use": {"type": "string", "enum": ["direct", "indirect", "context"]},
        "relationship": {"type": "string", "enum": sorted(RELATIONSHIPS)},
        "weight_rationale": _TEXT,
        "alignment_rationale": _TEXT,
        "claim_support_checked": {"type": "boolean"},
    },
)

_CERTAINTY = _object(
    ["framework", "rating", "rationale", "starting_point", "rating_explanation", "domains"],
    {
        "framework": {"type": "string", "enum": ["GRADE-informed", "descriptive"]},
        "rating": {"type": "string", "enum": list(CERTAINTY_RATINGS)},
        "rationale": _TEXT,
        "starting_point": _TEXT,
        "rating_explanation": _TEXT,
        "domains": _object(list(GRADE_DOMAINS), {key: _TEXT for key in GRADE_DOMAINS}),
    },
)

RESULT_SCHEMAS: dict[str, dict[str, Any]] = {
    "screening": _object(
        ["decision", "reason"],
        {
            "decision": {"type": "string", "enum": ["include", "exclude", "uncertain"]},
            "reason": _REASON,
            "basis": {"type": "string", "enum": ["title-abstract", "fulltext", "registry"]},
        },
    ),
    "coverage": _object(
        ["selection", "reason", "protocol_outcomes"],
        {
            "selection": {"type": "string", "enum": ["selected", "deferred", "unavailable"]},
            "reason": _REASON,
            "protocol_outcomes": {"type": "array", "maxItems": 24, "items": _TEXT},
        },
    ),
    "studies": _object(
        ["kind", "basis"],
        {
            "kind": {"type": "string", "enum": list(STUDY_KINDS)},
            "basis": _REASON,
            # "" keeps this record as its own study; any other value must name an existing study.
            "same_study_id": _TEXT,
        },
    ),
    "synthesis": _object(
        ["title", "limitations", "population", "comparison", "timepoint", "claim_basis",
         "comparator_type", "conclusion", "evidence", "overlap", "certainty"],
        {
            "title": _TEXT,
            "limitations": {"type": "array", "maxItems": 12, "items": _TEXT},
            "population": _TEXT,
            "comparison": _TEXT,
            "timepoint": _TEXT,
            "claim_basis": {"type": "string", "enum": list(CLAIM_BASES)},
            "comparator_type": {"type": "string", "enum": sorted(COMPARATORS)},
            "conclusion": _SENTENCE,
            "gap_reason": _TEXT,
            "gap_basis": _TEXT,
            "evidence": {"type": "array", "maxItems": 24, "items": _CONTRIBUTION},
            "overlap": _object(
                ["status", "rationale"],
                {"status": {"type": "string", "enum": list(OVERLAP_STATUSES)},
                 "rationale": _TEXT},
            ),
            "certainty": _CERTAINTY,
        },
    ),
}


# -- assessment, decided one protocol outcome at a time -----------------------------------------
#
# An assessment used to be one hand-edited file. On the host that cost about ten minutes of
# generation for a 26 KB rewrite, and the first submission was refused for a single missing
# rationale. These shapes let a session record one outcome per call instead, so a rejection costs
# one small answer rather than the whole file.

_LOCATION = _object(
    ["document_id", "locator"],
    {"document_id": {"type": "string", "minLength": 1},
     "locator": {"type": "string", "minLength": 1}},
)
_EFFECT = _object(
    ["measure", "basis", "units", "interval_type"],
    {
        "measure": {"type": "string", "minLength": 1, "maxLength": 120},
        "basis": {"type": "string", "enum": sorted(BASES)},
        "value": {"type": "number"},
        "ci_low": {"type": "number"},
        "ci_high": {"type": "number"},
        "units": {"type": "string", "minLength": 1, "maxLength": 60},
        "interval_type": {"type": "string", "enum": ["confidence", "credible", "none"]},
        "interval_level": {"type": "number"},
        "missing_reason": {"type": "string", "maxLength": 300},
    },
)
_QUOTED_LOCATION = _object(
    ["document_id", "locator", "quote"],
    {"document_id": {"type": "string", "minLength": 1},
     "locator": {"type": "string", "minLength": 1},
     "quote": {"type": "string", "minLength": 1, "maxLength": 400}},
)
_DOMAIN = _object(
    ["name", "status", "judgment", "rationale"],
    {
        "name": {"type": "string", "minLength": 1, "maxLength": 60},
        "status": {"type": "string", "enum": ["pending", "assessed", "unavailable"]},
        "judgment": {"type": "string", "minLength": 1, "maxLength": 60},
        "rationale": _REASON,
        # An assessed domain must cite where it read the judgment; an unavailable one says instead
        # what it inspected and why nothing was there.
        "source_locations": {"type": "array", "maxItems": 8, "items": _QUOTED_LOCATION},
        "assessment_basis": {"type": "string", "maxLength": 300},
        "missing_reason": {"type": "string",
                           "enum": ["access_unavailable", "not_reported", "insufficient_detail"]},
        "inspected_locations": {"type": "array", "maxItems": 12, "items": _LOCATION},
    },
)



# One audit group, answered in one call.  The payload deliberately does not carry `field`, `check`
# or `assertion`: the validator compares all three byte-for-byte against the target, and making the
# model echo them meant reproducing an assertion that reached 14 KB on a real finding, which no
# 4096-token answer can hold.  `apply_result` copies them from the task's own template instead, so
# the echo is exact by construction and the model spends its tokens on the judgment.
_AUDIT_OBSERVATION = _object(
    ["target_id", "verdict", "rationale"],
    {
        "target_id": {"type": "string", "minLength": 1, "maxLength": 80},
        "verdict": {"type": "string", "enum": ["supported", "unsupported", "uncertain"]},
        "rationale": _REASON,
        "sources": {"type": "array", "maxItems": 6, "items": _QUOTED_LOCATION},
    },
)
_AUDIT_MEMBERSHIP = _object(
    ["target_id", "supported_scope", "rationale"],
    {
        "target_id": {"type": "string", "minLength": 1, "maxLength": 80},
        "supported_scope": {
            "type": "string",
            "enum": ["outcome", "review", "unsupported", "unknown"],
        },
        "rationale": _REASON,
        "sources": {"type": "array", "maxItems": 6, "items": _QUOTED_LOCATION},
    },
)
# Only the verdicts.  Every status the old contract asked for -- the record's, each review check's,
# each report row's -- follows from them by rule, and those cross-field rules are precisely what a
# returned audit kept violating: "unsupported or uncertain requires revision" was refused over and
# over.  A rule a machine can apply is applied by `_apply_audit`, not asked of a model.
RESULT_SCHEMAS["audit"] = _object(
    ["observations"],
    {
        "observations": {"type": "array", "maxItems": 24, "items": _AUDIT_OBSERVATION},
        "memberships": {"type": "array", "maxItems": 12, "items": _AUDIT_MEMBERSHIP},
    },
)

CALL_KINDS = tuple(RESULT_SCHEMAS)

ASSESSMENT_SCHEMAS: dict[str, dict[str, Any]] = {
    "study_appraisal": _object(
        ["overall_judgment", "overall", "rationale", "domains"],
        {
            "overall_judgment": {
                "type": "string",
                "enum": ["low", "some_concerns", "high", "unclear", "not_assessable",
                         "descriptive"],
            },
            "overall": _REASON,
            "rationale": _SENTENCE,
            "domains": {"type": "array", "minItems": 1, "maxItems": 12, "items": _DOMAIN},
        },
    ),
    "outcome_extracted": _object(
        ["protocol_outcome", "population", "comparison", "outcome", "timepoint",
         "comparator_type", "outcome_type", "result", "effect", "favors",
         "direction_rationale", "source_location", "support_rationale"],
        {
            "protocol_outcome": {"type": "string", "minLength": 1, "maxLength": 200},
            "population": {"type": "string", "minLength": 1, "maxLength": 200},
            "comparison": {"type": "string", "minLength": 1, "maxLength": 200},
            "outcome": {"type": "string", "minLength": 1, "maxLength": 200},
            "timepoint": {"type": "string", "minLength": 1, "maxLength": 120},
            "comparator_type": {"type": "string", "enum": sorted(COMPARATORS)},
            "outcome_type": {"type": "string", "enum": ["benefit", "harm", "context"]},
            "sample_size": {"type": "integer"},
            "result": _SENTENCE,
            "effect": _EFFECT,
            "favors": {"type": "string",
                       "enum": ["intervention", "comparator", "neither", "uncertain",
                                "not-applicable"]},
            "direction_rationale": _REASON,
            "source_location": _object(
                ["document_id", "locator", "quote"],
                {"document_id": {"type": "string", "minLength": 1},
                 "locator": {"type": "string", "minLength": 1},
                 "quote": {"type": "string", "minLength": 1, "maxLength": 600}},
            ),
            "support_rationale": _REASON,
        },
    ),
    "outcome_missing": _object(
        ["protocol_outcome", "status", "rationale", "inspected_locations"],
        {
            "protocol_outcome": {"type": "string", "minLength": 1, "maxLength": 200},
            "status": {"type": "string", "enum": ["not_reported", "not_applicable"]},
            "rationale": _REASON,
            "inspected_locations": {"type": "array", "maxItems": 12, "items": _LOCATION},
        },
    ),
}


# Intake is not a Task kind: it turns the operator's plain words into a request the CLI validates
# before any Review exists.  It has an answer schema all the same, so the same machinery applies.
_GROUP = _object(
    ["label", "text", "synonyms", "candidate_mesh"],
    {
        "label": {"type": "string", "minLength": 1, "maxLength": 80},
        "text": {"type": "string", "minLength": 1, "maxLength": 200},
        "synonyms": {"type": "array", "maxItems": 12, "items": {"type": "string",
                                                                "maxLength": 120}},
        "candidate_mesh": {"type": "array", "maxItems": 6, "items": {"type": "string",
                                                                     "maxLength": 120}},
    },
)
_BLOCK = _object(
    ["groups"],
    {
        "operator": {"type": "string", "enum": ["all", "any"]},
        "groups": {"type": "array", "minItems": 1, "maxItems": 8, "items": _GROUP},
    },
)
# The union of every framework's components, so one static schema serves all five.
COMPONENT_KEYS = tuple(dict.fromkeys(
    key for required, optional in FRAMEWORKS.values() for key in (*required, *optional)
))
INTAKE_SCHEMA = _object(
    ["framework", "question", "components", "search_components", "eligibility", "outcomes",
     "search_rationale"],
    {
        "framework": {"type": "string", "enum": sorted(FRAMEWORKS)},
        "question": {"type": "string", "minLength": 1, "maxLength": 400},
        "components": _object([], {key: _BLOCK for key in COMPONENT_KEYS}),
        "search_components": {"type": "array", "minItems": 1, "maxItems": 5,
                              "items": {"type": "string", "enum": list(COMPONENT_KEYS)}},
        "sources": {"type": "array", "maxItems": 7,
                    "items": {"type": "string", "enum": sorted(SOURCES)}},
        "eligibility": _object(
            ["include", "exclude"],
            {
                "include": {"type": "array", "minItems": 1, "maxItems": 12,
                            "items": {"type": "string", "minLength": 1, "maxLength": 300}},
                "exclude": {"type": "array", "maxItems": 12,
                            "items": {"type": "string", "minLength": 1, "maxLength": 300}},
            },
        ),
        "outcomes": {"type": "array", "maxItems": 12,
                     "items": {"type": "string", "minLength": 1, "maxLength": 200}},
        "search_rationale": {"type": "string", "minLength": 1, "maxLength": 600},
    },
)
SCHEMAS: dict[str, dict[str, Any]] = {**RESULT_SCHEMAS, "intake": INTAKE_SCHEMA}

# An audit group answers up to 24 targets, each a verdict plus one sentence and a short quote, so
# it needs more room than a single decision and far less than a synthesis.
MAX_TOKENS = {"screening": 768, "coverage": 768, "studies": 768, "synthesis": 3072,
              "audit": 3072, "intake": 4096}

INSTRUCTIONS = {
    "intake": (
        "You turn a researcher's request into one structured review protocol. Choose the framework "
        "the question fits. Break the question into components, and give each component group the "
        "synonyms and candidate MeSH headings a high-recall search needs. State eligibility "
        "criteria the way a screener can apply them to a title and abstract, and list the outcomes "
        "the review will report. Add nothing the request does not support: a criterion nobody "
        "asked for silently narrows the review."
    ),
    "screening": (
        "You screen one bibliographic record for a medical evidence review. Apply every "
        "eligibility criterion below to the record's title and abstract. Judge only this record. "
        "A record whose abstract is missing or uninformative is uncertain, not excluded. Name the "
        "deciding criterion in one sentence; do not restate the record."
    ),
    "coverage": (
        "You decide whether one included record goes on to detailed assessment. Eligibility is "
        "already settled and must not be revisited. Name the protocol outcomes this record can "
        "answer, and give one sentence of reason."
    ),
    "studies": (
        "You decide whether one record reports a study already linked in this review. Link only on "
        "explicit identity evidence such as a shared registration number or a stated companion "
        "report. Otherwise leave it as its own study, and say in one sentence what decided it."
    ),
    "synthesis": (
        "You write one finding for one protocol outcome from the extraction and appraisal rows "
        "below, and from the unreported outcome decisions that tell you what is missing. Cite only "
        "the extraction ids shown, and keep every rationale to one sentence."
    ),
    "audit": (
        "You independently check one frozen group of assertions and never correct the work. For "
        "every target, decide whether the source text below supports what the assertion claims, "
        "and say so in one sentence. Quote only from the segments shown, exactly as they read; a "
        "quote that is not in them will be refused. An assertion you cannot confirm from the text "
        "is `uncertain`, and one the text contradicts is `unsupported`; either one makes the group "
        "`revise`. Answer every target once, by its `target_id`, and answer nothing else."
    ),
}

# Packet keys whose content is the same for every item of one Cycle, so they belong in the cached
# prompt prefix.  Everything else is the item itself and goes last.
STABLE_PACKET_KEYS = {
    "intake": (),
    "screening": ("eligibility",),
    "coverage": ("outcomes",),
    "studies": (),
    "synthesis": ("field_rules",),
    "audit": ("citation_contract",),
}


def response_format(kind: str) -> dict[str, Any]:
    """The request field that constrains a tool-free answer to one kind's shape.

    ``json_object`` with a schema is the form both llama.cpp and the OpenAI-compatible servers
    accept, and it is the only way to constrain a session that has no tools: a schema and a tool
    list cannot be combined on this gateway, which returns HTTP 400 for the pair.
    """
    return {"type": "json_object", "schema": SCHEMAS[kind]}


def read_object(text: str) -> Any:
    """Parse one answer without checking it, tolerating a fence or a trailing sentence."""
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        stripped = stripped[stripped.index("{"):] if "{" in stripped else stripped
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return _first_object(stripped)


def parse_answer(kind: str, text: str) -> dict[str, Any]:
    """Read one constrained answer and check it against its schema."""
    value = read_object(text)
    check_shape(value, SCHEMAS[kind])
    return value


def _first_object(text: str) -> Any:
    """The first brace-balanced object in the text, so a stray preface is survivable."""
    depth = 0
    start = -1
    for index, character in enumerate(text):
        if character == "{":
            depth += 1
            if depth == 1:
                start = index
        elif character == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    return json.loads(text[start:index + 1])
                except json.JSONDecodeError:
                    start = -1
    raise ValidationError(
        "the answer was not one JSON object; return only the object the contract describes"
    )


def schema_digest(kind: str) -> str:
    return sha256(canonical(response_format(kind)).encode()).hexdigest()


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def check_keywords(schema: Any) -> None:
    """Refuse a schema keyword the grammar converter or :func:`check_shape` would silently skip."""
    if isinstance(schema, dict):
        unknown = set(schema) - ALLOWED_KEYWORDS
        if unknown:
            raise ValidationError(f"unsupported schema keywords: {sorted(unknown)}")
        for key in ("properties", "items"):
            value = schema.get(key)
            if isinstance(value, dict):
                for member in (value.values() if key == "properties" else [value]):
                    check_keywords(member)


def check_shape(value: Any, schema: dict[str, Any], path: str = "result") -> None:
    """Validate a tool payload against the keyword subset, with messages a model can act on."""
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            raise ValidationError(f"{path} must be an object")
        for key in schema.get("required", []):
            if key not in value:
                raise ValidationError(f"{path}.{key} is required")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                raise ValidationError(
                    f"{path} has unknown field {extra[0]!r}; allowed fields are "
                    f"{sorted(properties)}"
                )
        for key, member in value.items():
            if key in properties:
                check_shape(member, properties[key], f"{path}.{key}")
        return
    if expected == "array":
        if not isinstance(value, list):
            raise ValidationError(f"{path} must be an array")
        if len(value) < schema.get("minItems", 0):
            raise ValidationError(f"{path} needs at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ValidationError(f"{path} allows at most {schema['maxItems']} items")
        for index, member in enumerate(value):
            check_shape(member, schema["items"], f"{path}[{index}]")
        return
    if expected == "boolean":
        if not isinstance(value, bool):
            raise ValidationError(f"{path} must be true or false")
        return
    if expected in {"integer", "number"}:
        # ``bool`` is an ``int`` in Python, and true is not a count.
        if isinstance(value, bool) or not isinstance(
            value, int if expected == "integer" else (int, float)
        ):
            raise ValidationError(f"{path} must be a {expected}")
        return
    if not isinstance(value, str):
        raise ValidationError(f"{path} must be a string")
    allowed = schema.get("enum")
    if allowed is not None and value not in allowed:
        raise ValidationError(f"{path} must be one of {list(allowed)}; got {value!r}")
    if len(value.strip()) < schema.get("minLength", 0):
        raise ValidationError(f"{path} must not be empty")
    if "maxLength" in schema and len(value) > schema["maxLength"]:
        raise ValidationError(
            f"{path} must be at most {schema['maxLength']} characters; keep it to one sentence"
        )


def framework_text() -> str:
    """Name each framework's own components, because the schema has to allow all of them.

    One static schema serves five frameworks, so its ``components`` object lists every key any
    framework uses. Without this the model fills keys its framework does not allow and the request
    is refused for components it should never have written.
    """
    lines = ["Each framework uses only its own components:"]
    for name, (required, optional) in FRAMEWORKS.items():
        lines.append(
            f"- {name}: {', '.join(required)}"
            + (f"; optional {', '.join(optional)}" if optional else "")
        )
    lines.append("Include no other component key, and omit any component you have nothing for.")
    return "\n".join(lines)


def contract_text(kind: str) -> str:
    """The answer contract, rendered from the schema so prose cannot drift from the grammar."""
    schema = SCHEMAS[kind]
    lines = ["Answer with one JSON object and nothing else:"]
    for field, member in schema["properties"].items():
        required = "required" if field in schema["required"] else "optional"
        if member.get("type") == "array":
            member_type = member["items"].get("type")
            shape = f"array of {'objects' if member_type == 'object' else 'strings'}"
        elif member.get("type") == "object":
            shape = "object with " + ", ".join(member["properties"])
        elif member.get("enum"):
            shape = "one of " + ", ".join(member["enum"])
        else:
            shape = member.get("type", "string")
        lines.append(f"- {field} ({required}): {shape}")
    return "\n".join(lines)


RECORD_FIELDS = ("record_id", "title", "year", "journal", "publication_types", "abstract", "doi")


def _record_view(packet: dict[str, Any]) -> dict[str, Any] | None:
    """The bibliographic fields a decision needs, without the document previews and excerpts.

    A packet also carries per-document locator previews for the session lane. Sending those into a
    single call costs prompt tokens and invites the model to summarize the paper instead of
    deciding.
    """
    record = (packet.get("source") or {}).get("record")
    if not isinstance(record, dict):
        return None
    return {field: record[field] for field in RECORD_FIELDS if record.get(field)}


def build_prompt(kind: str, packet: dict[str, Any], *, hint: str | None = None) -> tuple[str, str]:
    """``(prefix, tail)``.

    The prefix is byte-identical for every item of one Cycle, so the server's prompt cache holds
    across a whole step; the item and any retry hint go in the tail.
    """
    stable = {key: packet[key] for key in STABLE_PACKET_KEYS[kind] if key in packet}
    prefix_parts = [INSTRUCTIONS[kind], contract_text(kind)]
    for key, value in stable.items():
        prefix_parts.append(f"{key.replace('_', ' ').title()}:\n{_render(value)}")
    skip = {"schema_version", "run_id", "task_id", "role", "base_digests", "proposal_path",
            "source_list", "source_count", "instructions", "target_ids"}
    variable = {
        key: value
        for key, value in packet.items()
        if key not in STABLE_PACKET_KEYS[kind] and key not in skip
    }
    record = _record_view(packet)
    if record is not None:
        variable["source"] = record
    tail_parts = [f"{key.replace('_', ' ').title()}:\n{_render(value)}" for key, value in
                  variable.items()]
    if hint:
        tail_parts.append(f"Your previous answer was rejected: {hint}\nAnswer again, corrected.")
    return "\n\n".join(prefix_parts), "\n\n".join(tail_parts)


def _render(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "\n".join(f"- {item}" for item in value)
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False)


def apply_result(
    kind: str, proposal: dict[str, Any], result: Any, packet: dict[str, Any]
) -> dict[str, Any]:
    """Write a checked payload into the task's pre-filled proposal.

    Identity and provenance fields are never touched: record ids, study ids, finding ids, protocol
    outcomes, schema versions, ``base_digests`` and ``certainty.origin`` stay exactly as the task
    minted them, so a model cannot move a decision onto another record.
    """
    check_shape(result, SCHEMAS[kind])
    updated = deepcopy(proposal)
    if kind == "screening":
        row = updated["stages"]["screening"]["records"][0]
        row["decision"] = result["decision"]
        row["reason"] = result["reason"]
        if "basis" in result:
            row["basis"] = result["basis"]
    elif kind == "coverage":
        row = updated["stages"]["coverage"]["records"][0]
        row["selection"] = result["selection"]
        row["reason"] = result["reason"]
        known = list(packet.get("outcomes") or [])
        unknown = [name for name in result["protocol_outcomes"] if name not in known]
        if unknown:
            raise ValidationError(
                f"protocol_outcomes contains unknown outcome {unknown[0]!r}; "
                f"the protocol outcomes are {known}"
            )
        row["protocol_outcomes"] = result["protocol_outcomes"]
    elif kind == "studies":
        row = updated["stages"]["studies"]["records"][0]
        row["kind"] = result["kind"]
        row["basis"] = result["basis"]
        same = (result.get("same_study_id") or "").strip()
        if same:
            existing = {row["study_id"]: row for row in packet.get("existing_studies") or []}
            if same not in existing:
                raise ValidationError(
                    f"same_study_id {same!r} is not an existing study; the linked studies are "
                    f"{sorted(existing)}, and an unlinked record keeps its own study"
                )
            row["study_id"] = same
            row["record_ids"] = sorted({*existing[same]["record_ids"], *row["record_ids"]})
    elif kind == "synthesis":
        updated["title"] = result["title"]
        updated["limitations"] = result["limitations"]
        finding = updated["finding"]
        for field in ("population", "comparison", "timepoint", "claim_basis", "comparator_type",
                      "conclusion"):
            finding[field] = result[field]
        for field in ("gap_reason", "gap_basis"):
            if field in result:
                finding[field] = result[field]
        known_ids = {row["extraction_id"] for row in packet.get("extractions") or []}
        for contribution in result["evidence"]:
            if contribution["extraction_id"] not in known_ids:
                raise ValidationError(
                    f"evidence cites unknown extraction_id "
                    f"{contribution['extraction_id']!r}; this packet shows {sorted(known_ids)}"
                )
        finding["evidence"] = result["evidence"]
        finding["overlap"] = result["overlap"]
        certainty = dict(result["certainty"])
        certainty["origin"] = finding["certainty"]["origin"]
        finding["certainty"] = certainty
    elif kind == "audit":
        _apply_audit(updated, result)
    else:  # pragma: no cover - guarded by CALL_KINDS at every call site
        raise ValidationError(f"no constrained answer shape for {kind}")
    return updated


def _apply_audit(proposal: dict[str, Any], result: dict[str, Any]) -> None:
    """Write one audit verdict into the group's pre-filled template.

    ``target_id`` is the only binding the payload carries.  ``field``, ``check`` and ``assertion``
    are copied from the template, because the validator compares all three byte-for-byte and one
    real assertion reached 14 KB: asking a model to echo that is asking it to spend its whole
    answer proving it can copy.  Every status is derived here rather than asked, because each one
    follows from the verdicts by a rule the validator already enforces -- "unsupported or uncertain
    requires revision" -- and a returned audit kept being refused for breaking it.
    """
    needs_revision = {"unsupported", "uncertain"}
    verdicts = {item["target_id"]: item for item in result["observations"]}

    def fill(observations: list[dict[str, Any]], where: str) -> set[str]:
        """Fill each observation in place and return the verdicts it recorded."""
        recorded = set()
        for observation in observations:
            answer = verdicts.pop(observation["target_id"], None)
            if answer is None:
                raise ValidationError(
                    f"{where} has no verdict for target {observation['target_id']}"
                )
            observation["verdict"] = answer["verdict"]
            observation["rationale"] = answer["rationale"]
            observation["sources"] = [dict(source) for source in answer.get("sources") or []]
            recorded.add(answer["verdict"])
        return recorded

    if "record" in proposal:
        record = proposal["record"]
        by_check: dict[str, set[str]] = {}
        for observation in record["observations"]:
            answer = verdicts.get(observation["target_id"])
            if answer is not None and observation.get("check"):
                by_check.setdefault(observation["check"], set()).add(answer["verdict"])
        reasons = {
            observation.get("check"): verdicts[observation["target_id"]]["rationale"]
            for observation in record["observations"]
            if observation["target_id"] in verdicts
        }
        seen = fill(record["observations"], "this finding")
        scopes = {item["target_id"]: item for item in result.get("memberships") or []}
        unverified = False
        for assessment in record.get("membership_assessments") or []:
            answer = scopes.pop(assessment["target_id"], None)
            if answer is None:
                raise ValidationError(
                    f"this finding has no membership answer for {assessment['target_id']}"
                )
            assessment["supported_scope"] = answer["supported_scope"]
            assessment["rationale"] = answer["rationale"]
            assessment["sources"] = [dict(source) for source in answer.get("sources") or []]
            declared = assessment.get("declared_scope")
            supported = (
                answer["supported_scope"] == "outcome"
                if declared == "outcome"
                else answer["supported_scope"] in {"outcome", "review"}
            )
            unverified = unverified or not supported
        if scopes:
            raise ValidationError(
                f"membership answer for unknown target {sorted(scopes)[0]!r}"
            )
        for name, check in record["checks"].items():
            failed = bool(by_check.get(name, set()) & needs_revision)
            # An unverified membership is an overlap problem by rule, not a separate verdict.
            if name == "overlap" and unverified:
                failed = True
            check["status"] = "revise" if failed else "pass"
            check["rationale"] = reasons.get(name) or check.get("rationale") or (
                "No target contradicted this check."
            )
        record["status"] = (
            "revise"
            if (seen & needs_revision) or unverified
            or any(check["status"] == "revise" for check in record["checks"].values())
            else "pass"
        )
    else:
        for row in proposal["report_reviews"]:
            seen = fill(row["observations"], "this report group")
            row["status"] = "revise" if seen & needs_revision else "pass"
    if verdicts:
        raise ValidationError(
            f"verdict for unknown target {sorted(verdicts)[0]!r}; answer only this group's targets"
        )
