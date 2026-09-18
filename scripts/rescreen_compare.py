#!/usr/bin/env python3
"""Re-screen a finished Run's corpus in an isolated store and compare every decision.

This is the acceptance harness for the constrained screening lane, not a formality: it screens the
same records with the shipped path and reports, decision by decision, how the answers differ from
what is already recorded. The source store is opened read-only and is never written.

    hmr-rescreen --source-store PATH --source-run run-... --store /tmp/rescreen \\
        --request PATH --review-name rescreen-060 [--reference results.jsonl] [--limit N]

``--reference`` accepts the per-record trace of an earlier run, as a JSONL file whose objects carry
a record identity and a decision, so the report can pair three ways: the new run against the
recorded decisions, the new run against that earlier trace, and the trace against the recorded
decisions. The third pairing is the control: it must reproduce the earlier agreement, or the record
identities were matched wrongly and the other two numbers mean nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any

from hermes_medical_research import steps
from hermes_medical_research.automation import AutomationEngine
from hermes_medical_research.search.artifacts import RunStore
from hermes_medical_research.search.config import Credentials
from hermes_medical_research.search.models import Question, ValidationError
from hermes_medical_research.search.query import compile_strategy
from hermes_medical_research.tasks import RunCatalog

DECISIONS = ("include", "exclude", "uncertain")
IDENTITY_FIELDS = ("record_id", "doi", "pmid", "pmcid", "source_id", "title")
DECISION_FIELDS = ("decision", "screening_decision")


def identity(row: dict[str, Any]) -> str:
    """A stable key for one record, preferring a real identifier over a title."""
    for field in ("doi", "pmid", "pmcid", "source_id"):
        value = row.get(field)
        if isinstance(value, str) and value.strip():
            return f"{field}:{value.strip().casefold()}"
    title = str(row.get("title") or "").strip().casefold()
    if not title:
        raise ValidationError(f"record has no usable identity: {sorted(row)}")
    return f"title:{title}"


def read_reference(path: Path, by_internal_id: dict[str, str]) -> dict[str, str]:
    """Pair each recorded answer with a record identity, failing loudly on an unknown shape.

    ``by_internal_id`` translates a source Run's own ``record_id`` into the cross-store identity,
    because a stored decision names the record by that id and nothing else.
    """
    rows: list[dict[str, Any]] = []
    text = path.read_text()
    if path.suffix == ".jsonl":
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        loaded = json.loads(text)
        rows = list(loaded.values()) if isinstance(loaded, dict) else list(loaded)
    decisions: dict[str, str] = {}
    for row in rows:
        flat = {**row, **(row.get("parsed") or {})}
        decision = next(
            (flat[field] for field in DECISION_FIELDS
             if isinstance(flat.get(field), str) and flat[field] in DECISIONS),
            None,
        )
        if decision is None:
            continue
        internal = next(
            (flat[field] for field in ("queue_record_id", "record_id")
             if isinstance(flat.get(field), str) and flat[field] in by_internal_id),
            None,
        )
        if internal is not None:
            decisions[by_internal_id[internal]] = decision
            continue
        if not any(flat.get(field) for field in IDENTITY_FIELDS):
            raise ValidationError(
                f"cannot identify a reference row; its keys are {sorted(flat)}"
            )
        decisions[identity(flat)] = decision
    if not decisions:
        raise ValidationError(f"no usable decisions in {path}")
    return decisions


def rebuild_corpus(source: RunCatalog, source_run: str, destination: Path) -> tuple[Path, int]:
    """Write the source Run's records into a fresh search store the workspace can attach."""
    workspace = source.workspace(source_run)
    manifest = workspace.manifest_view()
    records = workspace.rows("records", fresh=False)
    if not records:
        raise ValidationError(f"{source_run} has no records dataset")
    question = Question.from_dict(manifest["protocol"]["question"])
    strategy = compile_strategy(
        question, mode="quick", limit_per_source=len(records), sources=["europe-pmc"]
    )
    store = RunStore(destination.resolve())
    search_manifest = store.initialize(question, strategy, Credentials())
    payload = [
        {
            "source": "europe-pmc",
            "source_id": row.get("source_id") or row["record_id"],
            "pmid": row.get("pmid") or "",
            "doi": row.get("doi") or "",
            "title": row.get("title") or "",
            "abstract": row.get("abstract") or "",
            "year": str(row.get("year") or ""),
            "authors": list(row.get("authors") or []),
            "publication_types": list(row.get("publication_types") or []),
            "retrieved_at": row.get("retrieved_at") or "2026-01-01T00:00:00Z",
            "url": row.get("url") or "",
        }
        for row in records
    ]
    store.append_source("europe-pmc", payload)
    search_manifest["status"] = "complete"
    search_manifest["sources"]["europe-pmc"].update(
        status="complete", retrieved=len(payload), retained=len(payload),
        reported_total=len(payload),
    )
    store.write_manifest(search_manifest)
    return destination, len(payload)


def matrix(pairs: list[tuple[str, str]]) -> dict[str, dict[str, int]]:
    table = {left: {right: 0 for right in DECISIONS} for left in DECISIONS}
    for left, right in pairs:
        if left in table and right in table[left]:
            table[left][right] += 1
    return table


def agreement(pairs: list[tuple[str, str]]) -> float:
    return round(sum(1 for left, right in pairs if left == right) / len(pairs), 4) if pairs else 0.0


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-store", type=Path, required=True)
    parser.add_argument("--source-run", required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--review-name", required=True)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--hermes-home", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--out", type=Path, default=Path("rescreen-report.json"))
    args = parser.parse_args()

    if args.store.resolve() == args.source_store.resolve():
        raise ValidationError("the destination store must not be the source store")
    source = RunCatalog(args.source_store)
    source_workspace = source.workspace(args.source_run)
    source_records = source_workspace.rows("records", fresh=False)
    by_internal_id = {row["record_id"]: identity(row) for row in source_records}
    titles = {identity(row): row.get("title") or "" for row in source_records}
    recorded = {
        by_internal_id[row["record_id"]]: row["decision"]
        for row in source_workspace.rows("screening", fresh=False)
        if row["record_id"] in by_internal_id
    }
    reference = read_reference(args.reference, by_internal_id) if args.reference else {}

    automation = AutomationEngine(args.store)
    created = automation.create_review(
        args.review_name, json.loads(args.request.read_text()),
        schedule="once", timezone="Asia/Seoul",
        # The corpus is replayed whole, so the per-source budget has to admit all of it.
        records=len(source_records), fulltexts=1,
    )
    run_id = automation.review_status(created["name"])["cycles"][0]["run_id"]
    workspace = automation.catalog.workspace(run_id)
    search_path, count = rebuild_corpus(source, args.source_run, args.store / "rescreen-search")
    workspace.attach(search_path)
    await automation.tick()
    print(f"attached {count} records into {run_id}", flush=True)

    started = time.time()
    result = await steps.run_step(
        "select", store=args.store, review=created["name"],
        hermes_home=args.hermes_home, limit=args.limit,
    )
    elapsed = round(time.time() - started, 1)

    new_by_id = {row["record_id"]: identity(row)
                 for row in workspace.rows("records", fresh=False)}
    fresh = {
        new_by_id[row["record_id"]]: row
        for row in workspace.rows("screening", fresh=False)
        if row["record_id"] in new_by_id
    }
    new_decisions = {key: row["decision"] for key, row in fresh.items()}
    pairings = {
        "new_vs_recorded": [(recorded[key], value) for key, value in new_decisions.items()
                            if key in recorded],
        "new_vs_reference": [(reference[key], value) for key, value in new_decisions.items()
                             if key in reference],
        "reference_vs_recorded": [(recorded[key], value) for key, value in reference.items()
                                  if key in recorded],
    }
    disagreements = [
        {
            "title": titles.get(key, "")[:120],
            "recorded": recorded.get(key),
            "reference": reference.get(key),
            "new": value,
            "new_reason": fresh[key].get("reason", ""),
        }
        for key, value in sorted(new_decisions.items())
        if key in recorded and recorded[key] != value
    ]
    report = {
        "run_id": run_id,
        "records": count,
        "decided": len(new_decisions),
        "seconds": elapsed,
        "per_record_seconds": round(elapsed / max(1, len(new_decisions)), 2),
        "state": result["state"],
        "processed": result["processed"],
        "failed": result["failed"],
        "lanes": result["lanes"],
        "failures": result["failures"],
        "counts": {value: sum(1 for item in new_decisions.values() if item == value)
                   for value in DECISIONS},
        "pairings": {
            name: {"n": len(pairs), "agreement": agreement(pairs), "matrix": matrix(pairs)}
            for name, pairs in pairings.items()
        },
        "disagreements": disagreements,
    }
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({key: report[key] for key in
                      ("records", "decided", "seconds", "per_record_seconds", "state",
                       "processed", "failed", "counts")}, indent=2))
    for name, view in report["pairings"].items():
        print(f"{name}: n={view['n']} agreement={view['agreement']}")
    print(f"{len(disagreements)} disagreements; full report at {args.out}")
    gates = {
        "every record decided": len(new_decisions) == count,
        "no failures": result["failed"] == 0,
        "prototype reproduced": (
            report["pairings"]["reference_vs_recorded"]["n"] == 0
            or report["pairings"]["reference_vs_recorded"]["agreement"] >= 0.9
        ),
    }
    for gate, passed in gates.items():
        print(f"{'PASS' if passed else 'FAIL'} {gate}")
    return 0 if all(gates.values()) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
