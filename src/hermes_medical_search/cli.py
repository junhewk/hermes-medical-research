from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from .artifacts import RunStore, confirmation_token, run_directory
from .config import Credentials
from .http import HttpSession
from .models import SOURCES, Question, Strategy, ValidationError
from .orchestrator import execute_search, preflight
from .providers import MeshResolver, provider_for
from .query import compile_strategy, default_sources


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes-medical-search",
        description="Plan and run reproducible PICO/PCC medical-literature searches.",
    )
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="Check source configuration and access")
    doctor.add_argument("--sources", help="Comma-separated source names; defaults to all")
    doctor.add_argument("--offline", action="store_true", help="Check configuration only")
    doctor.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    plan = commands.add_parser("plan", help="Validate a question and compile source strategies")
    _add_question_arguments(plan, review_mode=True)

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
    parser.add_argument("--no-mesh", action="store_true", help="Skip online MeSH resolution")


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
        run_path, _ = await _create_plan(args, mode=args.mode)
        print(run_path)
        return 0
    if args.command == "preflight":
        return await _preflight_command(args.run_dir)
    if args.command == "search":
        return await _search_command(args.run_dir, confirm_all=args.confirm_all)
    if args.command == "run":
        run_path, strategy = await _create_plan(args, mode="quick")
        credentials = Credentials.from_env()
        async with _session(credentials) as session:
            result = await preflight(strategy, session, credentials)
            store = RunStore(run_path)
            store.write_json("preflight.json", result)
            summary = await execute_search(strategy, store, session, credentials, result)
        print(json.dumps({"run_dir": str(run_path), "summary": summary}, indent=2))
        return 0
    raise ValueError(f"unsupported command {args.command}")


async def _create_plan(args: argparse.Namespace, *, mode: str) -> tuple[Path, Strategy]:
    question = _load_question(args.input)
    credentials = Credentials.from_env()
    if mode == "quick" and not question.filters.from_date:
        question.filters.from_date = _years_ago(date.today(), 3).isoformat()
    limit = _parse_limit(args.limit_per_source, mode=mode)
    sources = _select_sources(question, credentials, args.sources, args.exclude)
    warnings: list[str] = []
    if not args.no_mesh:
        async with _session(credentials) as session:
            warnings.extend(await MeshResolver(session, credentials).resolve_question(question))
    else:
        warnings.append("MeSH resolution was explicitly skipped.")
    strategy = compile_strategy(
        question,
        mode=mode,
        limit_per_source=limit,
        sources=sources,
        precision=args.precision,
    )
    strategy.warnings.extend(warnings)
    if mode == "quick" and not _input_has_from_date(args.input):
        strategy.warnings.append(
            f"Quick mode applied its visible three-year default from {question.filters.from_date}."
        )
    run_path = args.output or run_directory(Path.cwd() / "runs", question)
    store = RunStore(run_path.resolve())
    store.initialize(question, strategy, credentials)
    return store.path, strategy


async def _preflight_command(run_dir: Path) -> int:
    store = RunStore(run_dir.resolve())
    strategy = _load_strategy(store.path / "strategy.json")
    credentials = Credentials.from_env()
    async with _session(credentials) as session:
        result = await preflight(strategy, session, credentials)
    store.write_json("preflight.json", result)
    print(json.dumps(result, indent=2))
    return 0 if result["ready"] or strategy.mode == "quick" else 2


async def _search_command(run_dir: Path, *, confirm_all: str | None) -> int:
    store = RunStore(run_dir.resolve())
    strategy = _load_strategy(store.path / "strategy.json")
    credentials = Credentials.from_env()
    async with _session(credentials) as session:
        result = store.read_json("preflight.json", default=None)
        if not result:
            result = await preflight(strategy, session, credentials)
            store.write_json("preflight.json", result)
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
        summary = await execute_search(strategy, store, session, credentials, result)
    print(json.dumps(summary, indent=2))
    return 2 if strategy.mode == "review" and summary["source_failures"] else 0


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
                    return source, str(exc), None

            results = await asyncio.gather(*(check(source) for source in selected))
        for source, error, count in results:
            statuses[source]["live"] = "available" if error is None else "unavailable"
            statuses[source]["test_query_count"] = count
            statuses[source]["error"] = error
    ready = all(
        detail.get("configured") and (args.offline or detail.get("live") == "available")
        for detail in statuses.values()
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


def _input_has_from_date(path: Path) -> bool:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    return bool((data.get("filters") or {}).get("from_date"))


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
