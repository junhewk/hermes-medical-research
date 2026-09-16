"""The narrow, deterministic ``mdr`` command-line interface."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from hermes_medical_research import __version__
from hermes_medical_research.search.models import ValidationError

from .tasks import COMMAND_ROLES, Actor, RunCatalog, TaskEngine


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="mdr",
        description="Deterministic artifact boundary for Hermes medical-research bots.",
    )
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--actor", help="Hermes profile identity (normally supplied by the profile)")
    root.add_argument("--session-id", help="Stable Hermes chat/session identity")
    root.add_argument("--claim-token", help=argparse.SUPPRESS)
    root.add_argument("--store", type=Path, help=argparse.SUPPRESS)
    commands = root.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="Create, route, migrate, and inspect runs")
    run_commands = run.add_subparsers(dest="action", required=True)
    create = run_commands.add_parser("create", help="Create a run from a protocol request")
    create.add_argument("--request", type=Path, required=True)
    create.add_argument("--mode", choices=("report", "review-prep"), default="report")
    create.add_argument("--records-per-source", default=None)
    create.add_argument("--fulltexts", default=None)
    create.add_argument("--language", default="en")
    migrate = run_commands.add_parser("migrate", help="Import a validated v0.4 workspace")
    migrate.add_argument("legacy_path", type=Path)
    for action in ("status", "next"):
        item = run_commands.add_parser(action)
        item.add_argument("run_id")

    search = commands.add_parser("search", help="Perform bounded retrieval")
    search_commands = search.add_subparsers(dest="action", required=True)
    _add_next(search_commands, "search")
    search_commands.add_parser("claim", help="Claim the oldest available search task")
    _add_submit(search_commands, "search")
    search_run = search_commands.add_parser("run")
    search_run.add_argument("run_id")
    search_run.add_argument("task_id", nargs="?")
    search_run.add_argument("--confirm-all")
    approve = search_commands.add_parser("approve")
    approve.add_argument("run_id")
    approve.add_argument("task_id", nargs="?")
    approve.add_argument("--strategy-digest", required=True)

    for command in ("select", "extract", "synthesize", "audit"):
        role = commands.add_parser(command)
        role_commands = role.add_subparsers(dest="action", required=True)
        _add_next(role_commands, command)
        _add_submit(role_commands, command)
        role_commands.add_parser("claim", help=f"Claim the oldest available {command} task")

    source = commands.add_parser("source", help="Read one bounded source page")
    source_commands = source.add_subparsers(dest="action", required=True)
    list_command = source_commands.add_parser("list")
    list_command.add_argument("run_id")
    list_command.add_argument("task_id")
    list_command.add_argument("--page", type=int, default=1)
    show = source_commands.add_parser("show")
    show.add_argument("run_id")
    show.add_argument("task_id")
    show.add_argument("source_id")
    show.add_argument("--page", type=int, default=1)

    final = commands.add_parser("finalize", help="Verify and export a fully audited run")
    final.add_argument("run_id")
    final.add_argument("--offline", action="store_true")

    review = commands.add_parser("review", help="Manage human-named one-off and living Reviews")
    review_commands = review.add_subparsers(dest="action", required=True)
    create_review = review_commands.add_parser("create")
    _add_review_create(create_review)
    fork = review_commands.add_parser("fork")
    fork.add_argument("old_name")
    _add_review_create(fork)
    adopt = review_commands.add_parser("adopt")
    adopt.add_argument("--name", required=True)
    adopt.add_argument("run_id")
    adopt.add_argument("--schedule", required=True)
    adopt.add_argument("--timezone", default=_default_timezone())
    review_commands.add_parser("list")
    for action in ("status", "pause", "resume", "run-now"):
        item = review_commands.add_parser(action)
        item.add_argument("name")
        if action == "run-now":
            item.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    acknowledge = review_commands.add_parser("acknowledge")
    acknowledge.add_argument("event_id")

    work = commands.add_parser("work", help="Drive the durable cron work queue")
    work_commands = work.add_subparsers(dest="action", required=True)
    probe = work_commands.add_parser("probe")
    probe.add_argument("role", choices=tuple(COMMAND_ROLES.values()))
    work_commands.add_parser("tick")
    work_commands.add_parser("cron-tick", help=argparse.SUPPRESS)
    fail = work_commands.add_parser("fail")
    fail.add_argument("claim_id")
    fail.add_argument("--code", required=True)
    fail.add_argument("--message", required=True)
    work_commands.add_parser("notifications")

    hermes = commands.add_parser("hermes", help="Bootstrap and inspect isolated Hermes profiles")
    hermes_commands = hermes.add_subparsers(dest="action", required=True)
    bootstrap = hermes_commands.add_parser("bootstrap")
    bootstrap.add_argument("--apply", action="store_true")
    bootstrap.add_argument("--hermes-home", type=Path)
    bootstrap.add_argument("--source-profile", type=Path)
    doctor_command = hermes_commands.add_parser("doctor")
    doctor_command.add_argument("--hermes-home", type=Path)
    routine_command = hermes_commands.add_parser("routines")
    routine_command.add_argument("--apply", action="store_true")
    routine_command.add_argument("--hermes-home", type=Path)
    return root


def _default_timezone() -> str:
    configured = os.environ.get("TZ")
    if configured and "/" in configured:
        return configured
    timezone_file = Path("/etc/timezone")
    if timezone_file.is_file():
        value = timezone_file.read_text(encoding="utf-8").strip()
        if value:
            return value
    return "UTC"


def _add_review_create(command: argparse.ArgumentParser) -> None:
    command.add_argument("--name", required=True)
    command.add_argument("--request", type=Path, required=True)
    command.add_argument("--schedule", required=True)
    command.add_argument("--timezone", default=_default_timezone())
    command.add_argument("--mode", choices=("report", "review-prep"), default="report")
    command.add_argument("--records-per-source", default=None)
    command.add_argument("--fulltexts", default=None)
    command.add_argument("--language", default="en")


def _add_next(commands: argparse._SubParsersAction, name: str) -> None:
    item = commands.add_parser("next", help=f"Open the next {name} task")
    item.add_argument("run_id")
    item.add_argument("task_id", nargs="?")


def _add_submit(commands: argparse._SubParsersAction, name: str) -> None:
    item = commands.add_parser("submit", help=f"Validate and record one {name} proposal")
    item.add_argument("run_id")
    item.add_argument("task_id", nargs="?")
    item.add_argument("--from", dest="proposal", type=Path, required=True)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValidationError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"invalid JSON in {path}: {exc}") from exc


def _limit(value: str | None) -> int | str | None:
    if value is None or value == "all":
        return value
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValidationError("limits must be positive integers or 'all'") from exc
    if parsed < 1:
        raise ValidationError("limits must be positive integers or 'all'")
    return parsed


def _actor(args: argparse.Namespace) -> Actor:
    return Actor.resolve(args.actor, args.session_id)


def _engine(catalog: RunCatalog, run_id: str) -> TaskEngine:
    return TaskEngine(catalog.workspace(run_id))


def _task_id(engine: TaskEngine, command: str, supplied: str | None) -> str:
    if supplied:
        return supplied
    role = COMMAND_ROLES[command]
    active = [
        item
        for item in engine.status()["active"]
        if item["role"] == role and item["state"] in {"pending", "in_progress"}
    ]
    if len(active) != 1:
        raise ValidationError(f"task_id is required unless exactly one active {role} task exists")
    return active[0]["task_id"]


async def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    catalog = RunCatalog(args.store)
    if args.command == "review":
        from .automation import AutomationEngine

        automation = AutomationEngine(args.store)
        if args.action in {"create", "fork"}:
            request = _load_json(args.request)
            if not isinstance(request, dict):
                raise ValidationError("request must be a JSON object")
            values = {
                "schedule": args.schedule,
                "timezone": args.timezone,
                "mode": args.mode,
                "records": _limit(args.records_per_source),
                "fulltexts": _limit(args.fulltexts),
                "language": args.language,
            }
            if args.action == "create":
                return automation.create_review(args.name, request, **values)
            return automation.fork_review(args.old_name, args.name, request, **values)
        if args.action == "adopt":
            return automation.adopt_review(
                args.name,
                args.run_id,
                schedule=args.schedule,
                timezone=args.timezone,
            )
        if args.action == "list":
            return automation.list_reviews()
        if args.action == "status":
            return automation.review_status(args.name)
        if args.action == "pause":
            return automation.set_paused(args.name, True)
        if args.action == "resume":
            return automation.set_paused(args.name, False)
        if args.action == "run-now":
            return automation.trigger(args.name, scheduled=args.scheduled)
        return automation.acknowledge(args.event_id)

    if args.command == "work":
        from .automation import AutomationEngine

        automation = AutomationEngine(args.store)
        if args.action == "probe":
            return automation.probe(args.role)
        if args.action == "notifications":
            return automation.notification_probe()
        actor = _actor(args)
        if args.action in {"tick", "cron-tick"}:
            actor.require("coordinator")
            if args.action == "cron-tick":
                return {"_raw": await automation.cron_tick()}
            return await automation.tick()
        return automation.fail(args.claim_id, actor, code=args.code, message=args.message)

    if args.command == "run":
        if args.action == "create":
            request = _load_json(args.request)
            if not isinstance(request, dict):
                raise ValidationError("request must be a JSON object")
            return catalog.create(
                request,
                mode=args.mode,
                records=_limit(args.records_per_source),
                fulltexts=_limit(args.fulltexts),
                language=args.language,
            )
        if args.action == "migrate":
            return catalog.migrate(args.legacy_path)
        engine = _engine(catalog, args.run_id)
        if args.action == "status":
            return engine.status()
        return engine.route_next(_actor(args))

    if args.command in COMMAND_ROLES:
        if args.action == "claim":
            from .automation import AutomationEngine

            return AutomationEngine(args.store).claim(args.command, _actor(args))
        engine = _engine(catalog, args.run_id)
        task_id = _task_id(engine, args.command, args.task_id)
        actor = _actor(args)
        if args.action == "next":
            return engine.role_next(
                args.command, task_id, actor, claim_token=args.claim_token
            )
        if args.action == "submit":
            return await engine.submit(
                args.command,
                task_id,
                args.proposal,
                actor,
                claim_token=args.claim_token,
            )
        if args.action == "run":
            return await engine.run_search(
                task_id,
                actor,
                confirm_all=args.confirm_all,
                claim_token=args.claim_token,
            )
        return engine.approve_search(
            task_id, args.strategy_digest, actor, claim_token=args.claim_token
        )

    if args.command == "source":
        engine = _engine(catalog, args.run_id)
        if args.action == "list":
            return engine.source_list(
                args.task_id, args.page, _actor(args), claim_token=args.claim_token
            )
        return engine.source_show(
            args.task_id,
            args.source_id,
            args.page,
            _actor(args),
            claim_token=args.claim_token,
        )

    if args.command == "finalize":
        return await _engine(catalog, args.run_id).finalize(_actor(args), offline=args.offline)

    from .hermes import bootstrap_profiles, doctor, routines

    if args.action == "bootstrap":
        return bootstrap_profiles(
            apply=args.apply,
            hermes_home=args.hermes_home,
            source_profile=args.source_profile,
        )
    if args.action == "doctor":
        return doctor(hermes_home=args.hermes_home)
    return routines(
        apply=args.apply,
        hermes_home=args.hermes_home,
        store=args.store,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        result = asyncio.run(dispatch(parser().parse_args(argv)))
    except (ValidationError, OSError, KeyError, TypeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    if set(result) == {"_raw"}:
        if result["_raw"]:
            print(result["_raw"])
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
