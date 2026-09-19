#!/usr/bin/env python3
"""Re-audit a finished Run's frozen groups and compare every verdict with what is recorded.

This is the acceptance harness for the constrained audit lane. It rebuilds each audit group from
the Run's own candidate, answers it the way the `call` lane does, and reports how the verdicts
differ from the receipts already stored. The source store is opened read-only and is never written:
nothing here claims, submits, or touches the ledger.

    python scripts/reaudit_compare.py --store PATH --run run-... [--limit N] [--out rows.json]

Two numbers matter more than agreement. **Bad quotes** must be zero: every citation is checked
against the stored segment, which is the property that makes the audit independent rather than a
restatement of what the extractor already claimed. And **findings sent back for revision** must
still be sent back, because those are the defects the audit exists to catch -- on the reference run
three of seven findings carried a mislabelled comparator or a certainty block that misdescribed its
own evidence.

The model is reached the way the step runner reaches it, through the profile's provider endpoint.
Pass `--endpoint` when the gateway is not on its default address.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.request
from copy import deepcopy
from pathlib import Path
from typing import Any

from hermes_medical_research import answers, audit
from hermes_medical_research.tasks import RunCatalog, TaskEngine
from hermes_medical_research.workspace import normalized_text

DEFAULT_ENDPOINT = "http://127.0.0.1:8091/v1/chat/completions"


def quote_verifies(documents: dict[str, Any], source: dict[str, Any]) -> bool:
    """Whether a returned quote occurs verbatim at the locator it names, as `submit` will check."""
    document = documents.get(source.get("document_id"))
    if not document:
        return False
    for segment in document.get("segments", []):
        if segment.get("locator") == source.get("locator"):
            return normalized_text(source.get("quote", "")) in normalized_text(segment["text"])
    return False


def recorded_verdicts(workspace: Any) -> dict[str, str]:
    """The status each group carries in the Run's existing audit receipts."""
    ledger = workspace.load().get("task_engine", {})
    statuses: dict[str, str] = {}
    for task in ledger.get("tasks", {}).values():
        if task.get("kind") != "audit" or task.get("state") != "accepted":
            continue
        if not task.get("result_file"):
            continue
        result = workspace.store.read_json(task["result_file"])
        rows = [*(result.get("records") or []), *(result.get("report_reviews") or [])]
        if rows:
            statuses[task["group_id"]] = (
                "revise" if any(row.get("status") == "revise" for row in rows) else "pass"
            )
    return statuses


def answer(endpoint: str, model: str, packet: dict[str, Any]) -> tuple[Any, float]:
    prefix, tail = answers.build_prompt("audit", packet)
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prefix + tail}],
        "max_tokens": answers.MAX_TOKENS["audit"],
        "temperature": 0,
        "response_format": answers.response_format("audit"),
        "reasoning": {"effort": "none"},
    }
    request = urllib.request.Request(
        endpoint, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    started = time.time()
    with urllib.request.urlopen(request, timeout=900) as response:
        payload = json.loads(response.read())
    seconds = time.time() - started
    text = payload["choices"][0]["message"]["content"]
    return answers.read_object(text), seconds


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--model", default="qwen3.8-flash-next")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    workspace = RunCatalog(args.store).workspace(args.run)
    engine = TaskEngine(workspace)
    _candidate_digest, groups = audit.audit_groups(workspace)
    documents = workspace.source_index()
    contract = audit.citation_contract(workspace)
    before = recorded_verdicts(workspace)
    if args.limit:
        groups = groups[: args.limit]

    rows: list[dict[str, Any]] = []
    for group in groups:
        packet = {
            "kind": "audit",
            "citation_contract": contract,
            "audit_group": {
                key: value
                for key, value in group.items()
                if key not in {"proposal", "allowed_document_ids"}
            },
            "sources": engine._cited_segments(group),
        }
        row: dict[str, Any] = {
            "group": group["group_id"],
            "kind": group["kind"],
            "targets": len(group.get("targets") or []),
            "recorded": before.get(group["group_id"]),
        }
        try:
            result, seconds = answer(args.endpoint, args.model, packet)
            answers.check_shape(result, answers.RESULT_SCHEMAS["audit"])
            filled = answers.apply_result("audit", deepcopy(group["proposal"]), result, packet)
            observations = list((filled.get("record") or {}).get("observations") or [])
            for review in filled.get("report_reviews") or []:
                observations += list(review.get("observations") or [])
            bad = sum(
                1
                for observation in observations
                for source in observation.get("sources") or []
                if not quote_verifies(documents, source)
            )
            statuses = [row["status"] for row in filled.get("report_reviews") or []]
            if "record" in filled:
                statuses.append(filled["record"]["status"])
            row.update(
                ok=True,
                seconds=round(seconds, 1),
                now="revise" if "revise" in statuses else "pass",
                citations=sum(len(o.get("sources") or []) for o in observations),
                bad_quotes=bad,
                verdicts={
                    verdict: sum(1 for o in observations if o["verdict"] == verdict)
                    for verdict in ("supported", "unsupported", "uncertain")
                },
            )
        except Exception as exc:  # noqa: BLE001 - the report names the failure per group
            row.update(ok=False, error=f"{type(exc).__name__}: {exc}"[:300])
        rows.append(row)
        print(json.dumps(row), flush=True)

    answered = [row for row in rows if row.get("ok")]
    seconds = [row["seconds"] for row in answered]
    bad = sum(row["bad_quotes"] for row in answered)
    kept = [
        row for row in answered
        if row["kind"] == "finding" and row["recorded"] == "revise" and row["now"] == "revise"
    ]
    missed = [
        row for row in answered
        if row["kind"] == "finding" and row["recorded"] == "revise" and row["now"] != "revise"
    ]
    print("\n--- gates ---")
    print(f"answered           : {len(answered)} of {len(rows)}")
    print(f"off-schema/failed  : {len(rows) - len(answered)}")
    print(f"bad quotes         : {bad}   (must be 0)")
    print(f"findings still sent back: {len(kept)}  missed: {len(missed)}")
    if seconds:
        print(
            f"seconds per group  : median {statistics.median(seconds):.1f}"
            f"  max {max(seconds):.1f}"
        )
        print(f"total model time   : {sum(seconds) / 60:.1f} min")
    if args.out:
        args.out.write_text(json.dumps(rows, indent=1))
        print(f"wrote {args.out}")
    return 0 if answered and not bad and not missed else 1


if __name__ == "__main__":
    raise SystemExit(main())
