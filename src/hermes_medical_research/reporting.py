"""Deterministic, headless report and evidence-table exports."""

from __future__ import annotations

import csv
import html
import io
import json
import re
from collections import Counter
from typing import Any
from urllib.parse import quote, urlparse

from markdown_it import MarkdownIt

from hermes_medical_research.search.artifacts import _atomic_write
from hermes_medical_research.search.models import ValidationError

from . import arithmetic
from .validation import SCOPE_FIELDS, validate_complete
from .workspace import Workspace, digest

PROVISIONAL_WARNING = (
    "PROVISIONAL: the independent audit did not complete for this Run, so no assertion here has "
    "been checked against its sources by a second reviewer. Deterministic validation and the "
    "arithmetic checks in `arithmetic_findings` did run and did pass."
)


def text(value: Any) -> str:
    value = str(value if value is not None else "not reported")
    return re.sub(r"([\\`*_{}\[\]<>|#!])", r"\\\1", value).replace("\n", " ")


def safe_link(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return None
    return value.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def reference(record: dict[str, Any], metadata: dict[str, Any]) -> str:
    values = {**record, **{k: v for k, v in metadata.items() if v}}
    authors = ", ".join(values.get("authors") or []) or "Author not reported"
    parts = [authors, values.get("title"), values.get("journal"), values.get("year")]
    content = ". ".join(text(part) for part in parts if part) + "."
    doi = values.get("doi")
    link = f"https://doi.org/{quote(doi, safe='/')}" if doi else safe_link(values.get("url"))
    if link:
        content += f" [Source]({link})"
    for name in ("pmid", "nct_id"):
        if values.get(name):
            content += f" {name.upper()}: {text(values[name])}."
    return content


def _table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    return [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
        *["| " + " | ".join(text(cell) for cell in row) + " |" for row in rows],
    ]


def _csv(rows: list[dict[str, Any]], fields: list[str]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        prepared = {}
        for field in fields:
            value = row.get(field)
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                value = "'" + value
            prepared[field] = value
        writer.writerow(prepared)
    return stream.getvalue()


def export(workspace: Workspace, *, provisional: bool = False) -> dict[str, Any]:
    """Write the report artifacts.

    ``provisional`` exports a Run whose independent audit has not completed.  Refusing to export
    one is worse than exporting it plainly labelled: the evidence is already recorded, and a reader
    who is told what was not checked can judge it, while a reader given nothing cannot.  The state
    is stated in the report rather than implied by its absence.
    """
    audited = "reviews" in workspace.load()["datasets"]
    unaudited = provisional and not audited
    warnings = validate_complete(
        workspace, tolerate_missing=frozenset({"reviews"}) if unaudited else frozenset()
    )
    if unaudited:
        warnings.insert(0, PROVISIONAL_WARNING)
    modern = workspace.evidence_version == "2"
    if not modern:
        warnings.append(
            "Legacy evidence schema: estimate semantics and separate claim review "
            "were not validated."
        )
    manifest = workspace.load()
    verification = workspace.store.read_json("verification.json", default=None)
    current = {key: entry["digest"] for key, entry in manifest["datasets"].items()}
    if not verification or verification.get("datasets") != current:
        raise ValidationError(
            "run research verify after the latest evidence changes before exporting"
        )
    if not verification["ready"]:
        raise ValidationError("citation identity mismatches must be resolved before export")
    protocol = manifest["protocol"]
    synthesis = workspace.read("synthesis")
    records = workspace.index("records")
    extractions = workspace.index("extractions")
    appraisals = workspace.index("appraisals")
    studies = workspace.index("studies")
    docs = workspace.index("documents")
    cited = list(dict.fromkeys(e["record_id"] for e in extractions.values()))
    numbers = {rid: i for i, rid in enumerate(cited, 1)}
    unverified = [rid for rid in cited if verification["identity"][rid]["status"] != "verified"]
    if unverified:
        warnings.append(
            f"{len(unverified)} cited records have unverified or unavailable "
            "online identity checks."
        )
    warnings.extend(synthesis["limitations"])
    md = [
        f"# {text(synthesis['title'])}",
        "",
        "## Research question",
        "",
        text(protocol["question"]["question"]),
        "",
        (
            "A separate agent reviewed the findings and exported report claims against the "
            "stored evidence. "
            "Appraisals remain provisional and require human review."
            if modern
            else "Agent-assisted synthesis. Appraisals are provisional and require human review."
        ),
        "",
        f"Workflow: **{protocol['mode']}**. Report language: {text(protocol['language'])}.",
        "",
        "## Search methods",
        "",
        text(protocol["search_rationale"]),
        "",
        f"Framework: {protocol['question']['framework']}. "
        f"Records per source: {protocol['records_per_source']}; "
        f"full-text attempt limit: {protocol['fulltexts']}.",
        "",
        "### Eligibility",
        "",
        "Include: " + "; ".join(text(x) for x in protocol["eligibility"]["include"]),
        "",
        "Exclude: "
        + ("; ".join(text(x) for x in protocol["eligibility"]["exclude"]) or "None specified."),
        "",
    ]
    retrieval_rows = []
    retrieved = filtered = retained = 0
    for entry in manifest["searches"].values():
        if entry["status"] != "attached":
            continue
        snap = workspace.store.read_json(entry["snapshot"])
        if digest(snap) != entry["snapshot_digest"]:
            raise ValidationError("search snapshot was modified")
        for source, strategy in snap["strategy"]["strategies"].items():
            state = snap["manifest"]["sources"][source]
            retrieved += int(state.get("retrieved") or 0)
            filtered += int(state.get("filtered_out") or 0)
            retained += int(state.get("retained") or 0)
            retrieval_rows.append(
                [
                    source,
                    state.get("reported_total"),
                    state.get("retrieved"),
                    state.get("retained"),
                    state.get("filtered_out"),
                    state["status"],
                    state.get("truncated"),
                ]
            )
            query = (
                strategy["precision_query"]
                if strategy["selected_variant"] == "precision"
                else strategy["query"]
            )
            md += [
                f"### {text(source)} — {text(snap['strategy']['created_at'])}",
                "",
                f"Strategy digest: `{entry['strategy_digest']}`",
                "",
                "```text",
                query.replace("```", "` ` `"),
                "```",
                "",
                "Request parameters:",
                "",
                "```json",
                json.dumps(strategy["request_parameters"], ensure_ascii=False, indent=2).replace(
                    "```", "` ` `"
                ),
                "```",
                "",
            ]
            for warning in [*strategy.get("warnings", []), *snap["strategy"].get("warnings", [])]:
                md += ["- " + text(warning)]
            for degradation in strategy.get("degradations", []):
                md += [
                    "- "
                    + text(
                        f"{degradation['feature']}: {degradation['reason']} "
                        f"{degradation['fallback']}"
                    )
                ]
            md.append("")
    md += _table(
        ["Source", "Matches", "Retrieved", "Retained", "Filtered", "Status", "Truncated"],
        retrieval_rows,
    ) + [""]
    screening = workspace.rows("screening")
    decisions = Counter(row["decision"] for row in screening)
    flow = {
        "retrieved_records": retrieved,
        "filtered_records": filtered,
        "retained_before_deduplication": retained,
        "duplicates_removed": retained - len(records),
        "unique_records": len(records),
        "included_records": decisions["include"],
        "excluded_records": decisions["exclude"],
        "uncertain_records": decisions["uncertain"],
        "fulltext_available_records": len(
            {d["record_id"] for d in docs.values() if d["kind"] == "fulltext"}
        ),
        "study_groups": len(studies),
        "human_review": "pending",
    }
    md += [
        "## Selection and coverage",
        "",
        *_table(["Measure", "Count"], [[k, v] for k, v in flow.items()]),
        "",
        "Counts describe this recorded workflow; "
        "they are not evidence of a completed systematic review.",
        "",
    ]
    if modern and workspace.outcome_contract:
        decided = [item for row in workspace.rows("dispositions") for item in row["outcomes"]]
        md += [
            "### Outcome decisions per assessed record",
            "",
            *_table(
                ["Protocol outcome", "Extracted", "Not reported", "Not applicable"],
                [
                    [
                        outcome,
                        *(
                            sum(
                                item["protocol_outcome"] == outcome and item["status"] == status
                                for item in decided
                            )
                            for status in ("extracted", "not_reported", "not_applicable")
                        ),
                    ]
                    for outcome in protocol["outcomes"]
                ],
            ),
            "",
        ]
    md += [
        "## Findings",
        "",
    ]
    if not synthesis["findings"]:
        md += [
            "No supported findings were recorded. "
            "This does not establish that an intervention has no effect.",
            "",
        ]
    summary_rows = []
    for finding in synthesis["findings"]:
        certainty = finding["certainty"]
        contributions = finding["evidence"]
        ids = [item["extraction_id"] for item in contributions]
        refs = sorted({numbers[extractions[eid]["record_id"]] for eid in ids})
        primary = {
            extractions[eid]["study_id"]
            for eid in ids
            if studies[extractions[eid]["study_id"]]["kind"] == "primary"
        }
        summary_rows.append(
            [
                finding["outcome"],
                finding["conclusion"],
                certainty["rating"],
                len(primary),
                ", ".join(f"[{ref}]" for ref in refs),
            ]
        )
        md += [
            f"### {text(finding['finding_id'])}: {text(finding['outcome'])}",
            "",
            text(finding["conclusion"])
            + " "
            + " ".join(f"[{ref}](#reference-{ref})" for ref in refs),
            "",
            "; ".join(f"{field}: {text(finding[field])}" for field in SCOPE_FIELDS),
            "",
            f"**{text(certainty['framework'])}: {text(certainty['rating'])} (provisional).** "
            + text(certainty["rationale"]),
            "",
        ]
        if certainty["framework"] == "GRADE-informed":
            md += [
                "Starting point: " + text(certainty["starting_point"]),
                "",
                "Rating explanation: " + text(certainty["rating_explanation"]),
                "",
            ]
            md += [
                f"- {text(domain)}: {text(reason)}"
                for domain, reason in certainty["domains"].items()
            ] + [""]
        for item in contributions:
            extraction = extractions[item["extraction_id"]]
            access = docs[extraction["source_location"]["document_id"]]["kind"]
            number = numbers[extraction["record_id"]]
            md += [
                f"- [{number}](#reference-{number}) "
                f"{text(item['relationship'])}; {text(access)} evidence. "
                f"{text(item['weight_rationale'])} " + text(item.get("alignment_rationale", ""))
            ]
        md.append("")
    md += [
        "## Summary of findings",
        "",
        *_table(
            ["Outcome", "Finding", "Certainty", "Linked primary studies", "References"],
            summary_rows,
        ),
        "",
        "Linked primary studies counts separately recorded primary studies contributing to each "
        "finding. It does not count trials contained within reviews; zero does not mean a review "
        "contains no trials. Review overlap is assessed separately.",
        "",
        "## Extracted evidence",
        "",
    ]
    evidence_rows = []
    for extraction in extractions.values():
        eid = extraction["extraction_id"]
        effect = extraction["effect"]
        location = extraction["source_location"]
        row = {
            **extraction,
            "reference": numbers[extraction["record_id"]],
            "access": docs[location["document_id"]]["kind"],
            "appraisal": appraisals[eid],
            **{
                f"effect_{key}": effect.get(key)
                for key in (
                    "basis",
                    "measure",
                    "value",
                    "ci_low",
                    "ci_high",
                    "interval_type",
                    "interval_level",
                    "units",
                )
            },
            "appraisal_completion": appraisals[eid].get("completion", "legacy_unreviewed"),
            "appraisal_judgment": appraisals[eid].get("overall_judgment", "legacy_unreviewed"),
        }
        evidence_rows.append(row)
        md += [
            f"### {text(eid)} — [{row['reference']}](#reference-{row['reference']})",
            "",
            text(extraction["result"]),
            "",
            f"Measure: {text(effect['measure'])}; value: {text(effect['value'])}; "
            f"Interval ({text(effect.get('interval_type', 'unspecified'))}): "
            f"{text(effect['ci_low'])} to {text(effect['ci_high'])}; "
            f"units: {text(effect['units'])}; "
            f"sample size: {text(extraction.get('sample_size'))}.",
            "",
            f"Access: {row['access']}; location: {text(location['locator'])}; "
            f"document: {text(location['document_id'])}.",
            "",
            "> " + text(location["quote"]),
            "",
            f"Appraisal: {text(appraisals[eid]['method'])} "
            f"{text(appraisals[eid]['method_version'])}; "
            + (
                text(appraisals[eid]["completion"])
                + "; "
                + text(appraisals[eid]["overall_judgment"])
                + ". "
                if modern
                else ""
            )
            + text(appraisals[eid]["overall"])
            + ". "
            + text(appraisals[eid]["rationale"]),
            "",
        ]
        md += _table(
            ["Appraisal domain", "Judgment", "Rationale"],
            [
                [name, item["judgment"], item["rationale"]]
                for name, item in appraisals[eid]["domains"].items()
            ],
        ) + [""]
    md += ["## Limitations and remaining work", ""]
    md += ["- " + text(value) for value in dict.fromkeys(warnings)] or [
        "- Human review of screening, extraction, and appraisal remains pending."
    ]
    md += ["", "## References", ""]
    # Markdown-it has HTML disabled. Insert only our numeric anchor tags after rendering.
    for rid, number in numbers.items():
        meta = verification["identity"][rid]
        md += [
            f"### Reference {number}",
            "",
            reference(records[rid], meta.get("metadata") or {}),
            "",
            "Identity check: " + text(meta["status"]) + ".",
            "",
        ]
    if modern:
        methods_at = md.index("## Search methods")
        findings_at = md.index("## Findings")
        summary_at = md.index("## Summary of findings")
        extracted_at = md.index("## Extracted evidence")
        limitations_at = md.index("## Limitations and remaining work")
        references_at = md.index("## References")
        key_limits = [
            "## Report status and limitations",
            "",
            "Qualified report."
            if warnings
            else "Evidence workflow complete; human review remains pending.",
            "",
            (
                "Citation identity and source quotation checks are separate from the "
                "recorded host claim review."
            ),
            "",
            *["- " + text(w) for w in dict.fromkeys(warnings)],
            "",
        ]
        md = (
            md[:methods_at]
            + md[summary_at:extracted_at]
            + key_limits
            + md[findings_at:summary_at]
            + md[methods_at:findings_at]
            + md[extracted_at:limitations_at]
            + md[references_at:]
        )
    markdown = "\n".join(md).rstrip() + "\n"
    body = MarkdownIt("commonmark", {"html": False}).enable("table").render(markdown)
    body = re.sub(r"<h3>Reference (\d+)</h3>", r'<h3 id="reference-\1">Reference \1</h3>', body)
    page = (
        '<!doctype html><html lang="' + html.escape(protocol["language"], quote=True) + '"><head>'
        '<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>" + html.escape(synthesis["title"]) + "</title><style>"
        "body{max-width:1000px;margin:3rem auto;padding:0 1rem;"
        "font:17px/1.6 system-ui;color:#18232b}"
        "table{border-collapse:collapse;display:block;overflow-x:auto}"
        "th,td{border:1px solid #ccd5da;padding:.5rem}"
        "pre{overflow:auto;background:#f3f5f7;padding:1rem}"
        "blockquote{border-left:3px solid #547b8d;padding-left:1rem}"
        "a{color:#126177}h1,h2,h3{line-height:1.25}"
        "@media print{body{max-width:none;margin:0}table{display:table}}"
        "</style></head><body>" + body + "</body></html>\n"
    )
    ris = []
    for rid in cited:
        record = records[rid]

        def clean(value: Any) -> str:
            return str(value or "").replace("\r", " ").replace("\n", " ")

        ris += [
            "TY  - " + ("GEN" if record.get("record_kind") == "registration" else "JOUR"),
            "ID  - " + rid,
            "TI  - " + clean(record["title"]),
        ]
        ris += ["AU  - " + clean(author) for author in record.get("authors") or []]
        for tag, field in (("PY", "year"), ("JO", "journal"), ("DO", "doi"), ("UR", "url")):
            if record.get(field):
                ris.append(tag + "  - " + clean(record[field]))
        ris += ["ER  - ", ""]
    outputs = {
        "report.md": markdown,
        "report.html": page,
        "evidence.csv": _csv(
            evidence_rows,
            [
                "extraction_id",
                "record_id",
                "study_id",
                "reference",
                *SCOPE_FIELDS,
                "result",
                "sample_size",
                "effect",
                *(
                    [
                        "effect_basis",
                        "effect_measure",
                        "effect_value",
                        "effect_ci_low",
                        "effect_ci_high",
                        "effect_interval_type",
                        "effect_interval_level",
                        "effect_units",
                        "comparator_type",
                        "outcome_type",
                        "appraisal_completion",
                        "appraisal_judgment",
                        "harms",
                    ]
                    if modern
                    else []
                ),
                "favors",
                "access",
                "source_location",
                "appraisal",
            ],
        ),
        "screening.csv": _csv(screening, ["record_id", "decision", "basis", "reason"]),
        "studies.csv": _csv(list(studies.values()), ["study_id", "record_ids", "kind", "basis"]),
        "references.ris": "\n".join(ris),
    }
    if modern:
        outputs["findings.csv"] = _csv(
            [{**f, "certainty_rating": f["certainty"]["rating"]} for f in synthesis["findings"]],
            [
                "finding_id",
                "protocol_outcomes",
                *SCOPE_FIELDS,
                "claim_basis",
                "conclusion",
                "certainty_rating",
                "certainty",
                "published_certainty",
                "overlap",
                "evidence",
            ],
        )
        outputs["appraisals.csv"] = _csv(
            [
                {
                    "extraction_id": a["extraction_id"],
                    "method": a["method"],
                    "completion": a["completion"],
                    "overall_judgment": a["overall_judgment"],
                    "domain": name,
                    **domain,
                }
                for a in appraisals.values()
                for name, domain in a["domains"].items()
            ],
            [
                "extraction_id",
                "method",
                "completion",
                "overall_judgment",
                "domain",
                "status",
                "judgment",
                "rationale",
                "source_locations",
                "missing_reason",
                "assessment_basis",
                "inspected_locations",
            ],
        )
        outputs["coverage.csv"] = _csv(
            [{**r, "title": records[r["record_id"]]["title"]} for r in workspace.rows("coverage")],
            ["record_id", "title", "selection", "reason", "protocol_outcomes"],
        )
    dispositions = workspace.rows("dispositions") if modern and workspace.outcome_contract else []
    if modern and workspace.outcome_contract:
        outputs["dispositions.csv"] = _csv(
            [
                {
                    "record_id": row["record_id"],
                    "title": records[row["record_id"]]["title"],
                    **item,
                }
                for row in dispositions
                for item in row["outcomes"]
            ],
            [
                "record_id",
                "title",
                "protocol_outcome",
                "status",
                "rationale",
                "extraction_ids",
                "inspected_locations",
            ],
        )
    for filename, content in outputs.items():
        _atomic_write(workspace.path / filename, content)
    workspace.store.write_json("selection-counts.json", flow)
    workspace.store.write_json(
        "report.json",
        {
            "schema_version": workspace.evidence_version,
            "quality": (
                "provisional" if provisional and not audited
                else "qualified" if warnings else "ready"
            ),
            "limitations": list(dict.fromkeys(warnings)),
            "coverage": workspace.rows("coverage") if modern else [],
            "dispositions": dispositions,
            "audit_status": "complete" if audited else "not completed",
            "claim_reviews": workspace.rows("reviews") if modern and audited else [],
            "report_reviews": (
                workspace.read("reviews").get("report_reviews", [])
                if modern and audited else []
            ),
            "arithmetic_findings": arithmetic.check_workspace(workspace),
            "protocol": protocol,
            "synthesis": synthesis,
            "evidence": evidence_rows,
            "verification": verification,
            "selection_counts": flow,
        },
    )
    return {
        "run_dir": str(workspace.path),
        "artifacts": [*outputs, "report.json", "selection-counts.json"],
        "warnings": warnings,
    }
