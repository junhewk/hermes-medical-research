from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from contextlib import AsyncExitStack
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from hermes_medical_research import __version__

from .artifacts import (
    ARTIFACT_SCHEMA_VERSION,
    RunStore,
    confirmation_token,
    preflight_digest,
    run_directory,
    strategy_digest,
)
from .config import Credentials
from .http import HttpSession
from .models import SOURCES, Question, Strategy, ValidationError
from .orchestrator import execute_search, preflight
from .providers import MeshResolver, provider_for
from .query import compile_strategy, default_sources


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hmr-search-core",
        description="Internal medical literature retrieval commands.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="Check source configuration and access")
    doctor.add_argument("--sources", help="Comma-separated source names; defaults to all")
    doctor.add_argument("--offline", action="store_true", help="Check configuration only")
    doctor.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    plan = commands.add_parser("plan", help="Validate a question and compile source strategies")
    _add_question_arguments(plan, review_mode=True)
    plan.add_argument("--json", action="store_true", help="Emit the complete plan as JSON")

    approve = commands.add_parser("approve", help="Record approval of an exact review strategy")
    approve.add_argument("run_dir", type=Path)
    approve.add_argument(
        "--strategy-digest",
        required=True,
        help="Full digest shown with the strategy under review",
    )

    inspect = commands.add_parser("preflight", help="Count matches and validate source access")
    inspect.add_argument("run_dir", type=Path)

    search = commands.add_parser("search", help="Execute or resume an approved strategy")
    search.add_argument("run_dir", type=Path)
    search.add_argument(
        "--confirm-all",
        metavar="TOKEN",
        help="Confirmation token emitted by preflight for an all-results search",
    )

    run = commands.add_parser("run", help="Plan and execute a bounded quick search")
    _add_question_arguments(run, review_mode=False)
    return parser


def _add_question_arguments(parser: argparse.ArgumentParser, *, review_mode: bool) -> None:
    parser.add_argument("input", type=Path, help="Versioned PICO/PCC question JSON")
    if review_mode:
        parser.add_argument("--mode", choices=("quick", "review"), default="review")
    parser.add_argument("--output", type=Path, help="Run directory; defaults under ./runs")
    parser.add_argument("--limit-per-source", help="Positive integer or 'all'")
    parser.add_argument("--sources", help="Comma-separated source names")
    parser.add_argument("--exclude", help="Comma-separated sources to exclude")
    parser.add_argument("--precision", action="store_true", help="Select optional precision blocks")
    parser.add_argument(
        "--variant",
        action="append",
        default=[],
        metavar="SOURCE=VARIANT",
        help="Select sensitivity or precision independently for a source; repeatable",
    )
    parser.add_argument("--no-mesh", action="store_true", help="Skip online MeSH resolution")
    parser.add_argument(
        "--research-run", type=Path, help="Bind this search to a research protocol and budget"
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        exit_code = asyncio.run(_dispatch(args))
    except (ValidationError, ValueError, OSError, json.JSONDecodeError) as exc:
        parser.exit(2, f"error: {exc}\n")
    except KeyboardInterrupt:
        parser.exit(130, "interrupted\n")
    raise SystemExit(exit_code)


async def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "doctor":
        return await _doctor(args)
    if args.command == "plan":
        run_path, strategy = await _create_plan(args, mode=args.mode)
        if args.json:
            print(
                json.dumps(
                    {
                        "run_dir": str(run_path),
                        "strategy_digest": strategy_digest(strategy),
                        "status": "awaiting_strategy_approval"
                        if strategy.mode == "review"
                        else "planned",
                        "strategy": strategy.to_dict(),
                    },
                    indent=2,
                )
            )
        else:
            print(run_path)
        return 0
    if args.command == "approve":
        return await _approve_command(args.run_dir, args.strategy_digest)
    if args.command == "preflight":
        return await _preflight_command(args.run_dir)
    if args.command == "search":
        return await _search_command(args.run_dir, confirm_all=args.confirm_all)
    if args.command == "run":
        credentials = Credentials.from_env()
        async with _session(credentials) as session:
            run_path, strategy = await _create_plan(args, mode="quick", session=session)
            result = await preflight(strategy, session, credentials)
            store = RunStore(run_path)
            store.write_json("preflight.json", result)
            summary = await execute_search(strategy, store, session, credentials, result)
        print(json.dumps({"run_dir": str(run_path), "summary": summary}, indent=2))
        return 0
    raise ValueError(f"unsupported command {args.command}")


async def _create_plan(
    args: argparse.Namespace, *, mode: str, session: HttpSession | None = None
) -> tuple[Path, Strategy]:
    question = _load_question(args.input)
    credentials = Credentials.from_env()
    supplied_from_date = bool(question.filters.from_date)
    apply_date_default = (
        mode == "quick" and question.schema_version != "3" and not question.filters.from_date
    )
    if apply_date_default:
        question.filters.from_date = _years_ago(date.today(), 3).isoformat()
    limit = _parse_limit(args.limit_per_source, mode=mode)
    sources = _select_sources(question, credentials, args.sources, args.exclude)
    variants = _parse_variants(args.variant, sources=sources, precision=args.precision)
    warnings: list[str] = []
    if not args.no_mesh:
        async with AsyncExitStack() as stack:
            active = session or await stack.enter_async_context(_session(credentials))
            warnings.extend(await MeshResolver(active, credentials).resolve_question(question))
    else:
        warnings.append("MeSH resolution was explicitly skipped.")
    strategy = compile_strategy(
        question,
        mode=mode,
        limit_per_source=limit,
        sources=sources,
        variants=variants,
    )
    strategy.warnings.extend(warnings)
    if args.precision:
        strategy.warnings.append(
            "--precision is deprecated; use repeatable --variant SOURCE=precision."
        )
    if question.migrated_from_schema:
        strategy.warnings.append(
            "Question schema v1 was upgraded to schema v2 by wrapping each component "
            "in one labeled group."
        )
    if apply_date_default and not supplied_from_date:
        strategy.warnings.append(
            f"Quick mode applied its visible three-year default from {question.filters.from_date}."
        )
    research_path = getattr(args, "research_run", None)
    if research_path:
        from uuid import uuid4

        from hermes_medical_research.workspace import Workspace

        workspace = Workspace(research_path)
        run_path = args.output or workspace.path / "searches" / ("search-" + uuid4().hex[:12])
        with workspace.lock:
            workspace.reserve(run_path, strategy)
    else:
        run_path = args.output or run_directory(Path.cwd() / "runs", question)
    store = RunStore(run_path.resolve())
    manifest = store.initialize(question, strategy, credentials)
    if research_path:
        manifest["research_parent"] = os.path.relpath(research_path.resolve(), store.path)
        store.write_manifest(manifest)
    return store.path, strategy


async def _preflight_command(run_dir: Path) -> int:
    store = RunStore(run_dir.resolve())
    strategy, manifest = _load_run(store)
    if strategy.mode == "review":
        _require_strategy_approval(store, strategy)
        if manifest.get("status") not in {
            "strategy_approved",
            "preflight_ready",
            "preflight_failed",
        }:
            raise ValueError(
                f"review preflight is invalid while run status is {manifest.get('status')!r}"
            )
    credentials = Credentials.from_env()
    async with _session(credentials) as session:
        result = await preflight(strategy, session, credentials)
    store.write_json("preflight.json", result)
    manifest["status"] = "preflight_ready" if result["ready"] else "preflight_failed"
    store.write_manifest(manifest)
    print(json.dumps(result, indent=2))
    return 0 if result["ready"] or strategy.mode == "quick" else 2


async def _search_command(run_dir: Path, *, confirm_all: str | None) -> int:
    store = RunStore(run_dir.resolve())
    strategy, manifest = _load_run(store)
    if strategy.mode == "review":
        _require_strategy_approval(store, strategy)
    credentials = Credentials.from_env()
    async with _session(credentials) as session:
        result = store.read_json("preflight.json", default=None)
        if not result and strategy.mode == "review":
            raise ValueError(
                "review search requires an explicit successful preflight after approval"
            )
        if not result:
            result = await preflight(strategy, session, credentials)
            store.write_json("preflight.json", result)
        _validate_preflight(strategy, result)
        if strategy.mode == "review" and manifest.get("status") not in {
            "preflight_ready",
            "preflight_failed",
            "running",
            "failed",
            "complete",
        }:
            raise ValueError(
                f"review search is invalid while run status is {manifest.get('status')!r}"
            )
        if strategy.mode == "review" and not result.get("ready"):
            unavailable = [
                source
                for source, detail in result.get("sources", {}).items()
                if detail.get("status") != "available"
            ]
            raise ValueError(
                "review preflight failed for: "
                + ", ".join(unavailable)
                + "; configure or exclude these sources and create a new strategy"
            )
        if strategy.limit_per_source == "all":
            counts = {
                source: int(detail["count"])
                for source, detail in result.get("sources", {}).items()
                if detail.get("status") == "available"
            }
            expected = confirmation_token(strategy, counts)
            if not confirm_all or confirm_all != expected:
                raise ValueError(
                    "all-results retrieval requires --confirm-all with the token from preflight"
                )
            approval = _require_strategy_approval(store, strategy)
            approval["all_results"] = {
                "strategy_digest": strategy_digest(strategy),
                "preflight_digest": result["preflight_digest"],
                "expected_total": sum(counts.values()),
                "confirmed_at": datetime.now(UTC).isoformat(),
            }
            store.write_json("approval.json", approval)
        summary = await execute_search(strategy, store, session, credentials, result)
    print(json.dumps(summary, indent=2))
    return 2 if strategy.mode == "review" and summary["source_failures"] else 0


async def _approve_command(run_dir: Path, supplied_digest: str) -> int:
    store = RunStore(run_dir.resolve())
    strategy, manifest = _load_run(store)
    if strategy.mode != "review":
        raise ValueError("only review strategies require approval")
    current_digest = strategy_digest(strategy)
    if supplied_digest != current_digest:
        raise ValueError("supplied strategy digest does not match the stored strategy")
    existing = store.read_json("approval.json", default=None)
    if existing:
        approved = (existing.get("strategy") or {}).get("strategy_digest")
        if approved != current_digest:
            raise ValueError("approval.json belongs to a different strategy")
        print(json.dumps(existing, indent=2))
        return 0
    approval = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "strategy": {
            "strategy_digest": current_digest,
            "approved_at": datetime.now(UTC).isoformat(),
            "selected_variants": {
                source: item.selected_variant for source, item in strategy.strategies.items()
            },
        },
        "all_results": None,
    }
    store.write_json("approval.json", approval)
    manifest["status"] = "strategy_approved"
    store.write_manifest(manifest)
    print(json.dumps(approval, indent=2))
    return 0


def _load_run(store: RunStore) -> tuple[Strategy, dict[str, Any]]:
    strategy = _load_strategy(store.path / "strategy.json")
    manifest = store.read_json("manifest.json", default=None)
    if not isinstance(manifest, dict):
        raise ValueError("run directory has no manifest.json")
    if str(manifest.get("schema_version")) != ARTIFACT_SCHEMA_VERSION:
        raise ValueError("v0.1 run directories must be re-planned with v0.2")
    if manifest.get("strategy_digest") != strategy_digest(strategy):
        raise ValueError("strategy.json does not match the immutable run manifest")
    return strategy, manifest


def _require_strategy_approval(store: RunStore, strategy: Strategy) -> dict[str, Any]:
    approval = store.read_json("approval.json", default=None)
    if not isinstance(approval, dict):
        raise ValueError("review strategy has not been approved")
    strategy_approval = approval.get("strategy") or {}
    if strategy_approval.get("strategy_digest") != strategy_digest(strategy):
        raise ValueError("review approval does not match the stored strategy")
    return approval


def _validate_preflight(strategy: Strategy, result: dict[str, Any]) -> None:
    if result.get("strategy_digest") != strategy_digest(strategy):
        raise ValueError("preflight does not match the stored strategy")
    if result.get("preflight_digest") != preflight_digest(result):
        raise ValueError("preflight artifact digest is invalid")


async def _doctor(args: argparse.Namespace) -> int:
    credentials = Credentials.from_env()
    selected = _parse_sources(args.sources) if args.sources else list(SOURCES)
    configuration = credentials.configuration_status()
    statuses: dict[str, dict[str, Any]] = {
        source: dict(configuration[source]) for source in selected
    }
    if not args.offline:
        question = Question.from_dict(
            {
                "schema_version": "1",
                "framework": "PICO",
                "question": "Health intervention literature access check",
                "components": {
                    "population": {"text": "humans"},
                    "intervention": {"text": "health intervention"},
                },
            }
        )
        strategy = compile_strategy(
            question,
            mode="quick",
            limit_per_source=1,
            sources=selected,
        )
        async with _session(credentials) as session:

            async def check(source: str) -> tuple[str, str | None, int | None]:
                try:
                    count = await provider_for(source, session, credentials).count(
                        strategy.strategies[source]
                    )
                    return source, None, count
                except Exception as exc:
                    return source, credentials.redact(str(exc)), None

            results = await asyncio.gather(*(check(source) for source in selected))
        for source, error, count in results:
            statuses[source]["live"] = "available" if error is None else "unavailable"
            statuses[source]["test_query_count"] = count
            statuses[source]["error"] = error
    # Scopus is opt-in, so an unconfigured Scopus must not fail a doctor run the user never
    # asked to include. Explicitly requested sources are always held to the full standard.
    optional = set() if args.sources else {"scopus"}
    ready = all(
        detail.get("configured") and (args.offline or detail.get("live") == "available")
        for source, detail in statuses.items()
        if source not in optional or detail.get("configured")
    )
    payload = {"ready": ready, "sources": statuses}
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        for source, detail in statuses.items():
            state = detail.get("live") or ("configured" if detail["configured"] else "unconfigured")
            print(f"{source}: {state}")
            if detail.get("error"):
                print(f"  {detail['error']}")
    return 0 if ready else 2


def _load_question(path: Path) -> Question:
    with path.open(encoding="utf-8") as handle:
        return Question.from_dict(json.load(handle))


def _load_strategy(path: Path) -> Strategy:
    with path.open(encoding="utf-8") as handle:
        return Strategy.from_dict(json.load(handle))


def _parse_limit(raw: str | None, *, mode: str) -> int | str:
    if raw is None:
        if mode == "review":
            raise ValidationError("review mode requires --limit-per-source N or all")
        return 20
    if raw.casefold() == "all":
        if mode != "review":
            raise ValidationError("all-results retrieval is available only in review mode")
        return "all"
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValidationError("--limit-per-source must be a positive integer or all") from exc
    if value <= 0:
        raise ValidationError("--limit-per-source must be positive")
    return value


def _select_sources(
    question: Question,
    credentials: Credentials,
    cli_sources: str | None,
    cli_exclude: str | None,
) -> list[str]:
    selected = (
        _parse_sources(cli_sources)
        if cli_sources
        else question.sources or default_sources(scopus_configured=bool(credentials.scopus_api_key))
    )
    excluded = set(question.exclude_sources)
    if cli_exclude:
        excluded.update(_parse_sources(cli_exclude))
    result = [source for source in selected if source not in excluded]
    if not result:
        raise ValidationError("all sources were excluded")
    return result


def _parse_sources(raw: str) -> list[str]:
    values = list(dict.fromkeys(part.strip().casefold() for part in raw.split(",") if part.strip()))
    unknown = sorted(set(values) - set(SOURCES))
    if unknown:
        raise ValidationError(f"unsupported sources: {', '.join(unknown)}")
    if not values:
        raise ValidationError("source list is empty")
    return values


def _parse_variants(values: list[str], *, sources: list[str], precision: bool) -> dict[str, str]:
    if precision and values:
        raise ValidationError("--precision cannot be combined with --variant")
    if precision:
        return {source: "precision" for source in sources}
    variants: dict[str, str] = {}
    for raw in values:
        source, separator, variant = raw.partition("=")
        source = source.strip().casefold()
        variant = variant.strip().casefold()
        if (
            not separator
            or source not in SOURCES
            or variant
            not in {
                "sensitivity",
                "precision",
            }
        ):
            raise ValidationError("--variant must use SOURCE=sensitivity or SOURCE=precision")
        if source not in sources:
            raise ValidationError(f"--variant source is not selected: {source}")
        if source in variants:
            raise ValidationError(f"duplicate --variant for source: {source}")
        variants[source] = variant
    return variants


def _years_ago(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def _session(credentials: Credentials) -> HttpSession:
    return HttpSession(
        intervals={
            "ncbi": 0.1 if credentials.ncbi_api_key else 0.34,
            "semantic-scholar": 1.0,
        }
    )


if __name__ == "__main__":
    main(sys.argv[1:])
