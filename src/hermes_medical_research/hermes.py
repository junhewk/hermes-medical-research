"""Supported Hermes profile bootstrap without plugin or private-agent APIs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from hermes_medical_research import __version__
from hermes_medical_research.search.models import ValidationError

PROFILE_SKILLS = {
    "mdr-coordinator": (),
    "mdr-searcher": ("medical-search",),
    "mdr-selector": ("medical-select",),
    "mdr-extractor": ("medical-extract",),
    "mdr-synthesizer": ("medical-synthesize",),
    "mdr-auditor": ("medical-synthesize",),
}
DESCRIPTIONS = {
    "mdr-coordinator": "Routes opaque medical-research task IDs; never performs specialist work.",
    "mdr-searcher": "Builds and runs bounded, reproducible medical searches.",
    "mdr-selector": "Screens and selects one supplied medical record at a time.",
    "mdr-extractor": "Acquires full text, links studies, extracts results, and appraises them.",
    "mdr-synthesizer": "Synthesizes one protocol outcome at a time.",
    "mdr-auditor": "Independently audits frozen evidence and report assertions.",
}
MANAGED = "mdr-managed.json"
ROUTINES_MANAGED = "mdr-routines.json"
TOOLSETS = ["terminal", "file", "skills"]
WORKER_ROLES = ("searcher", "selector", "extractor", "synthesizer", "auditor")
ROLE_SKILLS = {
    "searcher": "medical-search",
    "selector": "medical-select",
    "extractor": "medical-extract",
    "synthesizer": "medical-synthesize",
    "auditor": "medical-synthesize",
}
ROLE_COMMANDS = {
    "searcher": "search",
    "selector": "select",
    "extractor": "extract",
    "synthesizer": "synthesize",
    "auditor": "audit",
}
# Per-session model-turn ceilings for one claimed Task.  An assessment reads each protocol
# outcome's locations and edits one proposal; an audit group checks several targets.
PROFILE_MAX_TURNS = {
    "mdr-searcher": 8,
    "mdr-selector": 16,
    "mdr-extractor": 60,
    "mdr-synthesizer": 40,
    "mdr-auditor": 48,
}
WORKER_SCRIPT_TIMEOUT_SECONDS = 24 * 60 * 60
HOST_ATTEMPTS = 3
HOST_TIMEOUT_SECONDS = 20 * 60
LEASE_RESERVE_SECONDS = 5 * 60


def _home(value: Path | None) -> Path:
    configured = value or (Path(os.environ["HERMES_HOME"]) if os.getenv("HERMES_HOME") else None)
    return (configured or Path.home() / ".hermes").expanduser().resolve()


def _profile_root(home: Path, name: str) -> Path:
    return home / "profiles" / name


def _system_timezone() -> str:
    configured = os.environ.get("TZ")
    if configured and "/" in configured:
        return configured
    path = Path("/etc/timezone")
    if path.is_file():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    return "UTC"


def _version(executable: str | None) -> str | None:
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except OSError:
        return None
    output = (completed.stdout or completed.stderr).strip()
    return output.splitlines()[0] if output else None


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _resource(path: str) -> bytes:
    return resources.files("hermes_medical_research").joinpath("resources", path).read_bytes()


def _source_settings(source_profile: Path | None, home: Path) -> dict[str, Any]:
    candidate = source_profile
    if candidate is None:
        candidate = home / "config.yaml"
    elif candidate.is_dir():
        candidate = candidate / "config.yaml"
    if not candidate.is_file():
        return {}
    try:
        value = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValidationError(f"cannot read source Hermes config {candidate}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError("source Hermes config must be a mapping")
    selected = {key: value[key] for key in ("model", "provider") if key in value}
    if isinstance(value.get("timezone"), str) and value["timezone"].strip():
        selected["timezone"] = value["timezone"].strip()
    model = value.get("model")
    provider_name = (
        model.get("provider") if isinstance(model, dict) else value.get("provider")
    )
    providers = value.get("providers")
    if (
        isinstance(provider_name, str)
        and isinstance(providers, dict)
        and provider_name in providers
    ):
        selected["providers"] = {provider_name: providers[provider_name]}
    custom = value.get("custom_providers")
    if isinstance(custom, list) and isinstance(model, dict):
        selected_base = str(model.get("base_url") or "").rstrip("/")
        selected_model = str(model.get("default") or "")
        matches = [
            item
            for item in custom
            if isinstance(item, dict)
            and (
                str(item.get("base_url") or "").rstrip("/").removesuffix("/v1")
                == selected_base.removesuffix("/v1")
                or (
                    selected_model
                    and selected_model
                    in {
                        str(item.get("model") or ""),
                        *(str(name) for name in item.get("models", []) if name),
                    }
                )
            )
        ]
        if matches:
            selected["custom_providers"] = matches
    return selected


def _desired_files(name: str, settings: dict[str, Any]) -> dict[str, bytes]:
    model = settings.get("model")
    if isinstance(model, dict):
        cron_model = model.get("default")
        cron_provider = model.get("provider")
    else:
        cron_model = model
        cron_provider = settings.get("provider")
    cron = {
        key: value
        for key, value in (
            ("model", cron_model),
            ("model_provider", cron_provider),
        )
        if isinstance(value, str) and value.strip()
    }
    if name != "mdr-coordinator":
        # Each worker Routine is a serial runner that may drain its queue for hours.
        cron["script_timeout_seconds"] = WORKER_SCRIPT_TIMEOUT_SECONDS
    config = {
        **settings,
        "timezone": settings.get("timezone", _system_timezone()),
        **({"cron": cron} if cron else {}),
        **(
            {"agent": {"max_turns": PROFILE_MAX_TURNS[name]}}
            if name in PROFILE_MAX_TURNS
            else {}
        ),
        "tools": {"enabled_toolsets": TOOLSETS},
    }
    files = {
        "SOUL.md": _resource(f"profiles/{name}.md"),
        "config.yaml": yaml.safe_dump(config, sort_keys=False).encode(),
    }
    for skill in PROFILE_SKILLS[name]:
        files[f"skills/{skill}/SKILL.md"] = _resource(f"skills/{skill}/SKILL.md")
    return files


def _read_manifest(root: Path) -> dict[str, Any] | None:
    path = root / MANAGED
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"invalid managed-profile manifest at {path}: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("files"), dict):
        raise ValidationError(f"invalid managed-profile manifest at {path}")
    return value


def _check_ownership(root: Path, desired: dict[str, bytes]) -> None:
    manifest = _read_manifest(root)
    if root.exists() and manifest is None and any(root.iterdir()):
        raise ValidationError(f"refusing to overwrite unmanaged Hermes profile: {root}")
    if manifest is None:
        return
    for relative, expected in manifest["files"].items():
        path = root / relative
        if not path.is_file() or _sha256(path.read_bytes()) != expected:
            raise ValidationError(f"managed Hermes profile was edited outside mdr: {path}")
    installed_skills = {
        path.parent.name for path in (root / "skills").glob("*/SKILL.md")
    }
    managed_skills = {
        Path(relative).parent.name
        for relative in manifest["files"]
        if relative.startswith("skills/")
    }
    unexpected_skills = installed_skills - set(PROFILE_SKILLS[root.name]) - managed_skills
    if unexpected_skills:
        raise ValidationError(
            "managed profile contains unmanaged skills: "
            + ", ".join(sorted(unexpected_skills))
        )


def bootstrap_profiles(
    *,
    apply: bool = False,
    hermes_home: Path | None = None,
    source_profile: Path | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    """Plan or apply the six isolated, CLI-only Hermes profiles."""
    home = _home(hermes_home)
    executable = shutil.which("hermes")
    settings = _source_settings(source_profile, home)
    plan = []
    desired_by_profile: dict[str, dict[str, bytes]] = {}
    names = [profile] if profile else list(PROFILE_SKILLS)
    if any(name not in PROFILE_SKILLS for name in names):
        raise ValidationError("unknown managed Hermes profile")
    for name in names:
        root = _profile_root(home, name)
        desired = _desired_files(name, settings)
        desired_by_profile[name] = desired
        _check_ownership(root, desired)
        plan.append(
            {
                "profile": name,
                "root": str(root),
                "skills": list(PROFILE_SKILLS[name]),
                "files": sorted(desired),
            }
        )
    if not apply:
        return {
            "applied": False,
            "hermes": executable,
            "hermes_version": _version(executable),
            "hermes_home": str(home),
            "profiles": plan,
        }
    if executable is None:
        raise ValidationError("Hermes CLI is not installed or is not on PATH")
    root_config = home / "config.yaml"
    if source_profile is not None and not root_config.exists():
        # Hermes resolves an unpinned cron job's creation snapshot from the
        # home-level config, even when the job belongs to a named profile.
        # Seed a brand-new isolated home with only the filtered settings that
        # are already copied to the managed profiles.  Never replace an
        # operator's existing root config.
        home.mkdir(parents=True, exist_ok=True)
        root_settings = {
            **settings,
            "timezone": settings.get("timezone", _system_timezone()),
        }
        root_config.write_text(
            yaml.safe_dump(root_settings, sort_keys=False), encoding="utf-8"
        )
    for item in plan:
        name = item["profile"]
        root = Path(item["root"])
        created = False
        if not root.exists():
            completed = subprocess.run(
                [
                    executable,
                    "profile",
                    "create",
                    name,
                    "--no-skills",
                    "--description",
                    DESCRIPTIONS[name],
                    "--no-alias",
                ],
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "HERMES_HOME": str(home)},
            )
            if completed.returncode:
                raise ValidationError(
                    f"Hermes could not create {name}: "
                    f"{(completed.stderr or completed.stdout).strip()}"
                )
            created = True
        root.mkdir(parents=True, exist_ok=True)
        desired = desired_by_profile[name]
        if not created:
            _check_ownership(root, desired)
        previous = _read_manifest(root)
        for relative in set((previous or {}).get("files", {})) - set(desired):
            (root / relative).unlink()
        for relative, content in desired.items():
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        manifest = {
            "schema_version": "1",
            "package": "hermes-medical-research",
            "version": __version__,
            "profile": name,
            "files": {relative: _sha256(content) for relative, content in desired.items()},
        }
        (root / MANAGED).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return {
        "applied": True,
        "hermes": executable,
        "hermes_version": _version(executable),
        "hermes_home": str(home),
        "profiles": plan,
        "bot_mode_discovery": "automatic",
        "workflow": "cron-routines",
    }


def doctor(*, hermes_home: Path | None = None) -> dict[str, Any]:
    home = _home(hermes_home)
    executable = shutil.which("hermes")
    profiles = []
    ready = executable is not None
    for name in PROFILE_SKILLS:
        root = _profile_root(home, name)
        desired = _desired_files(name, _source_settings(None, home))
        problems: list[str] = []
        try:
            _check_ownership(root, desired)
        except ValidationError as exc:
            problems.append(str(exc))
        manifest = _read_manifest(root)
        if manifest and manifest.get("version") != __version__:
            problems.append(
                f"managed profile version is {manifest.get('version')}; expected {__version__}"
            )
        if not root.is_dir():
            problems.append("profile is missing")
        for relative in desired:
            if not (root / relative).is_file():
                problems.append(f"missing {relative}")
        if problems:
            ready = False
        profiles.append({"profile": name, "ready": not problems, "problems": problems})
    routine_state = _routine_health(home)
    paused_routines = routine_status(home)["paused"] if routine_state["ready"] else []
    main_config = _source_settings(None, home)
    if not main_config:
        # Isolated qualification homes commonly contain only managed profiles,
        # not a root config.yaml.  The coordinator received the same selected
        # provider settings at bootstrap, so it is the authoritative fallback.
        main_config = _source_settings(
            _profile_root(home, "mdr-coordinator") / "config.yaml", home
        )
    raw_config: dict[str, Any] = {}
    config_path = home / "config.yaml"
    if config_path.is_file():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        raw_config = loaded if isinstance(loaded, dict) else {}
    gateway = raw_config.get("gateway")
    multiplex = bool(
        isinstance(gateway, dict) and gateway.get("multiplex_profiles") is True
    )
    mdr_executable = shutil.which("mdr")
    cron_doctors = []
    if executable:
        for name in PROFILE_SKILLS:
            completed = subprocess.run(
                [executable, "-p", name, "cron", "doctor"],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
                env={**os.environ, "HERMES_HOME": str(home)},
            )
            cron_doctors.append(
                {
                    "profile": name,
                    "ready": completed.returncode == 0,
                    "detail": (completed.stderr or completed.stdout).strip(),
                }
            )
    cron_ready = bool(cron_doctors) and all(item["ready"] for item in cron_doctors)
    return {
        "ready": (
            ready
            and routine_state["ready"]
            and multiplex
            and mdr_executable is not None
            and cron_ready
        ),
        "hermes": executable,
        "hermes_version": _version(executable),
        "hermes_home": str(home),
        "bot_mode_discovery": "automatic",
        "profiles": profiles,
        "routines": routine_state,
        "paused_routines": paused_routines,
        "mdr": mdr_executable,
        "gateway_multiplex_profiles": multiplex,
        "cron_schedulers": cron_doctors,
        "provider_selection": {
            "configured": bool(main_config.get("model") or main_config.get("provider")),
            "timezone": main_config.get("timezone") or _system_timezone(),
        },
    }


def _jobs(profile_root: Path) -> list[dict[str, Any]]:
    path = profile_root / "cron" / "jobs.json"
    if not path.is_file():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"invalid Hermes cron store {path}: {exc}") from exc
    jobs = value.get("jobs", []) if isinstance(value, dict) else value
    if not isinstance(jobs, list) or not all(isinstance(item, dict) for item in jobs):
        raise ValidationError(f"invalid Hermes cron jobs in {path}")
    return jobs


def _job_static(job: dict[str, Any]) -> dict[str, Any]:
    """Project only mdr-owned fields; user inference pins and pause state stay outside."""
    keys = (
        "name",
        "schedule",
        "prompt",
        "script",
        "monitor_script",
        "no_agent",
        "skills",
        "deliver",
        "failure_deliver",
        "workdir",
    )
    return {key: job.get(key) for key in keys if key in job}


def _routine_manifest(home: Path) -> dict[str, Any] | None:
    path = home / ROUTINES_MANAGED
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"invalid routine manifest {path}: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("scripts"), dict):
        raise ValidationError(f"invalid routine manifest {path}")
    return value


def _routine_health(home: Path) -> dict[str, Any]:
    manifest = _routine_manifest(home)
    if manifest is None:
        return {"ready": False, "problems": ["managed cron routines are not installed"]}
    problems = []
    for key, checksum in manifest["scripts"].items():
        profile, relative = key.split("/", 1)
        path = _profile_root(home, profile) / relative
        if not path.is_file() or _sha256(path.read_bytes()) != checksum:
            problems.append(f"managed routine script changed or is missing: {path}")
    for managed in manifest.get("jobs", []):
        matches = [
            job
            for job in _jobs(_profile_root(home, managed["profile"]))
            if job.get("name") == managed["name"]
        ]
        if len(matches) != 1:
            problems.append(
                f"managed cron job {managed['profile']}/{managed['name']} is missing or ambiguous"
            )
    return {"ready": not problems, "problems": problems, "jobs": manifest.get("jobs", [])}


def _routine_specs(store: Path, home: Path) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    from .automation import AutomationEngine

    quoted_store = shlex.quote(str(store))
    quoted_mdr = shlex.quote(shutil.which("mdr") or "mdr")
    quoted_hermes = shlex.quote(shutil.which("hermes") or "hermes")
    scripts: dict[str, bytes] = {}
    jobs: list[dict[str, Any]] = []
    tick_script = "mdr-work-tick.sh"
    scripts[f"mdr-coordinator/scripts/{tick_script}"] = (
        "#!/bin/sh\n"
        f"exec {quoted_mdr} --store {quoted_store} --actor mdr-coordinator work cron-tick\n"
    ).encode()
    jobs.append(
        {
            "profile": "mdr-coordinator",
            "name": "mdr-work-tick",
            "schedule": "* * * * *",
            "prompt": "",
            "script": tick_script,
            "no_agent": True,
            "deliver": "bot-chat",
            "skills": [],
            "paused": False,
        }
    )
    for role in WORKER_ROLES:
        profile = f"mdr-{role}"
        script = f"mdr-work-{role}.sh"
        scripts[f"{profile}/scripts/{script}"] = (
            "#!/bin/sh\n"
            f"exec {quoted_mdr} --store {quoted_store} hermes drain --role {role} "
            f"--hermes-home {shlex.quote(str(home))} "
            f"--hermes-executable {quoted_hermes}\n"
        ).encode()
        jobs.append(
            {
                "profile": profile,
                "name": f"mdr-work-{role}",
                "schedule": "* * * * *",
                "prompt": "",
                "script": script,
                "no_agent": True,
                "deliver": None,
                "failure_deliver": "bot-chat:mdr-coordinator",
                "skills": [],
                "paused": False,
            }
        )
    automation = AutomationEngine(store)
    coordinator_config = _profile_root(home, "mdr-coordinator") / "config.yaml"
    config = (
        yaml.safe_load(coordinator_config.read_text(encoding="utf-8")) or {}
        if coordinator_config.is_file()
        else {}
    )
    coordinator_timezone = config.get("timezone")
    for view in automation.list_reviews()["reviews"]:
        schedule = view["schedule"]
        if schedule["expression"] == "once":
            continue
        if not isinstance(coordinator_timezone, str) or not coordinator_timezone:
            raise ValidationError("Coordinator profile needs a configured IANA timezone")
        if schedule["timezone"] != coordinator_timezone:
            raise ValidationError(
                f"Review {view['name']} timezone {schedule['timezone']} differs from "
                f"Coordinator timezone {coordinator_timezone}"
            )
        script = f"mdr-review-{view['name']}.sh"
        scripts[f"mdr-coordinator/scripts/{script}"] = (
            "#!/bin/sh\n"
            f"exec {quoted_mdr} --store {quoted_store} review run-now "
            f"{shlex.quote(view['name'])} "
            "--scheduled\n"
        ).encode()
        jobs.append(
            {
                "profile": "mdr-coordinator",
                "name": f"mdr-review-{view['name']}",
                "schedule": schedule["expression"],
                "prompt": "",
                "script": script,
                "no_agent": True,
                "deliver": None,
                "skills": [],
                # The script asks the core whether the Review is paused.  Hermes's own
                # pause bit remains an operator-owned override and is never rewritten.
                "paused": False,
            }
        )
    return jobs, scripts


def _create_routine(executable: str, home: Path, spec: dict[str, Any]) -> None:
    command = [
        executable,
        "-p",
        spec["profile"],
        "cron",
        "create",
        spec["schedule"],
        spec["prompt"],
        "--name",
        spec["name"],
    ]
    if spec.get("no_agent"):
        command.extend(["--script", spec["script"], "--no-agent"])
    else:
        command.extend(["--monitor-script", spec["monitor_script"]])
        for skill in spec["skills"]:
            command.extend(["--skill", skill])
    if spec.get("deliver"):
        command.extend(["--deliver", spec["deliver"]])
    if spec.get("failure_deliver"):
        command.extend(["--failure-deliver", spec["failure_deliver"]])
    if spec.get("paused"):
        command.extend(["--paused", "--paused-reason", "Review is paused in mdr"])
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "HERMES_HOME": str(home)},
    )
    if completed.returncode:
        raise ValidationError(
            f"Hermes could not create routine {spec['name']}: "
            f"{(completed.stderr or completed.stdout).strip()}"
        )
    if not spec.get("no_agent"):
        matches = [
            job
            for job in _jobs(_profile_root(home, spec["profile"]))
            if job.get("name") == spec["name"]
        ]
        if len(matches) != 1 or not matches[0].get("id"):
            raise ValidationError(
                f"Hermes created an ambiguous routine: "
                f"{spec['profile']}/{spec['name']}"
            )
        # Profile creation can leave Hermes's cached creation snapshot behind
        # the config that bootstrap just installed.  Public resnap adopts the
        # current provider/model resolution while keeping the job unpinned.
        resnapped = subprocess.run(
            [
                executable,
                "-p",
                spec["profile"],
                "cron",
                "resnap",
                str(matches[0]["id"]),
            ],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "HERMES_HOME": str(home)},
        )
        if resnapped.returncode:
            raise ValidationError(
                f"Hermes could not resnap routine {spec['name']}: "
                f"{(resnapped.stderr or resnapped.stdout).strip()}"
            )


def _edit_routine(
    executable: str, home: Path, spec: dict[str, Any], existing: dict[str, Any]
) -> None:
    """Update an owned job through Hermes while leaving model pins and pause state alone."""
    job_id = existing.get("id")
    if not job_id:
        raise ValidationError(f"managed routine has no Hermes job id: {spec['name']}")
    command = [
        executable,
        "-p",
        spec["profile"],
        "cron",
        "edit",
        str(job_id),
        "--name",
        spec["name"],
        "--schedule",
        spec["schedule"],
        "--prompt",
        spec["prompt"],
    ]
    if spec["skills"]:
        for skill in spec["skills"]:
            command.extend(["--skill", skill])
    else:
        command.append("--clear-skills")
    if spec.get("no_agent"):
        command.extend(
            ["--script", spec["script"], "--no-agent", "--monitor-script", ""]
        )
    else:
        command.extend(["--agent", "--monitor-script", spec["monitor_script"]])
    if spec.get("deliver") is not None:
        command.extend(["--deliver", spec["deliver"]])
    if spec.get("failure_deliver") is not None:
        command.extend(["--failure-deliver", spec["failure_deliver"]])
    if spec.get("workdir") is not None:
        command.extend(["--workdir", spec["workdir"]])
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "HERMES_HOME": str(home)},
    )
    if completed.returncode:
        raise ValidationError(
            f"Hermes could not update routine {spec['name']}: "
            f"{(completed.stderr or completed.stdout).strip()}"
        )


def _invoke_claim(
    executable: str,
    home: Path,
    claim: dict[str, Any],
    actor: Any,
    role: str,
) -> subprocess.CompletedProcess[str]:
    """Run one fresh Hermes session for exactly one already-claimed Task."""
    commands = (
        "source_list",
        "source_show",
        "source_find",
        "source_read",
        "submit",
        "execute",
        "run",
        "approve",
        "fail",
    )
    instruction = {
        "schema_version": "2",
        "role": role,
        "kind": claim.get("kind"),
        "run_id": claim["run_id"],
        "task_id": claim["task_id"],
        "packet_path": claim["packet_path"],
        "proposal_path": claim["proposal_path"],
        **{key: claim[key] for key in commands if key in claim},
    }
    skill = ROLE_SKILLS[role]
    profile = f"mdr-{role}"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=f"mdr-{role}-",
        suffix=".json",
        delete=False,
    ) as handle:
        json.dump(instruction, handle, indent=2)
        handle.write("\n")
        instruction_path = Path(handle.name)
    try:
        instruction_path.chmod(0o600)
        prompt = (
            f"Use the {skill} skill. One {claim.get('kind')} task is already claimed for you. "
            f"Read the instruction file at {instruction_path}. It names the only packet, "
            "proposal, and commands you may use. Do not run a claim command and do not start "
            "another task. Complete this task as the skill describes, then return only the "
            "run_id, task_id, and recorded state."
        )
        return subprocess.run(
            [executable, "-p", profile, "--skills", skill, "-z", prompt],
            check=False,
            capture_output=True,
            text=True,
            timeout=HOST_TIMEOUT_SECONDS,
            env={
                **os.environ,
                "HERMES_HOME": str(home),
                "HERMES_SESSION_ID": actor.session_id,
                "HERMES_SESSION_PROFILE": profile,
            },
        )
    finally:
        instruction_path.unlink(missing_ok=True)


async def drain(
    *,
    role: str,
    store: Path,
    hermes_home: Path | None = None,
    hermes_executable: Path | None = None,
    invoke: Callable[[str, Path, dict[str, Any], Any, str], subprocess.CompletedProcess[str]]
    | None = None,
) -> dict[str, Any]:
    """Run one fresh host session per claimed Task until the role's queue is empty.

    A Task that its host session cannot finish is failed through the durable queue, with
    backoff, and the runner moves on instead of stalling every later Task.  Full-text
    acquisition has no semantic choice, so the runner submits its fixed proposal itself.
    """
    from .automation import AutomationEngine
    from .tasks import Actor, TaskEngine

    if role not in ROLE_SKILLS:
        raise ValidationError(f"unknown worker role: {role}")
    root = store.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    home = _home(hermes_home)
    executable = (
        shutil.which(str(hermes_executable.expanduser()))
        if hermes_executable is not None
        else shutil.which("hermes")
    )
    if executable is None:
        raise ValidationError("Hermes CLI is not installed or is not on PATH")
    call = invoke or _invoke_claim
    lock = FileLock(str(root / f".drain-{role}.lock"))
    try:
        lock.acquire(timeout=0)
    except FileLockTimeout:
        return {"state": "already_running", "role": role, "processed": 0, "failed": []}
    processed = 0
    failed: list[dict[str, Any]] = []
    profile = f"mdr-{role}"
    try:
        automation = AutomationEngine(root)
        while True:
            actor = Actor(profile, f"serial-{role}-{uuid4().hex}", role)
            claim = automation.claim(ROLE_COMMANDS[role], actor)
            if claim.get("state") == "idle":
                return {
                    "state": "drained",
                    "role": role,
                    "processed": processed,
                    "failed": failed,
                }
            engine = TaskEngine(automation.catalog.workspace(claim["run_id"]))
            packet = json.loads(Path(claim["packet_path"]).read_text(encoding="utf-8"))
            outcome: str | None = None
            last_error = f"{role} host session did not finish its assigned task."
            if role == "selector" and len(packet.get("target_ids", [])) != 1:
                last_error = "Selector task must contain exactly one article."
            elif claim.get("kind") == "fulltext":
                try:
                    await engine.submit(
                        "extract",
                        claim["task_id"],
                        Path(claim["proposal_path"]),
                        actor,
                        claim_token=claim["claim_token"],
                    )
                except (ValidationError, OSError, json.JSONDecodeError) as exc:
                    last_error = f"deterministic full-text submission failed: {exc}"[:500]
                state = engine.task_automation(claim["task_id"])["state"]
                outcome = None if state == "in_progress" else state
            else:
                expires = datetime.fromisoformat(claim["expires_at"])
                for _ in range(HOST_ATTEMPTS):
                    remaining = (expires - datetime.now(UTC)).total_seconds()
                    if remaining < LEASE_RESERVE_SECONDS:
                        last_error = "claim lease is nearly expired; retry later"
                        break
                    try:
                        completed = await asyncio.to_thread(
                            call, executable, home, claim, actor, role
                        )
                        detail = (completed.stderr or completed.stdout or "").strip()
                    except subprocess.TimeoutExpired:
                        detail = f"host session exceeded {HOST_TIMEOUT_SECONDS} seconds"
                    state = engine.task_automation(claim["task_id"])["state"]
                    if state != "in_progress":
                        outcome = state
                        break
                    last_error = detail[-500:] or last_error
            if outcome == "accepted":
                processed += 1
                continue
            record: dict[str, Any] = {
                "run_id": claim["run_id"],
                "task_id": claim["task_id"],
                "kind": claim.get("kind"),
            }
            if outcome is None:
                code = f"{role}_host_failed"
                try:
                    result = automation.fail(
                        claim["claim_id"], actor, code=code, message=last_error
                    )
                    record.update(code=code, blocked=result["blocked"])
                except ValidationError as exc:
                    record.update(code=code, fail_not_recorded=str(exc))
            else:
                # The host session itself ran the fail command, or the Task was superseded.
                record["state"] = outcome
            failed.append(record)
    finally:
        lock.release()


async def drain_selector(**kwargs: Any) -> dict[str, Any]:
    """Compatibility entry point for Routine scripts written before 0.5.8."""
    return await drain(role="selector", **kwargs)


def routine_status(hermes_home: Path | None = None) -> dict[str, Any]:
    """Report each managed Routine's Hermes pause state without changing it."""
    home = _home(hermes_home)
    manifest = _routine_manifest(home)
    if manifest is None:
        return {"installed": False, "jobs": [], "paused": []}
    jobs = []
    for managed in manifest.get("jobs", []):
        matches = [
            job
            for job in _jobs(_profile_root(home, managed["profile"]))
            if job.get("name") == managed["name"]
        ]
        job = matches[0] if len(matches) == 1 else {}
        jobs.append(
            {
                "profile": managed["profile"],
                "name": managed["name"],
                "id": job.get("id"),
                "present": len(matches) == 1,
                "enabled": job.get("enabled", True) if job else False,
                "state": job.get("state"),
                "last_status": job.get("last_status"),
                "last_run_at": job.get("last_run_at"),
            }
        )
    return {
        "installed": True,
        "jobs": jobs,
        "paused": [f"{job['profile']}/{job['name']}" for job in jobs if not job["enabled"]],
    }


def set_routines_paused(paused: bool, *, hermes_home: Path | None = None) -> dict[str, Any]:
    """Pause or resume every managed Routine through Hermes's public cron CLI."""
    home = _home(hermes_home)
    executable = shutil.which("hermes")
    if executable is None:
        raise ValidationError("Hermes CLI is not installed or is not on PATH")
    status = routine_status(home)
    if not status["installed"]:
        raise ValidationError("managed cron routines are not installed")
    changed = []
    for job in status["jobs"]:
        if not job["present"] or not job["id"] or job["enabled"] == (not paused):
            continue
        completed = subprocess.run(
            [executable, "-p", job["profile"], "cron", "pause" if paused else "resume", job["id"]],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "HERMES_HOME": str(home)},
        )
        if completed.returncode:
            raise ValidationError(
                f"Hermes could not {'pause' if paused else 'resume'} "
                f"{job['profile']}/{job['name']}: "
                f"{(completed.stderr or completed.stdout).strip()}"
            )
        changed.append(f"{job['profile']}/{job['name']}")
    return {"changed": changed, **routine_status(home)}


def routines(
    *,
    apply: bool = False,
    hermes_home: Path | None = None,
    store: Path | None = None,
) -> dict[str, Any]:
    """Plan or install the managed cron fleet through Hermes's public CLI."""
    home = _home(hermes_home)
    from .tasks import data_home

    artifact_store = (store or data_home()).expanduser().resolve()
    jobs, scripts = _routine_specs(artifact_store, home)
    plan = {
        "applied": False,
        "hermes_home": str(home),
        "store": str(artifact_store),
        "jobs": jobs,
        "scripts": sorted(scripts),
    }
    if not apply:
        return plan
    executable = shutil.which("hermes")
    if executable is None:
        raise ValidationError("Hermes CLI is not installed or is not on PATH")
    previous = _routine_manifest(home)
    if previous:
        for key, checksum in previous["scripts"].items():
            profile, relative = key.split("/", 1)
            path = _profile_root(home, profile) / relative
            observed = _sha256(path.read_bytes()) if path.is_file() else None
            desired = _sha256(scripts[key]) if key in scripts else None
            if observed not in {checksum, desired}:
                raise ValidationError(f"managed routine script was edited outside mdr: {path}")
        for key in set(previous["scripts"]) - set(scripts):
            profile, relative = key.split("/", 1)
            (_profile_root(home, profile) / relative).unlink(missing_ok=True)
    managed_names = {
        (item["profile"], item["name"]) for item in (previous or {}).get("jobs", [])
    }
    previous_jobs = {
        (item["profile"], item["name"]): item
        for item in (previous or {}).get("jobs", [])
    }
    desired_names = {(item["profile"], item["name"]) for item in jobs}
    removed = managed_names - desired_names
    if removed:
        raise ValidationError(
            "managed routine removal requires explicit operator cleanup: "
            + ", ".join(f"{profile}/{name}" for profile, name in sorted(removed))
        )
    for key, content in scripts.items():
        profile, relative = key.split("/", 1)
        destination = _profile_root(home, profile) / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        destination.chmod(0o700)
    progress = {
        key: value for key, value in previous_jobs.items() if key in desired_names
    }
    manifest = {
        "schema_version": "1",
        "package": "hermes-medical-research",
        "version": __version__,
        "store": str(artifact_store),
        "scripts": {key: _sha256(content) for key, content in scripts.items()},
        "jobs": list(progress.values()),
    }
    (home / ROUTINES_MANAGED).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    for spec in jobs:
        key = (spec["profile"], spec["name"])
        desired_digest = _sha256(
            json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()
        )
        existing = [
            job
            for job in _jobs(_profile_root(home, spec["profile"]))
            if job.get("name") == spec["name"]
        ]
        owned = key in managed_names
        if len(existing) > 1 or existing and not owned:
            raise ValidationError(
                f"refusing to replace unmanaged or ambiguous cron job: "
                f"{spec['profile']}/{spec['name']}"
            )
        if existing and owned:
            observed = previous_jobs[key].get("observed")
            if observed is not None and _job_static(existing[0]) != observed:
                raise ValidationError(
                    f"managed cron job was edited outside mdr: "
                    f"{spec['profile']}/{spec['name']}"
                )
            if previous_jobs[key].get("spec_digest") != desired_digest:
                _edit_routine(executable, home, spec, existing[0])
                existing = [
                    job
                    for job in _jobs(_profile_root(home, spec["profile"]))
                    if job.get("name") == spec["name"]
                ]
                if len(existing) != 1:
                    raise ValidationError(
                        f"Hermes updated an ambiguous routine: "
                        f"{spec['profile']}/{spec['name']}"
                    )
        if not existing:
            _create_routine(executable, home, spec)
            existing = [
                job
                for job in _jobs(_profile_root(home, spec["profile"]))
                if job.get("name") == spec["name"]
            ]
            if len(existing) != 1:
                raise ValidationError(
                    f"Hermes reported success but routine was not recorded: "
                    f"{spec['profile']}/{spec['name']}"
                )
        progress[key] = {
            "profile": spec["profile"],
            "name": spec["name"],
            "spec_digest": desired_digest,
            "observed": _job_static(existing[0]),
        }
        manifest["jobs"] = [progress[item] for item in sorted(progress)]
        (home / ROUTINES_MANAGED).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return {**plan, "applied": True, "health": _routine_health(home)}
