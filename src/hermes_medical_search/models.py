from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

SCHEMA_VERSION = "2"
LEGACY_SCHEMA_VERSION = "1"
SOURCES = ("pubmed", "pmc", "openalex", "semantic-scholar", "scopus")
CORE_SOURCES = SOURCES[:-1]
# (ISO 639-1, English name, *aliases) for the languages PubMed reports. PubMed emits ISO 639-2/B
# ("eng", "ger", "fre"), OpenAlex emits ISO 639-1 ("en"), and users write English names, so all
# three must normalize to one value before a language filter can compare them. Where 639-2/B and
# /T differ both are listed (ger/deu, fre/fra, chi/zho, ...).
_LANGUAGE_ALIASES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("af", "afrikaans", ("afr",)),
    ("am", "amharic", ("amh",)),
    ("ar", "arabic", ("ara",)),
    ("az", "azerbaijani", ("aze",)),
    ("bg", "bulgarian", ("bul",)),
    ("bn", "bengali", ("ben",)),
    ("bs", "bosnian", ("bos",)),
    ("ca", "catalan", ("cat",)),
    ("cs", "czech", ("cze", "ces")),
    ("cy", "welsh", ("wel", "cym")),
    ("da", "danish", ("dan",)),
    ("de", "german", ("ger", "deu")),
    ("el", "greek", ("gre", "ell")),
    ("en", "english", ("eng",)),
    ("eo", "esperanto", ("epo",)),
    ("es", "spanish", ("spa",)),
    ("et", "estonian", ("est",)),
    ("fa", "persian", ("per", "fas")),
    ("fi", "finnish", ("fin",)),
    ("fr", "french", ("fre", "fra")),
    ("gd", "scottish gaelic", ("gla",)),
    ("he", "hebrew", ("heb",)),
    ("hi", "hindi", ("hin",)),
    ("hr", "croatian", ("hrv",)),
    ("hu", "hungarian", ("hun",)),
    ("hy", "armenian", ("arm", "hye")),
    ("id", "indonesian", ("ind",)),
    ("is", "icelandic", ("ice", "isl")),
    ("it", "italian", ("ita",)),
    ("ja", "japanese", ("jpn",)),
    ("ka", "georgian", ("geo", "kat")),
    ("ko", "korean", ("kor",)),
    ("la", "latin", ("lat",)),
    ("lt", "lithuanian", ("lit",)),
    ("lv", "latvian", ("lav",)),
    ("mi", "maori", ("mao", "mri")),
    ("mk", "macedonian", ("mac", "mkd")),
    ("ml", "malayalam", ("mal",)),
    ("ms", "malay", ("may", "msa")),
    ("nl", "dutch", ("dut", "nld")),
    ("no", "norwegian", ("nor",)),
    ("pl", "polish", ("pol",)),
    ("ps", "pushto", ("pus",)),
    ("pt", "portuguese", ("por",)),
    ("ro", "romanian", ("rum", "ron")),
    ("ru", "russian", ("rus",)),
    ("rw", "kinyarwanda", ("kin",)),
    ("sa", "sanskrit", ("san",)),
    ("sk", "slovak", ("slo", "slk")),
    ("sl", "slovenian", ("slv",)),
    ("sq", "albanian", ("alb", "sqi")),
    ("sr", "serbian", ("srp",)),
    ("sv", "swedish", ("swe",)),
    ("th", "thai", ("tha",)),
    ("tr", "turkish", ("tur",)),
    ("uk", "ukrainian", ("ukr",)),
    ("ur", "urdu", ("urd",)),
    ("vi", "vietnamese", ("vie",)),
    ("zh", "chinese", ("chi", "zho")),
)
LANGUAGE_CODES = {
    alias: code
    for code, name, aliases in _LANGUAGE_ALIASES
    for alias in (name, code, *aliases)
}
# PubMed's [Language] field matches the full English name only: "eng"[Language] and
# "en"[Language] both return zero results silently, so a code must be expanded before it is
# compiled into a query.
LANGUAGE_NAMES = {
    alias: name
    for code, name, aliases in _LANGUAGE_ALIASES
    for alias in (name, code, *aliases)
}
# PubMed's "undetermined" and "multiple languages" markers carry no filterable language, so they
# normalize to the empty string and _passes_filters treats them as missing metadata rather than
# as a language that failed to match.
LANGUAGE_CODES.update({"und": "", "undetermined": "", "mul": "", "multiple": ""})


class ValidationError(ValueError):
    """Raised when a versioned input or strategy is invalid."""


def language_code(value: str) -> str:
    """Normalize an English name, ISO 639-1, or ISO 639-2/B|T code to ISO 639-1."""
    normalized = " ".join(value.split()).casefold()
    return LANGUAGE_CODES.get(normalized, normalized)


def language_name(value: str) -> str:
    """Normalize any accepted spelling to the full English name PubMed's [Language] requires."""
    normalized = " ".join(value.split()).casefold()
    return LANGUAGE_NAMES.get(normalized, normalized)


def _strings(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValidationError(f"{field_name} must be an array of strings")
    seen: set[str] = set()
    result: list[str] = []
    for item in value:
        cleaned = " ".join(item.split())
        if cleaned and cleaned.casefold() not in seen:
            seen.add(cleaned.casefold())
            result.append(cleaned)
    return result


def _date(value: Any, field_name: str) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be an ISO date")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValidationError(f"{field_name} must be YYYY-MM-DD") from exc


@dataclass(slots=True)
class ConceptGroup:
    label: str
    text: str
    synonyms: list[str] = field(default_factory=list)
    candidate_mesh: list[str] = field(default_factory=list)
    resolved_mesh: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Any, field_name: str) -> ConceptGroup:
        if not isinstance(data, dict):
            raise ValidationError(f"{field_name} must be an object")
        label = " ".join(str(data.get("label", "")).split())
        text = " ".join(str(data.get("text", "")).split())
        if not label:
            raise ValidationError(f"{field_name}.label is required")
        if not text:
            raise ValidationError(f"{field_name}.text is required")
        return cls(
            label=label,
            text=text,
            synonyms=_strings(data.get("synonyms"), f"{field_name}.synonyms"),
            candidate_mesh=_strings(
                data.get("candidate_mesh"), f"{field_name}.candidate_mesh"
            ),
            resolved_mesh=_strings(
                data.get("resolved_mesh"), f"{field_name}.resolved_mesh"
            ),
        )

    def free_terms(self) -> list[str]:
        return _dedupe([self.text, *self.synonyms])

    def all_terms(self) -> list[str]:
        return _dedupe([*self.free_terms(), *self.resolved_mesh])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ConceptBlock:
    groups: list[ConceptGroup]

    @classmethod
    def from_dict(cls, data: Any, name: str, *, schema_version: str) -> ConceptBlock:
        field_name = f"components.{name}"
        if not isinstance(data, dict):
            raise ValidationError(f"{field_name} must be an object")
        if schema_version == LEGACY_SCHEMA_VERSION:
            legacy = dict(data)
            legacy["label"] = name
            return cls(groups=[ConceptGroup.from_dict(legacy, field_name)])
        raw_groups = data.get("groups")
        if not isinstance(raw_groups, list) or not raw_groups:
            raise ValidationError(f"{field_name}.groups must be a non-empty array")
        groups = [
            ConceptGroup.from_dict(value, f"{field_name}.groups[{index}]")
            for index, value in enumerate(raw_groups)
        ]
        labels = [group.label.casefold() for group in groups]
        if len(labels) != len(set(labels)):
            raise ValidationError(f"{field_name}.groups labels must be unique")
        return cls(groups=groups)

    def free_terms(self) -> list[str]:
        return _dedupe([term for group in self.groups for term in group.free_terms()])

    def all_terms(self) -> list[str]:
        return _dedupe([term for group in self.groups for term in group.all_terms()])

    def to_dict(self) -> dict[str, Any]:
        return {"groups": [group.to_dict() for group in self.groups]}


@dataclass(slots=True)
class SearchFilters:
    from_date: str | None = None
    to_date: str | None = None
    languages: list[str] = field(default_factory=list)
    publication_types: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Any) -> SearchFilters:
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ValidationError("filters must be an object")
        result = cls(
            from_date=_date(data.get("from_date"), "filters.from_date"),
            to_date=_date(data.get("to_date"), "filters.to_date"),
            languages=_strings(data.get("languages"), "filters.languages"),
            publication_types=_strings(
                data.get("publication_types"), "filters.publication_types"
            ),
        )
        if result.from_date and result.to_date and result.from_date > result.to_date:
            raise ValidationError("filters.from_date must not be after filters.to_date")
        return result

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Question:
    schema_version: str
    framework: str
    question: str
    components: dict[str, ConceptBlock]
    filters: SearchFilters = field(default_factory=SearchFilters)
    sources: list[str] = field(default_factory=list)
    exclude_sources: list[str] = field(default_factory=list)
    migrated_from_schema: str | None = field(default=None, repr=False)

    @classmethod
    def from_dict(cls, data: Any) -> Question:
        if not isinstance(data, dict):
            raise ValidationError("question input must be a JSON object")
        input_version = str(data.get("schema_version", ""))
        if input_version not in {LEGACY_SCHEMA_VERSION, SCHEMA_VERSION}:
            raise ValidationError(
                f"unsupported schema_version {input_version!r}; expected '1' or '2'"
            )
        framework = str(data.get("framework", "")).upper()
        if framework not in {"PICO", "PCC"}:
            raise ValidationError("framework must be PICO or PCC")
        question = str(data.get("question", "")).strip()
        if not question:
            raise ValidationError("question is required")
        raw_components = data.get("components")
        if not isinstance(raw_components, dict):
            raise ValidationError("components must be an object")
        required = ("population", "intervention") if framework == "PICO" else (
            "population",
            "concept",
        )
        optional = ("comparison", "outcome") if framework == "PICO" else ("context",)
        components: dict[str, ConceptBlock] = {}
        for name in (*required, *optional):
            if name in raw_components and raw_components[name] not in (None, ""):
                components[name] = ConceptBlock.from_dict(
                    raw_components[name], name, schema_version=input_version
                )
            elif name in required:
                raise ValidationError(f"components.{name} is required for {framework}")
        sources = _validate_sources(data.get("sources", []), "sources")
        excluded = _validate_sources(data.get("exclude_sources", []), "exclude_sources")
        return cls(
            schema_version=SCHEMA_VERSION,
            framework=framework,
            question=question,
            components=components,
            filters=SearchFilters.from_dict(data.get("filters")),
            sources=sources,
            exclude_sources=excluded,
            migrated_from_schema=(
                input_version if input_version != SCHEMA_VERSION else None
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "framework": self.framework,
            "question": self.question,
            "components": {key: value.to_dict() for key, value in self.components.items()},
            "filters": self.filters.to_dict(),
            "sources": self.sources,
            "exclude_sources": self.exclude_sources,
        }


@dataclass(slots=True)
class QueryDegradation:
    feature: str
    reason: str
    fallback: str

    @classmethod
    def from_dict(cls, data: Any, field_name: str) -> QueryDegradation:
        if not isinstance(data, dict):
            raise ValidationError(f"{field_name} must be an object")
        values = {
            key: " ".join(str(data.get(key, "")).split())
            for key in ("feature", "reason", "fallback")
        }
        for key, value in values.items():
            if not value:
                raise ValidationError(f"{field_name}.{key} is required")
        return cls(**values)

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(slots=True)
class SourceStrategy:
    source: str
    query: str
    precision_query: str | None
    selected_variant: str
    request_parameters: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    degradations: list[QueryDegradation] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Any) -> SourceStrategy:
        if not isinstance(data, dict):
            raise ValidationError("source strategy must be an object")
        source = str(data.get("source", ""))
        if source not in SOURCES:
            raise ValidationError(f"unsupported source {source!r}")
        selected = str(data.get("selected_variant", "sensitivity"))
        if selected not in {"sensitivity", "precision"}:
            raise ValidationError("selected_variant must be sensitivity or precision")
        query = str(data.get("query", "")).strip()
        precision = data.get("precision_query")
        if not query:
            raise ValidationError(f"empty query for {source}")
        if selected == "precision" and not precision:
            raise ValidationError(f"precision variant is unavailable for {source}")
        return cls(
            source=source,
            query=query,
            precision_query=str(precision).strip() if precision else None,
            selected_variant=selected,
            request_parameters=dict(data.get("request_parameters") or {}),
            warnings=_strings(data.get("warnings"), f"strategies.{source}.warnings"),
            degradations=[
                QueryDegradation.from_dict(value, f"strategies.{source}.degradations[{index}]")
                for index, value in enumerate(data.get("degradations") or [])
            ],
        )

    @property
    def selected_query(self) -> str:
        if self.selected_variant == "precision" and self.precision_query:
            return self.precision_query
        return self.query

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "query": self.query,
            "precision_query": self.precision_query,
            "selected_variant": self.selected_variant,
            "request_parameters": self.request_parameters,
            "warnings": self.warnings,
            "degradations": [item.to_dict() for item in self.degradations],
        }


@dataclass(slots=True)
class Strategy:
    schema_version: str
    tool_version: str
    mode: str
    created_at: str
    question: Question
    limit_per_source: int | str
    strategies: dict[str, SourceStrategy]
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Any) -> Strategy:
        if not isinstance(data, dict):
            raise ValidationError("strategy must be a JSON object")
        version = str(data.get("schema_version"))
        if version != SCHEMA_VERSION:
            if version == LEGACY_SCHEMA_VERSION:
                raise ValidationError(
                    "v0.1 run directories cannot be resumed by v0.2; re-plan the v1 question"
                )
            raise ValidationError("unsupported strategy schema_version")
        mode = str(data.get("mode", ""))
        if mode not in {"quick", "review"}:
            raise ValidationError("mode must be quick or review")
        limit: int | str = data.get("limit_per_source", 0)
        if limit != "all" and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0
        ):
            raise ValidationError("limit_per_source must be a positive integer or 'all'")
        strategies = {
            name: SourceStrategy.from_dict(value)
            for name, value in dict(data.get("strategies") or {}).items()
        }
        if not strategies:
            raise ValidationError("at least one source strategy is required")
        return cls(
            schema_version=SCHEMA_VERSION,
            tool_version=str(data.get("tool_version", "")),
            mode=mode,
            created_at=str(data.get("created_at", "")),
            question=Question.from_dict(data.get("question")),
            limit_per_source=limit,
            strategies=strategies,
            warnings=_strings(data.get("warnings"), "warnings"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "tool_version": self.tool_version,
            "mode": self.mode,
            "created_at": self.created_at,
            "question": self.question.to_dict(),
            "limit_per_source": self.limit_per_source,
            "strategies": {key: value.to_dict() for key, value in self.strategies.items()},
            "warnings": self.warnings,
        }


def _validate_sources(value: Any, field_name: str) -> list[str]:
    # Casefold to match the CLI's --sources/--exclude parsing; one vocabulary, one set of rules.
    names = [name.casefold() for name in _strings(value, field_name)]
    unknown = sorted(set(names) - set(SOURCES))
    if unknown:
        raise ValidationError(f"unsupported {field_name}: {', '.join(unknown)}")
    return list(dict.fromkeys(names))


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = re.sub(r"\s+", " ", value).strip()
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result
