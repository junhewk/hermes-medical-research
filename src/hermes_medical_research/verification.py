"""Citation identity checks; semantic support is recorded by the host agent separately."""

from __future__ import annotations

import asyncio
import re
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import quote

from hermes_medical_research.search.biomedical import CTG, EPMC, europe_record, trial_record
from hermes_medical_research.search.config import Credentials
from hermes_medical_research.search.http import HttpSession

from .validation import validate_complete
from .workspace import Workspace, now


def title_key(value: Any) -> str:
    return re.sub(r"[^\w]+", " ", re.sub(r"<[^>]*>", "", str(value or "")).casefold()).strip()


def compare_identity(record: dict[str, Any], metadata: dict[str, Any]) -> list[str]:
    problems = []
    left, right = title_key(record.get("title")), title_key(metadata.get("title"))
    if not left or not right or SequenceMatcher(None, left, right).ratio() < 0.9:
        problems.append("title mismatch")
    for key in ("pmid", "doi", "nct_id"):
        a, b = str(record.get(key) or "").casefold(), str(metadata.get(key) or "").casefold()
        if a and b and a != b:
            problems.append(f"{key} mismatch")
    publication_types = " ".join(metadata.get("publication_types") or []).lower()
    if metadata.get("is_retracted") or "retracted publication" in publication_types:
        problems.append("retracted publication")
    return problems


async def lookup(
    record: dict[str, Any], session: HttpSession, credentials: Credentials
) -> dict[str, Any] | None:
    if record.get("nct_id") and record.get("record_kind") == "registration":
        return trial_record(
            await session.json("clinicaltrials", CTG + "/" + quote(record["nct_id"], safe=""))
        )
    if record.get("pmid"):
        data = await session.json(
            "europe-pmc",
            f"{EPMC}/search",
            params={
                "query": f"EXT_ID:{record['pmid']} AND SRC:MED",
                "format": "json",
                "resultType": "core",
                "pageSize": 1,
            },
        )
        items = (data.get("resultList") or {}).get("result") or []
        return europe_record(items[0]) if items else None
    if record.get("doi"):
        data = await session.json(
            "crossref", "https://api.crossref.org/works/" + quote(record["doi"], safe="")
        )
        item = data.get("message") or {}
        date = (item.get("published") or {}).get("date-parts") or [[]]
        return {
            "doi": item.get("DOI"),
            "title": (item.get("title") or [""])[0],
            "journal": (item.get("container-title") or [None])[0],
            "authors": [
                " ".join(filter(None, [a.get("given"), a.get("family")]))
                for a in item.get("author", [])
            ],
            "year": str(date[0][0]) if date[0] else None,
            "volume": item.get("volume"),
            "issue": item.get("issue"),
            "pages": item.get("page"),
        }
    if record.get("source") == "openalex":
        data = await session.json(
            "openalex",
            "https://api.openalex.org/works/" + quote(record["source_id"], safe=""),
            params={"api_key": credentials.openalex_api_key}
            if credentials.openalex_api_key
            else {},
        )
        return {"title": data.get("title"), "is_retracted": data.get("is_retracted", False)}
    return None


async def verify(
    workspace: Workspace, *, offline: bool = False, session: HttpSession | None = None
) -> dict[str, Any]:
    warnings = validate_complete(workspace)
    records = workspace.index("records")
    cited = {e["record_id"] for e in workspace.rows("extractions")}
    credentials = Credentials.from_env()
    if session is None and not offline:
        async with HttpSession() as active:
            return await verify(workspace, session=active)
    semaphore = asyncio.Semaphore(4)

    async def check(rid: str) -> tuple[str, dict[str, Any]]:
        if offline:
            return rid, {
                "status": "unverified",
                "reason": "Online identity checks were explicitly skipped.",
            }
        async with semaphore:
            try:
                metadata = await lookup(records[rid], session, credentials)
                if not metadata:
                    return rid, {
                        "status": "unverified",
                        "reason": "No supported identity lookup returned metadata.",
                    }
                issues = compare_identity(records[rid], metadata)
                return rid, {
                    "status": "mismatch" if issues else "verified",
                    "issues": issues,
                    "metadata": metadata,
                    "retraction_check": "available metadata only",
                }
            except Exception as exc:
                return rid, {"status": "unavailable", "reason": credentials.redact(str(exc))}

    results = dict(await asyncio.gather(*(check(rid) for rid in sorted(cited))))
    manifest = workspace.load()
    value = {
        "schema_version": "1",
        "checked_at": now(),
        "offline": offline,
        "datasets": {key: entry["digest"] for key, entry in manifest["datasets"].items()},
        "identity": results,
        "warnings": warnings,
        "semantic_support": "Host-agent judgments; source locations and quotes validated by CLI.",
        "ready": not any(r["status"] == "mismatch" for r in results.values()),
    }
    workspace.store.write_json("verification.json", value)
    return value
