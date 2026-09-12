"""Headless full-text acquisition with page/table-level source locations."""

from __future__ import annotations

import hashlib
import io
import ipaddress
import tarfile
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from defusedxml.ElementTree import fromstring
from pdfminer.high_level import extract_pages
from pdfminer.layout import LTTextContainer

from hermes_medical_search.biomedical import EPMC
from hermes_medical_search.config import Credentials
from hermes_medical_search.http import HttpSession
from hermes_medical_search.models import ValidationError

from .workspace import Workspace, now

MAX_BYTES = 50 * 1024 * 1024


def parse_jats(raw: bytes) -> list[dict[str, str]]:
    root = fromstring(raw)
    body = next((e for e in root.iter() if e.tag.split("}")[-1] == "body"), None)
    if body is None:
        return []
    segments: list[dict[str, str]] = []
    counters = {"paragraph": 0, "heading": 0, "table": 0}

    def visit(element: Any) -> None:
        tag = element.tag.split("}")[-1]
        if tag in {"p", "title", "table-wrap"}:
            kind = {"p": "paragraph", "title": "heading", "table-wrap": "table"}[tag]
            counters[kind] += 1
            if kind == "table":
                parts = []
                for node in element.iter():
                    name = node.tag.split("}")[-1]
                    if name in {"label", "caption", "table-wrap-foot"}:
                        parts.append(" ".join(node.itertext()).strip())
                    elif name == "tr":
                        parts.append(" | ".join(" ".join(cell.itertext()).strip() for cell in node))
                text = "\n".join(parts)
            else:
                text = " ".join(element.itertext()).strip()
            if text:
                segments.append({"locator": f"{kind}:{counters[kind]}", "text": text})
            return
        for child in element:
            visit(child)

    visit(body)
    return segments


def parse_pdf(raw: bytes) -> list[dict[str, str]]:
    if not raw.startswith(b"%PDF"):
        raise ValidationError("file is not a PDF")
    result = []
    for number, page in enumerate(extract_pages(io.BytesIO(raw)), start=1):
        text = "\n".join(
            element.get_text() for element in page if isinstance(element, LTTextContainer)
        ).strip()
        if text:
            result.append({"locator": f"page:{number}", "text": text})
    if not result:
        raise ValidationError("PDF contains no extractable text; OCR is required")
    return result


def public_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValidationError("full-text download requires a public HTTPS URL")
    host = parsed.hostname.casefold()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValidationError("local full-text download hosts are not supported")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return value
    if not address.is_global:
        raise ValidationError("private full-text download addresses are not supported")
    return value


async def download(session: HttpSession, url: str) -> bytes:
    # Redirect targets are checked again; never pass credentials from literature API clients.
    for _ in range(6):
        public_url(url)
        async with session.client.stream("GET", url, follow_redirects=False) as response:
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise ValidationError("download redirect has no location")
                url = str(response.url.join(location))
                continue
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > MAX_BYTES:
                    raise ValidationError("full text exceeds 50 MiB download limit")
            return bytes(data)
    raise ValidationError("too many full-text download redirects")


async def acquire(
    record: dict[str, Any], session: HttpSession, credentials: Credentials
) -> tuple[bytes, list[dict[str, str]], str, str]:
    errors = []
    pmcid = record.get("pmcid")
    if not pmcid and record.get("pmid"):
        try:
            lookup = await session.json(
                "europe-pmc",
                f"{EPMC}/search",
                params={
                    "query": f"EXT_ID:{record['pmid']} AND SRC:MED",
                    "format": "json",
                    "pageSize": 1,
                },
            )
            hits = (lookup.get("resultList") or {}).get("result") or []
            pmcid = hits[0].get("pmcid") if hits else None
        except Exception as exc:
            errors.append(f"PMCID lookup: {exc}")
    if pmcid:
        url = f"{EPMC}/{quote(str(pmcid), safe='')}/fullTextXML"
        try:
            raw = await download(session, url)
            segments = parse_jats(raw)
            if segments:
                return raw, segments, url, "xml"
            errors.append("Europe PMC XML has no article body")
        except Exception as exc:
            errors.append(f"Europe PMC XML: {exc}")
    if record.get("doi") and credentials.ncbi_email:
        try:
            data = await session.json(
                "unpaywall",
                "https://api.unpaywall.org/v2/" + quote(record["doi"], safe=""),
                params={"email": credentials.ncbi_email},
            )
            locations = [data.get("best_oa_location"), *(data.get("oa_locations") or [])]
            urls = list(
                dict.fromkeys(
                    loc["url_for_pdf"] for loc in locations if loc and loc.get("url_for_pdf")
                )
            )
            for url in urls[:3]:
                try:
                    raw = await download(session, url)
                    return raw, parse_pdf(raw), url, "pdf"
                except Exception as exc:
                    errors.append(f"OA PDF: {exc}")
        except Exception as exc:
            errors.append(f"Unpaywall: {exc}")
    if pmcid:
        try:
            xml = await session.text(
                "ncbi", "https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi", params={"id": pmcid}
            )
            for link in fromstring(xml).iter("link"):
                url = link.get("href", "").replace(
                    "ftp://ftp.ncbi.nlm.nih.gov/", "https://ftp.ncbi.nlm.nih.gov/"
                )
                if link.get("format") not in {"pdf", "tgz"}:
                    continue
                raw = await download(session, url)
                if link.get("format") == "pdf":
                    return raw, parse_pdf(raw), url, "pdf"
                with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
                    candidates = [
                        m
                        for m in archive.getmembers()
                        if m.isfile() and m.name.lower().endswith(".pdf") and m.size <= MAX_BYTES
                    ]
                    if len(candidates) != 1:
                        errors.append(
                            "PMC archive has zero or multiple PDFs; "
                            "supply the article PDF explicitly"
                        )
                        continue
                    stream = archive.extractfile(candidates[0])
                    if stream:
                        pdf = stream.read(MAX_BYTES + 1)
                        if len(pdf) > MAX_BYTES:
                            raise ValidationError("archived PDF exceeds size limit")
                        return pdf, parse_pdf(pdf), url + "#" + quote(candidates[0].name), "pdf"
        except Exception as exc:
            errors.append(f"PMC OA: {exc}")
    raise ValidationError("; ".join(errors) or "No accessible full-text identifier or PDF location")


async def fetch_fulltexts(
    workspace: Workspace,
    ids: list[str] | None,
    *,
    pdf: Path | None = None,
    retry: bool = False,
    session: HttpSession | None = None,
) -> dict[str, Any]:
    records = workspace.index("records")
    docs = workspace.rows("documents")
    complete = {doc["record_id"] for doc in docs if doc["kind"] == "fulltext"}
    manifest = workspace.load()
    attempts = manifest["fulltext_attempts"]
    limit = manifest["protocol"]["fulltexts"]
    remaining = len(records) if limit == "all" else max(0, limit - len(attempts))
    if ids is None:
        candidates = [
            row["record_id"]
            for row in workspace.rows("screening")
            if row["decision"] == "include" and row["record_id"] not in complete
        ]
        pending = [
            rid
            for rid in candidates
            if rid in attempts and (attempts[rid]["status"] == "running" or retry)
        ]
        ids = pending + [rid for rid in candidates if rid not in attempts][:remaining]
    ids = list(dict.fromkeys(ids))
    if set(ids) - set(records):
        raise ValidationError("unknown record identifier in full-text request")
    if pdf and len(ids) != 1:
        raise ValidationError("--pdf requires exactly one --ids record identifier")
    if sum(rid not in attempts for rid in ids) > remaining:
        raise ValidationError("full-text request exceeds the protocol budget")
    credentials = Credentials.from_env()
    if session is None:
        async with HttpSession() as active:
            return await fetch_fulltexts(workspace, ids, pdf=pdf, retry=retry, session=active)
    outcomes = {}
    for rid in ids:
        if rid in complete and not pdf:
            outcomes[rid] = "already-available"
            continue
        if rid in attempts and attempts[rid]["status"] == "unavailable" and not (retry or pdf):
            outcomes[rid] = "previously-unavailable; use --retry or --pdf"
            continue
        manifest = workspace.load()
        manifest["fulltext_attempts"][rid] = {"status": "running", "started_at": now()}
        workspace.save(manifest)
        try:
            if pdf:
                if pdf.stat().st_size > MAX_BYTES:
                    raise ValidationError("PDF exceeds 50 MiB limit")
                raw = pdf.read_bytes()
                segments, url, extension = parse_pdf(raw), None, "pdf"
            else:
                raw, segments, url, extension = await acquire(records[rid], session, credentials)
            sha = hashlib.sha256(raw).hexdigest()
            relative = f"fulltext/{sha}.{extension}"
            target = workspace.path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            doc = {
                "document_id": f"{rid}:fulltext:{sha[:16]}",
                "record_id": rid,
                "kind": "fulltext",
                "url": url,
                "retrieved_at": now(),
                "sha256": sha,
                "file": relative,
                "segments": segments,
                "extraction_note": (
                    "Table text preserves content; verify spanning cells against the original."
                ),
            }
            docs = [d for d in docs if not (d["record_id"] == rid and d["kind"] == "fulltext")]
            docs.append(doc)
            workspace.put("documents", {"schema_version": "1", "records": docs}, validate=False)
            result = {
                "status": "available",
                "document_id": doc["document_id"],
                "completed_at": now(),
            }
        except Exception as exc:
            result = {
                "status": "unavailable",
                "error": credentials.redact(str(exc)),
                "completed_at": now(),
            }
        manifest = workspace.load()
        manifest["fulltext_attempts"][rid] = result
        workspace.save(manifest)
        outcomes[rid] = result
    return {"fulltexts": outcomes, "status": workspace.status()}
