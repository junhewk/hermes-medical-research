"""Small CLI surface for host agents; all judgments enter as reviewable JSON files."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from hermes_medical_search.artifacts import RunStore, strategy_digest
from hermes_medical_search.config import Credentials
from hermes_medical_search.models import Question, SourceStrategy, ValidationError
from hermes_medical_search.query import compile_strategy

from .fulltext import fetch_fulltexts
from .packets import documents, next_packet
from .reporting import export
from .validation import METHODS
from .verification import verify
from .workflow import check, finalize, submit_batch
from .workspace import DEPENDENCIES, Workspace


def add_commands(commands: Any) -> None:
    research = commands.add_parser(
        "research", help="Create, resume, and export an evidence research workspace"
    )
    actions = research.add_subparsers(dest="research_command", required=True)
    host_session = actions.add_parser(
        "host-session", help="Check native hook binding and shared budget"
    )
    host_session.add_argument("run_dir", type=Path)
    host_session.add_argument("--total-turns", type=int, default=150)
    host_session.add_argument("--hook-token", help=argparse.SUPPRESS)
    init = actions.add_parser("init", help="Initialize a protocol and research budgets")
    init.add_argument("input", type=Path)
    init.add_argument("--output", type=Path, required=True)
    init.add_argument("--mode", choices=("report", "review-prep"), default="report")
    init.add_argument("--records-per-source")
    init.add_argument("--fulltexts")
    init.add_argument("--language", default="en")
    attach = actions.add_parser(
        "attach-search", help="Snapshot a completed or failed search with provenance"
    )
    attach.add_argument("run_dir", type=Path)
    attach.add_argument("search_dir", type=Path)
    fulltext = actions.add_parser("fulltext", help="Acquire OA texts or attach a user-supplied PDF")
    fulltext.add_argument("run_dir", type=Path)
    fulltext.add_argument(
        "--ids", help="Comma-separated record IDs; default: included records within budget"
    )
    fulltext.add_argument("--pdf", type=Path, help="User-supplied PDF for exactly one --ids record")
    fulltext.add_argument("--retry", action="store_true")
    record = actions.add_parser("record", help="Validate and replace a complete stage payload")
    record.add_argument("run_dir", type=Path)
    mode = record.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--stage",
        choices=(
            "screening",
            "studies",
            "extractions",
            "appraisals",
            "coverage",
            "synthesis",
            "reviews",
        ),
    )
    mode.add_argument("--batch", action="store_true")
    record.add_argument("--input", type=Path, required=True)
    verify_parser = actions.add_parser(
        "verify", help="Validate evidence links and check cited identities"
    )
    verify_parser.add_argument("run_dir", type=Path)
    verify_parser.add_argument(
        "--offline", action="store_true", help="Explicitly skip online identity checks"
    )
    status = actions.add_parser(
        "status", help="Show remaining work or page through a recorded stage"
    )
    status.add_argument("run_dir", type=Path)
    status.add_argument("--stage", choices=tuple(DEPENDENCIES))
    status.add_argument("--offset", type=int, default=0)
    status.add_argument("--limit", type=int, default=50)
    status.add_argument("--record-id")
    status.add_argument("--document-id")
    status.add_argument("--locator")
    status.add_argument("--query", help="Literal case-insensitive text search in source segments")
    next_parser = actions.add_parser(
        "next", help="Write the next source packet and editable batch template"
    )
    next_parser.add_argument("run_dir", type=Path)
    next_parser.add_argument(
        "--stage",
        choices=(
            "retrieval",
            "screening",
            "coverage",
            "fulltext",
            "studies",
            "assessment",
            "synthesis",
            "review",
            "finalize",
        ),
    )
    next_parser.add_argument("--limit", type=int)
    next_parser.add_argument("--output", type=Path)
    checker = actions.add_parser(
        "check", help="Read-only, aggregated evidence and readiness checks"
    )
    checker.add_argument("run_dir", type=Path)
    checker.add_argument("--input", type=Path, help="Preview a proposed batch without committing")
    finalizer = actions.add_parser("finalize", help="Check, optionally submit, verify, and export")
    finalizer.add_argument("run_dir", type=Path)
    finalizer.add_argument("--input", type=Path)
    finalizer.add_argument(
        "--offline", action="store_true", help="Explicitly skip online identity checks"
    )
    exporter = actions.add_parser(
        "export", help="Write Markdown, HTML, CSV, JSON, and RIS artifacts"
    )
    exporter.add_argument("run_dir", type=Path)
    snowball = actions.add_parser(
        "snowball", help="Plan a bounded one-hop Europe PMC citation search"
    )
    snowball.add_argument("run_dir", type=Path)
    snowball.add_argument("--record-id", required=True)
    snowball.add_argument("--direction", choices=("references", "citations"), default="references")
    snowball.add_argument("--limit", type=int, default=25)
    actions.add_parser(
        "methods", help="Show supported appraisal methods, versions, and domain keys"
    )


def _limit(value: str | None) -> int | str | None:
    if value is None or value == "all":
        return value
    try:
        return int(value)
    except ValueError as exc:
        raise ValidationError("limits must be a positive integer or all") from exc


async def dispatch(args: argparse.Namespace) -> int:
    action = args.research_command
    if action == "methods":
        print(
            json.dumps(
                {
                    name: {"version": version, "domains": domains.split()}
                    for name, (version, domains) in METHODS.items()
                },
                indent=2,
            )
        )
        return 0
    if action == "host-session":
        from .native_review import STATE, budget_status

        workspace = Workspace(args.run_dir)
        state = workspace.store.read_json(STATE, default=None)
        binding = workspace.store.read_json("native-binding.json", default={})
        if not state or not args.hook_token or binding.get("token") != args.hook_token:
            raise ValidationError(
                "Native hooks did not bind this report. Enable/trust plugin hooks; "
                "do not self-review."
            )
        print(json.dumps({"host": state["host"], "budget": budget_status(state)}))
        return 0
    if action == "init":
        workspace = Workspace(args.output)
        workspace.path.mkdir(parents=True, exist_ok=True)
        with workspace.lock:
            value = workspace.init(
                json.loads(args.input.read_text(encoding="utf-8")),
                mode=args.mode,
                records=_limit(args.records_per_source),
                fulltexts=_limit(args.fulltexts),
                language=args.language,
            )
    else:
        workspace = Workspace(args.run_dir)
        workspace.load()
        if action == "check":
            value = check(workspace, json.loads(args.input.read_text()) if args.input else None)
        elif action == "status":
            value = workspace.status()
            if args.stage:
                if args.offset < 0 or args.limit < 1:
                    raise ValidationError("offset must be nonnegative and limit must be positive")
                data = workspace.read(args.stage, fresh=False)
                if args.stage == "documents" and any(
                    (args.record_id, args.document_id, args.locator, args.query)
                ):
                    value["data"] = documents(
                        workspace,
                        record_id=args.record_id,
                        document_id=args.document_id,
                        locator=args.locator,
                        query=args.query,
                        offset=args.offset,
                        limit=args.limit,
                    )
                    print(json.dumps(value, ensure_ascii=False, indent=2))
                    return 0
                value["data"] = (
                    data
                    if args.stage == "synthesis"
                    else {
                        "total": len(data["records"]),
                        "offset": args.offset,
                        "records": data["records"][args.offset : args.offset + args.limit],
                    }
                )
        else:
            with workspace.lock:
                if action == "attach-search":
                    value = workspace.attach(args.search_dir)
                elif action == "fulltext":
                    ids = (
                        [item.strip() for item in args.ids.split(",") if item.strip()]
                        if args.ids
                        else None
                    )
                    value = await fetch_fulltexts(workspace, ids, pdf=args.pdf, retry=args.retry)
                elif action == "record":
                    payload = json.loads(args.input.read_text(encoding="utf-8"))
                    if args.batch:
                        value = submit_batch(workspace, payload)
                    else:
                        workspace.put(args.stage, payload)
                        value = workspace.status()
                elif action == "next":
                    value = next_packet(
                        workspace, stage=args.stage, limit=args.limit, output=args.output
                    )
                elif action == "finalize":
                    value = await finalize(
                        workspace,
                        json.loads(args.input.read_text()) if args.input else None,
                        offline=args.offline,
                    )
                elif action == "verify":
                    value = await verify(workspace, offline=args.offline)
                elif action == "export":
                    value = export(workspace)
                elif action == "snowball":
                    value = plan_snowball(workspace, args.record_id, args.direction, args.limit)
                else:
                    raise ValidationError("unknown research command")
    print(json.dumps(value, ensure_ascii=False, indent=2))
    failed = (
        (action in {"verify", "check"} and not value["ready"])
        or (action == "record" and value.get("accepted") is False)
        or (action == "finalize" and not value["completed"])
    )
    return 2 if failed else 0


def plan_snowball(
    workspace: Workspace, record_id: str, direction: str, limit: int
) -> dict[str, Any]:
    records = workspace.index("records")
    if record_id not in records or not str(records[record_id].get("pmid") or "").isdigit():
        raise ValidationError("Europe PMC citation chaining requires a seed record with a PMID")
    if not 1 <= limit <= 100:
        raise ValidationError("one-hop citation limit must be between 1 and 100")
    protocol = workspace.load()["protocol"]
    question = Question.from_dict(protocol["question"])
    # Link traversal has no semantic/date filtering; screening applies the recorded protocol.
    question.filters = type(question.filters)()
    mode = "review" if protocol["mode"] == "review-prep" else "quick"
    strategy = compile_strategy(question, mode=mode, limit_per_source=limit, sources=["europe-pmc"])
    pmid = str(records[record_id]["pmid"])
    strategy.strategies["europe-pmc"] = SourceStrategy(
        source="europe-pmc",
        query=f"{direction}(MED:{pmid})",
        precision_query=None,
        selected_variant="sensitivity",
        request_parameters={"link_seed": pmid, "link_direction": direction},
        warnings=["One-hop citation traversal; apply protocol eligibility during screening."],
    )
    output = workspace.path / "searches" / ("citation-" + uuid4().hex[:12])
    workspace.reserve(output, strategy)
    store = RunStore(output)
    manifest = store.initialize(question, strategy, Credentials.from_env())
    manifest["research_parent"] = os.path.relpath(workspace.path, output)
    store.write_manifest(manifest)
    return {
        "run_dir": str(output),
        "strategy_digest": strategy_digest(strategy),
        "status": manifest["status"],
        "strategy": strategy.to_dict(),
    }
