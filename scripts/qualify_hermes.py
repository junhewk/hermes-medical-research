#!/usr/bin/env python3
"""Qualify the step runners against a real Hermes and a real model, on a scratch store."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

from hermes_medical_research.automation import AutomationEngine
from hermes_medical_research.search.artifacts import RunStore
from hermes_medical_research.search.config import Credentials
from hermes_medical_research.search.models import Question
from hermes_medical_research.search.query import compile_strategy
from hermes_medical_research.tasks import ROLE_COMMANDS, TaskEngine


def protocol() -> dict:
    return {
        "schema_version": "3",
        "framework": "PICO",
        "question": "Does synthetic exercise improve a synthetic outcome in adults?",
        "components": {
            "population": {"groups": [{"label": "population", "text": "adults"}]},
            "intervention": {"groups": [{"label": "intervention", "text": "exercise"}]},
        },
        "sources": ["europe-pmc"],
        "eligibility": {"include": ["Adults"], "exclude": ["Animal-only studies"]},
        "outcomes": ["Synthetic outcome"],
        "search_rationale": "One-source qualification search for the deterministic workflow.",
    }


def synthetic_selector_run(
    automation: AutomationEngine, scratch: Path
) -> tuple[str, str, str]:
    name = "selector-pilot-" + uuid4().hex[:12]
    automation.create_review(
        name, protocol(), schedule="once", timezone="UTC", records=1, fulltexts=1
    )
    run_id = automation.review_status(name)["cycles"][0]["run_id"]
    workspace = automation.catalog.workspace(run_id)
    question = Question.from_dict(workspace.load()["protocol"]["question"])
    strategy = compile_strategy(
        question, mode="quick", limit_per_source=1, sources=["europe-pmc"]
    )
    search = RunStore(scratch / ("search-" + uuid4().hex))
    manifest = search.initialize(question, strategy, Credentials())
    search.append_source(
        "europe-pmc",
        [
            {
                "source": "europe-pmc",
                "source_id": "MED:1",
                "pmid": "1",
                "title": "Synthetic exercise trial in adults",
                "abstract": (
                    "Adults were assigned to exercise or control; outcome data were reported."
                ),
                "year": "2025",
                "authors": ["Qualification Fixture"],
                "publication_types": ["Randomized trial"],
                "retrieved_at": "2026-09-15T00:00:00Z",
                "url": "https://example.invalid/synthetic-selector",
            }
        ],
    )
    manifest["status"] = "complete"
    manifest["sources"]["europe-pmc"].update(
        status="complete", retrieved=1, retained=1, reported_total=1
    )
    search.write_manifest(manifest)
    workspace.attach(search.path)
    asyncio.run(automation.tick())
    route = TaskEngine(workspace).status()["active"][0]
    if route.get("role") != "selector":
        raise RuntimeError(f"expected selector task, got {route}")
    return name, run_id, route["task_id"]


def run_step_now(args, step: str, review: str) -> dict:
    """Run one step to completion in the foreground, as an operator would to watch it."""
    command = [
        str(args.hmr),
        "--store",
        str(args.store),
        "step",
        step,
        "--review",
        review,
        "--foreground",
        "--max-wait",
        "0",
        "--hermes-home",
        str(args.hermes_home),
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=args.timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return {"step": step, "returncode": 124, "detail": "step timed out"}
    return {
        "step": step,
        "returncode": completed.returncode,
        "detail": (completed.stdout or completed.stderr).strip()[-400:],
    }


def selector_pilot(
    args, automation: AutomationEngine, scratch: Path
) -> list[dict]:
    results = []
    for _number in range(1, 3):
        review, run_id, task_id = synthetic_selector_run(automation, scratch)
        invocation = run_step_now(args, "select", review)
        engine = TaskEngine(automation.catalog.workspace(run_id))
        state = engine.task_automation(task_id)["state"]
        passed = state == "accepted"
        results.append(
            {
                "run_id": run_id,
                "review": review,
                "task_id": task_id,
                "passed": passed,
                "state": state,
                "invocation": invocation,
            }
        )
        if not passed:
            break
    return results


def full_runs(args, automation: AutomationEngine) -> list[dict]:
    results = []
    for number in range(1, args.full_runs + 1):
        name = f"full-qualification-{number}-{uuid4().hex[:8]}"
        automation.create_review(
            name,
            protocol(),
            schedule="once",
            timezone="UTC",
            records=5,
            fulltexts=2,
        )
        run_id = automation.review_status(name)["cycles"][0]["run_id"]
        invocations = []
        for _ in range(500):
            asyncio.run(automation.tick())
            review = automation.review_status(name)
            if review["latest_cycle_state"] in {"completed", "blocked"}:
                break
            status = TaskEngine(automation.catalog.workspace(run_id)).status()
            if not status["active"]:
                continue
            role = status["active"][0]["role"]
            invocation = run_step_now(args, ROLE_COMMANDS[role], name)
            invocations.append(invocation)
            if invocation["returncode"]:
                break
        final = automation.review_status(name)
        passed = final["latest_cycle_state"] == "completed"
        results.append(
            {
                "run_id": run_id,
                "review": name,
                "passed": passed,
                "state": final["latest_cycle_state"],
                "invocations": invocations,
            }
        )
        if not passed:
            break
    return results


def main() -> int:
    parsed = argparse.ArgumentParser()
    parsed.add_argument("--hermes", default="hermes")
    parsed.add_argument("--hermes-home", type=Path, required=True)
    parsed.add_argument("--store", type=Path)
    parsed.add_argument("--scratch", type=Path)
    parsed.add_argument("--timeout", type=int, default=1800)
    parsed.add_argument("--full-runs", type=int, default=0)
    parsed.add_argument("--prepare-selector-only", action="store_true")
    parsed.add_argument("--output", type=Path)
    args = parsed.parse_args()
    if args.full_runs not in {0, 3}:
        parsed.error("--full-runs must be 0 (selector gate only) or 3")
    temporary = Path(tempfile.mkdtemp(prefix="hmr-qualification-"))
    args.store = (args.store or temporary / "store").resolve()
    scratch = (args.scratch or temporary / "scratch").resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    automation = AutomationEngine(args.store)
    if args.prepare_selector_only:
        review, run_id, task_id = synthetic_selector_run(automation, scratch)
        report = {
            "prepared": True,
            "profile": "hmr-selector",
            "run_id": run_id,
            "task_id": task_id,
            "review": review,
            "store": str(args.store),
        }
        encoded = json.dumps(report, indent=2)
        if args.output:
            args.output.write_text(encoded + "\n")
        print(encoded)
        return 0
    pilot = selector_pilot(args, automation, scratch)
    pilot_passed = len(pilot) == 2 and all(item["passed"] for item in pilot)
    full = full_runs(args, automation) if pilot_passed and args.full_runs else []
    passed = pilot_passed and (
        args.full_runs == 0 or len(full) == 3 and all(item["passed"] for item in full)
    )
    report = {
        "passed": passed,
        "selector_terminal_gate": pilot,
        "full_runs": full,
        "store": str(args.store),
    }
    encoded = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(encoded + "\n")
    print(encoded)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
