from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from .models import CORE_SOURCES


@dataclass(frozen=True, slots=True)
class Credentials:
    ncbi_email: str | None = None
    ncbi_api_key: str | None = None
    openalex_api_key: str | None = None
    semantic_scholar_api_key: str | None = None
    scopus_api_key: str | None = None
    scopus_insttoken: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Credentials:
        values = env if env is not None else os.environ
        return cls(
            ncbi_email=_clean(values.get("NCBI_EMAIL")),
            ncbi_api_key=_clean(values.get("NCBI_API_KEY")),
            openalex_api_key=_clean(values.get("OPENALEX_API_KEY")),
            semantic_scholar_api_key=_clean(
                values.get("SEMANTIC_SCHOLAR_API_KEY") or values.get("S2_API_KEY")
            ),
            scopus_api_key=_clean(values.get("SCOPUS_API_KEY")),
            scopus_insttoken=_clean(values.get("SCOPUS_INSTTOKEN")),
        )

    def configured_sources(self) -> list[str]:
        return [*CORE_SOURCES, *(["scopus"] if self.scopus_api_key else [])]

    def configuration_status(self) -> dict[str, dict[str, object]]:
        return {
            "pubmed": {
                "configured": bool(self.ncbi_email),
                "required": ["NCBI_EMAIL"],
                "optional_configured": bool(self.ncbi_api_key),
            },
            "pmc": {
                "configured": bool(self.ncbi_email),
                "required": ["NCBI_EMAIL"],
                "optional_configured": bool(self.ncbi_api_key),
            },
            "openalex": {
                "configured": True,
                "required": [],
                "optional_configured": bool(self.openalex_api_key),
            },
            "semantic-scholar": {
                "configured": True,
                "required": [],
                "optional_configured": bool(self.semantic_scholar_api_key),
            },
            "scopus": {
                "configured": bool(self.scopus_api_key),
                "required": ["SCOPUS_API_KEY"],
                "optional_configured": bool(self.scopus_insttoken),
            },
        }

    def redacted(self) -> dict[str, object]:
        return {
            "ncbi_email_configured": bool(self.ncbi_email),
            "ncbi_api_key_configured": bool(self.ncbi_api_key),
            "openalex_api_key_configured": bool(self.openalex_api_key),
            "semantic_scholar_api_key_configured": bool(
                self.semantic_scholar_api_key
            ),
            "scopus_api_key_configured": bool(self.scopus_api_key),
            "scopus_insttoken_configured": bool(self.scopus_insttoken),
        }


def _clean(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned or None
