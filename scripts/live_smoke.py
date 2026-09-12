"""Bounded, headless checks of public APIs; no research conclusions are generated."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import quote

from hermes_medical_search.biomedical import ClinicalTrialsProvider, EuropePMCProvider
from hermes_medical_search.config import Credentials
from hermes_medical_search.http import HttpSession
from hermes_medical_search.models import Question
from hermes_medical_search.query import compile_strategy
from medical_deep_research_plugin.fulltext import acquire


async def main() -> int:
    credentials = Credentials.from_env()
    question = Question.from_dict(
        {
            "schema_version": "1",
            "framework": "PICO",
            "question": "API connectivity fixture",
            "components": {
                "population": {"text": "hypertension"},
                "intervention": {"text": "exercise"},
            },
        }
    )
    strategies = compile_strategy(
        question, mode="quick", limit_per_source=1, sources=["europe-pmc", "clinicaltrials"]
    ).strategies
    results = {}
    async with HttpSession(timeout=30) as session:
        for name, cls in (
            ("europe-pmc", EuropePMCProvider),
            ("clinicaltrials", ClinicalTrialsProvider),
        ):
            try:
                provider = cls(session, credentials)
                strategy = strategies[name]
                if name == "europe-pmc":
                    strategy.query += " AND OPEN_ACCESS:Y"
                page = await provider.fetch_page(strategy, None, 1)
                if not page.records:
                    raise ValueError("smoke query returned no records")
                record = page.records[0]
                results[name] = {"status": "ok", "id": record["source_id"], "total": page.total}
                if name == "europe-pmc":
                    try:
                        raw, segments, _url, extension = await acquire(record, session, credentials)
                        results["fulltext"] = {
                            "status": "ok",
                            "bytes": len(raw),
                            "segments": len(segments),
                            "format": extension,
                        }
                    except Exception as exc:
                        results["fulltext"] = {
                            "status": "failed",
                            "error": credentials.redact(str(exc)),
                        }
                    if record.get("doi"):
                        try:
                            data = await session.json(
                                "crossref",
                                "https://api.crossref.org/works/" + quote(record["doi"], safe=""),
                            )
                            assert data["message"]["DOI"].lower() == record["doi"].lower()
                            results["crossref"] = {"status": "ok", "doi": record["doi"]}
                        except Exception as exc:
                            results["crossref"] = {
                                "status": "failed",
                                "error": credentials.redact(str(exc)),
                            }
                    if record.get("pmid"):
                        strategy.request_parameters = {
                            "link_seed": record["pmid"],
                            "link_direction": "references",
                        }
                        try:
                            linked = await provider.fetch_page(strategy, None, 1)
                            results["citation-chaining"] = {
                                "status": "ok",
                                "total": linked.total,
                                "retrieved": len(linked.records),
                            }
                        except Exception as exc:
                            results["citation-chaining"] = {
                                "status": "failed",
                                "error": credentials.redact(str(exc)),
                            }
            except Exception as exc:
                results[name] = {"status": "failed", "error": credentials.redact(str(exc))}
    print(json.dumps(results, indent=2))
    return int(any(value["status"] != "ok" for value in results.values()))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
