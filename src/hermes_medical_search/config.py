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
            "europe-pmc": {"configured": True, "required": []},
            "clinicaltrials": {"configured": True, "required": []},
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

    def redact(self, text: str) -> str:
        """Strip configured secret values from text destined for a run artifact.

        Provider errors are persisted verbatim into preflight.json, manifest.json, and
        summary.json, and they are built from upstream response bodies. OpenAlex and NCBI both
        take their keys as query parameters, so an upstream error that echoes the request would
        otherwise write a live credential into a run directory. Redacting here makes the
        "secrets are never written to run artifacts" guarantee hold by construction.
        """
        cleaned = text
        for secret in (
            self.ncbi_api_key,
            self.openalex_api_key,
            self.semantic_scholar_api_key,
            self.scopus_api_key,
            self.scopus_insttoken,
            self.ncbi_email,
        ):
            # Short values would over-match and destroy the diagnostic; real keys are far longer.
            if secret and len(secret) >= 8:
                cleaned = cleaned.replace(secret, "[redacted]")
        return cleaned

    def redacted(self) -> dict[str, object]:
        return {
            "ncbi_email_configured": bool(self.ncbi_email),
            "ncbi_api_key_configured": bool(self.ncbi_api_key),
            "openalex_api_key_configured": bool(self.openalex_api_key),
            "semantic_scholar_api_key_configured": bool(self.semantic_scholar_api_key),
            "scopus_api_key_configured": bool(self.scopus_api_key),
            "scopus_insttoken_configured": bool(self.scopus_insttoken),
        }


def _clean(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned or None
