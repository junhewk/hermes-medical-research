"""The narrow, deterministic ``hmr`` command-line interface."""

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
        prog="hmr",
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
    search_execute = search_commands.add_parser("execute")
    search_execute.add_argument("run_id")
    search_execute.add_argument("task_id", nargs="?")
    search_execute.add_argument("--from", dest="proposal", type=Path)
    search_execute.add_argument("--confirm-all")
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
    find = source_commands.add_parser(
        "find", help="Rank document locators by search words, with short snippets"
    )
    find.add_argument("run_id")
    find.add_argument("task_id")
    find.add_argument("words", nargs="+")
    find.add_argument("--source", dest="source_id")
    find.add_argument("--offset", type=int, default=0)
    find.add_argument("--limit", type=int, default=5)
    read = source_commands.add_parser("read", help="Read one document locator verbatim")
    read.add_argument("run_id")
    read.add_argument("task_id")
    read.add_argument("source_id")
    read.add_argument("locator")
    read.add_argument("--page", type=int, default=1)

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
    cancel = review_commands.add_parser("cancel")
    cancel.add_argument("name")
    cancel.add_argument("--reason", required=True)
    retry = review_commands.add_parser(
        "retry", help="Reopen a blocked latest Cycle in place with fresh task attempts"
    )
    retry.add_argument("name")
    retry.add_argument("--reason", required=True)

    intake = review_commands.add_parser(
        "intake", help="State the request in plain language and confirm the protocol"
    )
    intake.add_argument("text", nargs="?")
    intake.add_argument("--from-file", type=Path, dest="from_file")
    intake.add_argument("--list", action="store_true", dest="list_drafts")
    intake.add_argument("--show")
    intake.add_argument("--revise", nargs=2, metavar=("DRAFT_ID", "CLARIFICATION"))
    intake.add_argument("--confirm")
    intake.add_argument("--discard")
    intake.add_argument("--name")
    intake.add_argument("--schedule", default="once")
    intake.add_argument("--timezone", dest="timezone_name")
    intake.add_argument("--mode", choices=("report", "review-prep"), default="report")
    intake.add_argument("--records-per-source", type=int, dest="records")
    intake.add_argument("--fulltexts", type=int)
    intake.add_argument("--language", default="en")
    intake.add_argument("--hermes-home", type=Path)

    work = commands.add_parser("work", help="Operator diagnostics for the durable work queue")
    work_commands = work.add_subparsers(dest="action", required=True)
    work_commands.add_parser("tick", help=argparse.SUPPRESS)
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
    from .hermes import PROFILES

    bootstrap.add_argument("--profile", choices=tuple(PROFILES))
    bootstrap.add_argument("--remove", action="store_true")
    bootstrap.add_argument("--set-model", action="append", metavar="KIND=MODEL[@PROVIDER]")
    bootstrap.add_argument("--set-lane", action="append", metavar="KIND=call|agent")
    bootstrap.add_argument("--show", action="store_true")
    profiles_alias = hermes_commands.add_parser(
        "profiles", help="Create, check, remove, and assign models to the managed profiles"
    )
    for argument in bootstrap._actions[1:]:
        profiles_alias._add_action(argument)
    doctor_command = hermes_commands.add_parser("doctor")
    doctor_command.add_argument("--hermes-home", type=Path)
    quick = hermes_commands.add_parser(
        "commands", help="Install the step slash commands as Hermes quick commands"
    )
    quick_mode = quick.add_mutually_exclusive_group()
    quick_mode.add_argument("--apply", action="store_true")
    quick_mode.add_argument("--status", action="store_true")
    quick.add_argument("--remove", action="store_true")
    quick.add_argument("--review")
    quick.add_argument("--hermes-home", type=Path)
    routine_command = hermes_commands.add_parser(
        "routines", help="Report and remove the retired 0.5.x cron fleet"
    )
    routine_command.add_argument("--apply", action="store_true")
    routine_command.add_argument("--remove", action="store_true")
    routine_mode = routine_command.add_mutually_exclusive_group()
    routine_mode.add_argument("--status", action="store_true")
    routine_mode.add_argument("--pause-all", action="store_true")
    routine_mode.add_argument("--resume-all", action="store_true")
    routine_command.add_argument("--hermes-home", type=Path)

    mcp = commands.add_parser("mcp", help="Serve the task tools a Hermes session calls")
    mcp_commands = mcp.add_subparsers(dest="action", required=True)
    serve = mcp_commands.add_parser("serve")
    serve.add_argument("--role", required=True, choices=tuple(COMMAND_ROLES.values()))
    serve.add_argument("--kind", action="append")

    from .steps import STEPS

    step = commands.add_parser("step", help="Run one pipeline step yourself")
    step_commands = step.add_subparsers(dest="action", required=True)
    use = step_commands.add_parser("use", help="Point argument-less steps at one Review")
    use.add_argument("name", nargs="?")
    use.add_argument("--clear", action="store_true")
    for name in ("status", "next", "finalize"):
        view = step_commands.add_parser(name)
        view.add_argument("--review")
    stop = step_commands.add_parser("stop")
    stop.add_argument("--review")
    stop.add_argument("--step", choices=tuple(STEPS))
    retry = step_commands.add_parser("retry")
    retry.add_argument("--review")
    retry.add_argument("--reason", default="operator retried the blocked cycle from a step")
    prompt = step_commands.add_parser(
        "prompt", help="Print the prompt a constrained call would send, without sending it"
    )
    prompt.add_argument("step", choices=tuple(STEPS))
    prompt.add_argument("--review")
    for name in STEPS:
        runner = step_commands.add_parser(name)
        runner.add_argument("--review")
        runner.add_argument("--limit", type=int)
        runner.add_argument("--max-wait", type=float, default=40.0)
        runner.add_argument("--foreground", action="store_true")
        runner.add_argument("--hermes-home", type=Path)
        runner.add_argument("--hermes-executable", type=Path)
    worker = step_commands.add_parser("run", help=argparse.SUPPRESS)
    worker.add_argument("step", choices=tuple(STEPS))
    worker.add_argument("--review")
    worker.add_argument("--job")
    worker.add_argument("--limit", type=int)
    worker.add_argument("--max-wait", type=float, default=40.0)
    worker.add_argument("--hermes-home", type=Path)
    worker.add_argument("--hermes-executable", type=Path)
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
        if args.action == "intake":
            return _intake(args, catalog)
        if args.action == "run-now":
            return automation.trigger(args.name, scheduled=args.scheduled)
        if args.action == "cancel":
            return automation.cancel_review(args.name, _actor(args), reason=args.reason)
        if args.action == "retry":
            return automation.retry_review(args.name, _actor(args), reason=args.reason)
        return automation.acknowledge(args.event_id)

    if args.command == "work":
        from .automation import AutomationEngine

        automation = AutomationEngine(args.store)
        if args.action == "notifications":
            return automation.notification_probe()
        actor = _actor(args)
        if args.action == "tick":
            actor.require("coordinator")
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
        if args.action == "execute":
            return await engine.execute_search_plan(
                task_id,
                actor,
                proposal_path=args.proposal,
                confirm_all=args.confirm_all,
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
        if args.action == "find":
            return engine.source_find(
                args.task_id,
                " ".join(args.words),
                _actor(args),
                source_id=args.source_id,
                offset=args.offset,
                limit=args.limit,
                claim_token=args.claim_token,
            )
        if args.action == "read":
            return engine.source_read(
                args.task_id,
                args.source_id,
                args.locator,
                args.page,
                _actor(args),
                claim_token=args.claim_token,
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

    if args.command == "mcp":
        from .mcp_server import serve

        return {"_raw": str(serve(catalog.root, args.role, kinds=tuple(args.kind or ()) or None))}

    if args.command == "step":
        return await _step(args, catalog)

    from .hermes import (
        bootstrap_profiles,
        doctor,
        routine_status,
        routines,
        set_routines_paused,
    )

    if args.action in {"bootstrap", "profiles"}:
        from .hermes import step_assignment, write_assignment

        if args.set_model or args.set_lane:
            write_assignment(
                args.hermes_home,
                models=dict(_pairs(args.set_model)),
                lanes=dict(_pairs(args.set_lane)),
            )
        if args.show:
            return step_assignment(args.hermes_home)
        return bootstrap_profiles(
            apply=args.apply,
            hermes_home=args.hermes_home,
            source_profile=args.source_profile,
            profile=args.profile,
            store=catalog.root,
            remove=args.remove,
        )
    if args.action == "doctor":
        return doctor(hermes_home=args.hermes_home, store=catalog.root)
    if args.action == "commands":
        from .quick_commands import install
        from .quick_commands import status as quick_status

        if args.status:
            return quick_status(hermes_home=args.hermes_home, store=catalog.root,
                                review=args.review)
        return install(
            store=catalog.root,
            apply=args.apply,
            remove=args.remove,
            hermes_home=args.hermes_home,
            review=args.review,
        )
    if args.status:
        return routine_status(args.hermes_home)
    if args.pause_all or args.resume_all:
        return set_routines_paused(args.pause_all, hermes_home=args.hermes_home)
    return routines(
        apply=args.apply,
        remove=args.remove,
        hermes_home=args.hermes_home,
    )


def _pairs(values: list[str] | None) -> list[tuple[str, str]]:
    """``KIND=VALUE`` arguments, rejected early so a typo never reaches a config file."""
    pairs = []
    for item in values or []:
        key, separator, value = item.partition("=")
        if not separator or not key.strip() or not value.strip():
            raise ValidationError(f"expected KIND=VALUE, got {item!r}")
        pairs.append((key.strip(), value.strip()))
    return pairs


def _intake(args: argparse.Namespace, catalog: RunCatalog) -> Any:
    """Plain language in, a protocol to read, and only then a Review."""
    from . import intake

    store = catalog.root
    options = {
        "mode": args.mode,
        "records": args.records,
        "fulltexts": args.fulltexts,
        "language": args.language,
    }
    if args.list_drafts:
        return intake.list_drafts(store)
    if args.show:
        return {"_raw": intake.render(intake.read_draft(store, args.show))}
    if args.discard:
        return intake.discard(store, args.discard)
    if args.confirm:
        if not args.name:
            raise ValidationError("--confirm needs --name SLUG for the Review")
        return intake.confirm(
            store, args.confirm, name=args.name, schedule=args.schedule,
            timezone_name=args.timezone_name, **options,
        )
    if args.revise:
        draft_id, clarification = args.revise
        return {"_raw": intake.render(intake.revise(
            store, draft_id, clarification, hermes_home=args.hermes_home, **options
        ))}
    text = args.text
    if args.from_file is not None:
        text = sys.stdin.read() if str(args.from_file) == "-" else args.from_file.read_text()
    if not text:
        raise ValidationError("say what the review should answer, or pass --from-file")
    return {"_raw": intake.render(intake.propose(
        store, text, hermes_home=args.hermes_home, **options
    ))}


async def _step(args: argparse.Namespace, catalog: RunCatalog) -> Any:
    from . import steps

    store = catalog.root
    if args.action == "use":
        if args.clear:
            return steps.set_active_review(store, None)
        if not args.name:
            raise ValidationError("name a Review, or pass --clear")
        return steps.set_active_review(store, args.name)
    if args.action == "status":
        return {"_raw": steps.render_status(steps.status_view(store, args.review))}
    if args.action == "next":
        view = steps.status_view(store, args.review)
        return {"_raw": steps.render_next(view)}
    if args.action == "stop":
        return steps.request_stop(store, review=args.review, step=args.step)
    if args.action == "retry":
        from .automation import AutomationEngine

        name = steps.active_review(store, args.review)
        return AutomationEngine(store).retry_review(
            name, Actor("hmr-coordinator", f"step-retry-{os.getpid()}", "coordinator"),
            reason=args.reason,
        )
    if args.action == "finalize":
        return await steps.finalize(store, review=args.review)
    if args.action == "prompt":
        return {"_raw": steps.render_prompt(store, args.step, review=args.review)}
    if args.action == "run":
        return await steps.run_step(
            args.step,
            store=store,
            review=args.review,
            hermes_home=args.hermes_home,
            hermes_executable=args.hermes_executable,
            job_id=args.job,
            limit=args.limit,
            max_wait=args.max_wait,
        )
    if args.foreground:
        return await steps.run_step(
            args.action,
            store=store,
            review=args.review,
            hermes_home=args.hermes_home,
            hermes_executable=args.hermes_executable,
            limit=args.limit,
            max_wait=args.max_wait,
        )
    return {"_raw": steps.render_started(steps.start(
        args.action,
        store=store,
        review=args.review,
        hermes_home=args.hermes_home,
        limit=args.limit,
        max_wait=args.max_wait,
    ))}


GLOBAL_OPTIONS = ("--actor", "--session-id", "--claim-token", "--store")


def hoist_global_options(argv: list[str]) -> list[str]:
    """Move root options written after a subcommand to the front, preserving their values."""
    front: list[str] = []
    rest: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        name = item.split("=", 1)[0]
        if name in GLOBAL_OPTIONS:
            if "=" in item:
                front.append(item)
            elif index + 1 < len(argv):
                front.extend(argv[index : index + 2])
                index += 1
            else:
                rest.append(item)
        else:
            rest.append(item)
        index += 1
    return [*front, *rest]


def main(argv: list[str] | None = None) -> int:
    arguments = hoist_global_options(list(sys.argv[1:] if argv is None else argv))
    try:
        result = asyncio.run(dispatch(parser().parse_args(arguments)))
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
