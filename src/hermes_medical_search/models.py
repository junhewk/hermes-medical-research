from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

SCHEMA_VERSION = "1"
SOURCES = ("pubmed", "pmc", "openalex", "semantic-scholar", "scopus")
CORE_SOURCES = SOURCES[:-1]


class ValidationError(ValueError):
    """Raised when a versioned input or strategy is invalid."""


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
class ConceptBlock:
    text: str
    synonyms: list[str] = field(default_factory=list)
    candidate_mesh: list[str] = field(default_factory=list)
    resolved_mesh: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Any, name: str) -> ConceptBlock:
        if not isinstance(data, dict):
            raise ValidationError(f"components.{name} must be an object")
        text = " ".join(str(data.get("text", "")).split())
        if not text:
            raise ValidationError(f"components.{name}.text is required")
        return cls(
            text=text,
            synonyms=_strings(data.get("synonyms"), f"components.{name}.synonyms"),
            candidate_mesh=_strings(
                data.get("candidate_mesh"), f"components.{name}.candidate_mesh"
            ),
            resolved_mesh=_strings(
                data.get("resolved_mesh"), f"components.{name}.resolved_mesh"
            ),
        )

    def free_terms(self) -> list[str]:
        return _dedupe([self.text, *self.synonyms])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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

    @classmethod
    def from_dict(cls, data: Any) -> Question:
        if not isinstance(data, dict):
            raise ValidationError("question input must be a JSON object")
        version = str(data.get("schema_version", ""))
        if version != SCHEMA_VERSION:
            raise ValidationError(
                f"unsupported schema_version {version!r}; expected {SCHEMA_VERSION!r}"
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
                components[name] = ConceptBlock.from_dict(raw_components[name], name)
            elif name in required:
                raise ValidationError(f"components.{name} is required for {framework}")
        sources = _validate_sources(data.get("sources", []), "sources")
        excluded = _validate_sources(data.get("exclude_sources", []), "exclude_sources")
        return cls(
            schema_version=version,
            framework=framework,
            question=question,
            components=components,
            filters=SearchFilters.from_dict(data.get("filters")),
            sources=sources,
            exclude_sources=excluded,
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
class SourceStrategy:
    source: str
    query: str
    precision_query: str | None
    selected_variant: str
    request_parameters: dict[str, Any]
    warnings: list[str] = field(default_factory=list)

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
        return cls(
            source=source,
            query=query,
            precision_query=str(precision).strip() if precision else None,
            selected_variant=selected,
            request_parameters=dict(data.get("request_parameters") or {}),
            warnings=_strings(data.get("warnings"), f"strategies.{source}.warnings"),
        )

    @property
    def selected_query(self) -> str:
        if self.selected_variant == "precision" and self.precision_query:
            return self.precision_query
        return self.query

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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
        if str(data.get("schema_version")) != SCHEMA_VERSION:
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
    names = _strings(value, field_name)
    unknown = sorted(set(names) - set(SOURCES))
    if unknown:
        raise ValidationError(f"unsupported {field_name}: {', '.join(unknown)}")
    return names


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
