"""Supported Hermes profile bootstrap without plugin or private-agent APIs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from hermes_medical_research import __version__
from hermes_medical_research.search.models import ValidationError


@dataclass(frozen=True)
class ProfileSpec:
    """One managed Hermes profile.

    ``kind`` names the Task kind whose submit tool this profile carries.  Such a profile is
    tool-free apart from that one MCP tool and pins ``tool_choice: required``, which is what makes
    the model server compile a grammar for the tool's ``result`` object from the first token.
    ``skills`` and shell toolsets belong to the session profiles instead, which must read arbitrary
    full text.
    """

    description: str
    skills: tuple[str, ...] = ()
    toolsets: tuple[str, ...] = ()
    max_turns: int | None = None
    kind: str | None = None
    role: str | None = None


SESSION_TOOLSETS = ("terminal", "file", "skills")
# Per-session model-turn ceilings for one claimed Task.  An assessment reads each protocol
# outcome's locations; an audit group checks several targets.  A constrained call needs two: the
# tool call and the turn that sees its result.
PROFILES: dict[str, ProfileSpec] = {
    "hmr-coordinator": ProfileSpec(
        "Human-facing intake; never performs specialist work.",
        toolsets=SESSION_TOOLSETS,
        role="coordinator",
    ),
    "hmr-selector": ProfileSpec(
        "Screens and selects one supplied medical record at a time.",
        skills=("medical-select",),
        toolsets=SESSION_TOOLSETS,
        max_turns=16,
        role="selector",
    ),
    "hmr-extractor": ProfileSpec(
        "Acquires full text, links studies, extracts results, and appraises them.",
        skills=("medical-extract",),
        toolsets=SESSION_TOOLSETS,
        max_turns=60,
        role="extractor",
    ),
    "hmr-synthesizer": ProfileSpec(
        "Synthesizes one protocol outcome at a time.",
        skills=("medical-synthesize",),
        toolsets=SESSION_TOOLSETS,
        max_turns=40,
        role="synthesizer",
    ),
    "hmr-auditor": ProfileSpec(
        "Independently audits frozen evidence and report assertions.",
        skills=("medical-synthesize",),
        toolsets=SESSION_TOOLSETS,
        max_turns=48,
        role="auditor",
    ),
    "hmr-screen": ProfileSpec(
        "Answers one screening packet with one constrained tool call.",
        max_turns=2,
        kind="screening",
        role="selector",
    ),
    "hmr-cover": ProfileSpec(
        "Answers one assessment-coverage packet with one constrained tool call.",
        max_turns=2,
        kind="coverage",
        role="selector",
    ),
    "hmr-link": ProfileSpec(
        "Answers one study-linking packet with one constrained tool call.",
        max_turns=2,
        kind="studies",
        role="extractor",
    ),
    "hmr-finding": ProfileSpec(
        "Writes one protocol outcome's finding with one constrained tool call.",
        max_turns=2,
        kind="synthesis",
        role="synthesizer",
    ),
}
CALL_PROFILES = {spec.kind: name for name, spec in PROFILES.items() if spec.kind}
SESSION_PROFILES = {spec.role: name for name, spec in PROFILES.items()
                    if spec.kind is None and spec.role}
# 0.5.x installed a Searcher profile for a role that now runs no session at all.
RETIRED_PROFILES = ("hmr-searcher", "mdr-searcher")
MANAGED = "hmr-managed.json"
ROUTINES_MANAGED = "hmr-routines.json"
ASSIGNMENT_FILE = "hmr-steps.json"
MCP_SERVER_NAME = "hmr-tasks"
WORKER_ROLES = ("searcher", "selector", "extractor", "synthesizer", "auditor")
ROLE_COMMANDS = {
    "searcher": "search",
    "selector": "select",
    "extractor": "extract",
    "synthesizer": "synthesize",
    "auditor": "audit",
}
# One session may take many slow model turns; the runner renews its claim while it works.
HOST_TIMEOUT_SECONDS = 75 * 60
# A constrained call is one short turn; 7 s measured, with headroom for a cold prefix.
CALL_TIMEOUT_SECONDS = 5 * 60
LEASE_RENEW_SECONDS = 10 * 60
# Transient claim failures (for example a busy Run lock) are retried before the runner gives up.
CLAIM_RETRIES = 5
CLAIM_RETRY_SECONDS = 30


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


def _provider_name(settings: dict[str, Any]) -> str | None:
    model = settings.get("model")
    if isinstance(model, dict) and isinstance(model.get("provider"), str):
        return model["provider"]
    provider = settings.get("provider")
    return provider if isinstance(provider, str) else None


def _with_forced_tool_call(settings: dict[str, Any], kind: str) -> dict[str, Any]:
    """Pin ``tool_choice: required`` on the provider entry this profile actually selects.

    A tool grammar is lazy under ``auto``: it binds nothing until the model opens a tool call, and
    a model that answers in prose never opens one.  ``required`` makes the call mandatory and the
    grammar active from the first token.  Hermes merges a provider's ``extra_body`` into every
    chat-completions request, and the entry named by ``model.provider`` is the one it resolves, so
    both spellings of that entry carry it.
    """
    from . import answers

    extra = {"tool_choice": "required"}
    updated = deepcopy(settings)
    name = _provider_name(settings)
    providers = updated.get("providers")
    if name and isinstance(providers, dict) and isinstance(providers.get(name), dict):
        providers[name] = {**providers[name], "extra_body": extra}
    custom = updated.get("custom_providers")
    if isinstance(custom, list):
        updated["custom_providers"] = [
            {**entry, "extra_body": extra} if isinstance(entry, dict) else entry
            for entry in custom
        ]
    model = updated.get("model")
    if isinstance(model, dict):
        # A constrained tool call is short; a small ceiling bounds a pathological string argument.
        updated["model"] = {**model, "max_tokens": answers.MAX_TOKENS[kind]}
    return updated


def _mcp_entry(store: Path, role: str, kind: str | None, executable: str) -> dict[str, Any]:
    args = ["mcp", "serve", "--store", str(store), "--role", role]
    if kind:
        args += ["--kind", kind]
    return {
        MCP_SERVER_NAME: {
            "command": executable,
            "args": args,
            "enabled": True,
            "timeout": 300,
        }
    }


def _desired_files(
    name: str,
    settings: dict[str, Any],
    *,
    store: Path | None = None,
    assignment: dict[str, Any] | None = None,
) -> dict[str, bytes]:
    spec = PROFILES[name]
    # Order matters: the forced tool call is pinned on the provider entry the profile ends up
    # selecting, so the operator's model assignment has to be applied first.
    settings = _with_assigned_model(settings, spec, assignment)
    if spec.kind:
        settings = _with_forced_tool_call(settings, spec.kind)
    config: dict[str, Any] = {
        **settings,
        "timezone": settings.get("timezone", _system_timezone()),
        **({"agent": {"max_turns": spec.max_turns}} if spec.max_turns else {}),
        "tools": {"enabled_toolsets": list(spec.toolsets)},
    }
    if spec.kind:
        # The submit tool is the profile's whole tool surface, and the server resolves its own
        # claim, so nothing task-specific is ever written into a config file.
        config["mcp_servers"] = _mcp_entry(
            store if store is not None else _default_store(),
            spec.role or "",
            spec.kind,
            _hmr_path(),
        )
    files = {
        "SOUL.md": _resource(f"profiles/{name}.md"),
        "config.yaml": yaml.safe_dump(config, sort_keys=False).encode(),
    }
    for skill in spec.skills:
        files[f"skills/{skill}/SKILL.md"] = _resource(f"skills/{skill}/SKILL.md")
    return files


def _with_assigned_model(
    settings: dict[str, Any], spec: ProfileSpec, assignment: dict[str, Any] | None
) -> dict[str, Any]:
    """Apply the operator's per-step model choice, if they made one for this profile's kind."""
    chosen = ((assignment or {}).get("kinds") or {}).get(spec.kind or "", {})
    model_name, provider = chosen.get("model"), chosen.get("provider")
    if not model_name:
        return settings
    updated = deepcopy(settings)
    model = updated.get("model")
    if isinstance(model, dict):
        model["default"] = model_name
        if provider:
            model["provider"] = provider
    else:
        updated["model"] = {"default": model_name, **({"provider": provider} if provider else {})}
    return updated


def _default_store() -> Path:
    from .tasks import data_home

    return data_home()


def _hmr_path() -> str:
    return shutil.which("hmr") or "hmr"


def profile_home(value: Path | None = None) -> Path:
    """The Hermes home a step runner and the profile writer share."""
    return _home(value)


def hermes_executable() -> str:
    return shutil.which("hermes") or "hermes"


def assignment_path(home: Path | None = None) -> Path:
    return _home(home) / ASSIGNMENT_FILE


def step_assignment(home: Path | None = None) -> dict[str, Any]:
    """The operator's per-kind lane, profile and model choices; ``{}`` when never set."""
    try:
        data = json.loads(assignment_path(home).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_assignment(
    home: Path | None = None,
    *,
    models: dict[str, str] | None = None,
    lanes: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Record a per-kind model (``MODEL`` or ``MODEL@PROVIDER``) or lane choice."""
    from . import answers

    path = assignment_path(home)
    current = step_assignment(home)
    kinds = dict(current.get("kinds") or {})
    for kind, value in (models or {}).items():
        if kind not in answers.CALL_KINDS:
            raise ValidationError(
                f"{kind} has no constrained answer shape; kinds are "
                f"{', '.join(answers.CALL_KINDS)}"
            )
        model_name, _, provider = value.partition("@")
        entry = dict(kinds.get(kind) or {})
        entry["model"] = model_name.strip()
        if provider.strip():
            entry["provider"] = provider.strip()
        entry.setdefault("profile", CALL_PROFILES[kind])
        kinds[kind] = entry
    for kind, lane in (lanes or {}).items():
        if kind not in answers.CALL_KINDS:
            raise ValidationError(f"{kind} is always answered by a session; it has no lane choice")
        if lane not in {"call", "agent"}:
            raise ValidationError("lane must be call or agent")
        entry = dict(kinds.get(kind) or {})
        entry["lane"] = lane
        entry.setdefault("profile", CALL_PROFILES[kind])
        kinds[kind] = entry
    payload = {
        "schema_version": "1",
        "package": "hermes-medical-research",
        "version": __version__,
        "kinds": kinds,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload


def profile_for_kind(kind: str, assignment: dict[str, Any] | None = None) -> str:
    chosen = ((assignment or {}).get("kinds") or {}).get(kind, {}).get("profile")
    return str(chosen) if chosen else CALL_PROFILES[kind]


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
            raise ValidationError(f"managed Hermes profile was edited outside hmr: {path}")
    installed_skills = {
        path.parent.name for path in (root / "skills").glob("*/SKILL.md")
    }
    managed_skills = {
        Path(relative).parent.name
        for relative in manifest["files"]
        if relative.startswith("skills/")
    }
    unexpected_skills = installed_skills - set(PROFILES[root.name].skills) - managed_skills
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
    store: Path | None = None,
    remove: bool = False,
) -> dict[str, Any]:
    """Plan, apply, or remove the managed Hermes profiles.

    Five carry a role's skill and shell tools for work that reads full text; four carry one
    constrained submit tool and nothing else.  Model settings are still the operator's: only a
    per-step assignment recorded through ``write_assignment`` overrides them.
    """
    home = _home(hermes_home)
    executable = shutil.which("hermes")
    settings = _source_settings(source_profile, home)
    artifact_store = (store or _default_store()).expanduser().resolve()
    assignment = step_assignment(home)
    plan = []
    desired_by_profile: dict[str, dict[str, bytes]] = {}
    names = [profile] if profile else list(PROFILES)
    if any(name not in PROFILES for name in names):
        raise ValidationError("unknown managed Hermes profile")
    for name in names:
        root = _profile_root(home, name)
        desired = _desired_files(name, settings, store=artifact_store,
                                 assignment=assignment)
        desired_by_profile[name] = desired
        _check_ownership(root, desired)
        plan.append(
            {
                "profile": name,
                "root": str(root),
                "skills": list(PROFILES[name].skills),
                "kind": PROFILES[name].kind,
                "files": sorted(desired),
            }
        )
    retired = [
        {"profile": name, "root": str(_profile_root(home, name))}
        for name in RETIRED_PROFILES
        if _profile_root(home, name).exists()
    ]
    if not apply:
        return {
            "applied": False,
            "hermes": executable,
            "hermes_version": _version(executable),
            "hermes_home": str(home),
            "store": str(artifact_store),
            "profiles": plan,
            "retired_profiles": retired,
            "removing": remove,
        }
    if remove:
        return {
            "applied": True,
            "hermes_home": str(home),
            "removed": [_remove_profile(_profile_root(home, item["profile"])) for item in
                        [*plan, *retired]],
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
                    PROFILES[name].description,
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
        "store": str(artifact_store),
        "bot_mode_discovery": "automatic",
        "workflow": "user-invoked-steps",
        "retired_profiles": [_remove_profile(Path(item["root"])) for item in retired],
    }


def _remove_profile(root: Path) -> dict[str, Any]:
    """Delete exactly the files this package installed, and nothing an operator added."""
    manifest = _read_manifest(root)
    if manifest is None:
        return {"profile": root.name, "removed": False,
                "reason": "profile is not managed by hmr"}
    for relative in manifest.get("files", {}):
        (root / relative).unlink(missing_ok=True)
    (root / MANAGED).unlink(missing_ok=True)
    for directory in sorted((path for path in root.rglob("*") if path.is_dir()), reverse=True):
        if not any(directory.iterdir()):
            directory.rmdir()
    leftover = sorted(path.name for path in root.iterdir()) if root.exists() else []
    if not leftover and root.exists():
        root.rmdir()
    return {"profile": root.name, "removed": not leftover, "kept": leftover}


def doctor(*, hermes_home: Path | None = None, store: Path | None = None) -> dict[str, Any]:
    home = _home(hermes_home)
    executable = shutil.which("hermes")
    assignment = step_assignment(home)
    profiles = []
    ready = executable is not None
    for name in PROFILES:
        root = _profile_root(home, name)
        desired = _desired_files(
            name, _source_settings(None, home),
            store=(store or _default_store()).expanduser().resolve(),
            assignment=assignment,
        )
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
        entry: dict[str, Any] = {"profile": name, "ready": not problems, "problems": problems}
        if PROFILES[name].kind:
            entry["schema"] = _schema_report(root, PROFILES[name].kind, problems)
        if problems:
            ready = False
        entry["ready"] = not problems
        profiles.append(entry)
    legacy = _legacy_routines(home)
    for name in RETIRED_PROFILES:
        if _profile_root(home, name).exists():
            ready = False
    main_config = _source_settings(None, home)
    if not main_config:
        # Isolated qualification homes commonly contain only managed profiles,
        # not a root config.yaml.  The coordinator received the same selected
        # provider settings at bootstrap, so it is the authoritative fallback.
        main_config = _source_settings(
            _profile_root(home, "hmr-coordinator") / "config.yaml", home
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
    hmr_executable = shutil.which("hmr")
    from .quick_commands import status as quick_command_status

    commands = quick_command_status(hermes_home=home)
    assignment_report = _assignment_report(assignment)
    return {
        "ready": (
            ready
            and hmr_executable is not None
            and commands["installed"]
            and not legacy["installed"]
            and not assignment_report["problems"]
        ),
        "hermes": executable,
        "hermes_version": _version(executable),
        "hermes_home": str(home),
        "bot_mode_discovery": "automatic",
        "workflow": "user-invoked-steps",
        "profiles": profiles,
        "retired_profiles": [
            name for name in RETIRED_PROFILES if _profile_root(home, name).exists()
        ],
        "legacy_routines": legacy,
        "quick_commands": commands,
        "assignments": assignment_report,
        "hmr": hmr_executable,
        # A per-profile gateway pin mattered when each profile ran its own cron job; a step starts
        # its own session, so this is reported and no longer gates readiness.
        "gateway_multiplex_profiles": multiplex,
        "provider_selection": {
            "configured": bool(main_config.get("model") or main_config.get("provider")),
            "timezone": main_config.get("timezone") or _system_timezone(),
        },
    }


def _schema_report(root: Path, kind: str, problems: list[str]) -> dict[str, Any]:
    """Compare the installed profile's forced-tool settings against this release's schema."""
    from . import answers

    config_path = root / "config.yaml"
    installed: dict[str, Any] = {}
    if config_path.is_file():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        installed = loaded if isinstance(loaded, dict) else {}
    provider = _provider_name(installed)
    entry = ((installed.get("providers") or {}).get(provider or "") or {})
    forced = (entry.get("extra_body") or {}).get("tool_choice")
    servers = installed.get("mcp_servers") or {}
    if forced != "required":
        problems.append("provider entry does not pin tool_choice: required")
    if MCP_SERVER_NAME not in servers:
        problems.append(f"profile does not host the {MCP_SERVER_NAME} tool server")
    return {
        "kind": kind,
        "tool": f"submit_{kind}",
        "schema_digest": answers.schema_digest(kind),
        "tool_choice": forced,
        "mcp_server": MCP_SERVER_NAME in servers,
        "provider_entry": f"providers.{provider}" if provider else None,
    }


def _assignment_report(assignment: dict[str, Any]) -> dict[str, Any]:
    """Check the operator's per-step choices against the kinds and profiles that exist."""
    from . import answers

    problems: list[str] = []
    kinds = assignment.get("kinds") or {}
    for kind, entry in kinds.items():
        if kind not in answers.CALL_KINDS:
            problems.append(f"{kind} has no constrained answer shape")
            continue
        lane = entry.get("lane")
        if lane not in (None, "call", "agent"):
            problems.append(f"{kind} lane must be call or agent")
        profile = entry.get("profile")
        if profile and profile not in PROFILES:
            problems.append(f"{kind} names unknown profile {profile}")
        elif profile and PROFILES[profile].kind != kind and lane != "agent":
            problems.append(f"{profile} does not carry the {kind} submit tool")
    return {"path": ASSIGNMENT_FILE, "kinds": kinds, "problems": problems}


def _legacy_routines(home: Path) -> dict[str, Any]:
    """Report a 0.5.x cron fleet that is still installed; steps and cron must not both claim."""
    manifest = _routine_manifest(home)
    jobs = [
        f"{item['profile']}/{item['name']}"
        for item in (manifest or {}).get("jobs", [])
        if any(job.get("name") == item["name"]
               for job in _jobs(_profile_root(home, item["profile"])))
    ]
    return {
        "installed": bool(jobs),
        "jobs": sorted(jobs),
        "problems": (
            ["legacy cron routines are still installed; run "
             "`hmr hermes routines --remove --apply`"] if jobs else []
        ),
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
    """Project only hmr-owned fields; user inference pins and pause state stay outside."""
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


def invoke_task_session(
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
    skill = PROFILES[SESSION_PROFILES[role]].skills[0]
    profile = f"hmr-{role}"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=f"hmr-{role}-",
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


def invoke_call_session(
    executable: str,
    home: Path,
    profile: str,
    prompt: str,
    kind: str,
    *,
    usage_file: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one fresh tool-free Hermes session that must answer with one constrained tool call.

    Reasoning is off on purpose: llama.cpp ignores a response schema while thinking is enabled, and
    a thinking model also spends most of its tokens before the tool call.  The session carries no
    skill and no shell toolset, so the profile's one MCP submit tool is its whole tool surface, and
    the answer reaches the store through that tool rather than through this process.
    """
    argv = [
        executable,
        "-p", profile,
        "--ignore-rules",
        "--reasoning", "none",
        *(["--usage-file", str(usage_file)] if usage_file else []),
        "-z", prompt,
    ]
    env = {key: value for key, value in os.environ.items()
           if key not in {"HERMES_SESSION_ID", "HERMES_SESSION_PROFILE"}}
    env["HERMES_HOME"] = str(home)
    return subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=CALL_TIMEOUT_SECONDS,
        env=env,
    )


async def run_with_renewal(
    automation: Any,
    claim: dict[str, Any],
    actor: Any,
    *,
    runner: Callable[[dict[str, Any], Any], subprocess.CompletedProcess[str]],
) -> subprocess.CompletedProcess[str]:
    """Run one host session in a worker thread, renewing its claim until the session exits."""
    session = asyncio.ensure_future(asyncio.to_thread(runner, claim, actor))
    while True:
        done, _ = await asyncio.wait({session}, timeout=LEASE_RENEW_SECONDS)
        if done:
            return session.result()
        automation.renew(claim["claim_id"], actor)


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
    remove: bool = False,
    hermes_home: Path | None = None,
    store: Path | None = None,
) -> dict[str, Any]:
    """Report and remove the 0.5.x managed cron fleet.

    Cron holds no workflow authority any more: steps are user-invoked, so a Routine that claims work
    on a timer would race the operator's own step runner. This entry point exists to take the fleet
    off a host that already has it, and to keep reporting whatever is left until it is gone.
    """
    del store  # the fleet is identified by its manifest, not by a store path
    home = _home(hermes_home)
    manifest = _routine_manifest(home)
    installed = [
        {
            "profile": item["profile"],
            "name": item["name"],
            "id": item.get("id"),
            "present": any(
                job.get("name") == item["name"]
                for job in _jobs(_profile_root(home, item["profile"]))
            ),
        }
        for item in (manifest or {}).get("jobs", [])
    ]
    plan = {
        "applied": False,
        "hermes_home": str(home),
        "workflow": "user-invoked-steps",
        "legacy_jobs": installed,
        "scripts": sorted((manifest or {}).get("scripts", {})),
        "next": (
            "run `hmr hermes routines --remove --apply` to delete them"
            if installed or (manifest or {}).get("scripts")
            else "nothing to remove"
        ),
    }
    if not apply:
        return plan
    if not remove:
        raise ValidationError(
            "cron routines are retired; pass --remove --apply to delete the managed fleet"
        )
    executable = shutil.which("hermes")
    if executable is None:
        raise ValidationError("Hermes CLI is not installed or is not on PATH")
    if manifest is None:
        return {**plan, "applied": True, "removed": [], "remaining": []}
    # Pause first, so a partial removal can never leave a job that still claims work.
    set_routines_paused(True, hermes_home=home)
    removed: list[str] = []
    remaining: list[str] = []
    for item in manifest.get("jobs", []):
        profile, name = item["profile"], item["name"]
        matches = [job for job in _jobs(_profile_root(home, profile)) if job.get("name") == name]
        if not matches:
            removed.append(f"{profile}/{name}")
            continue
        if len(matches) > 1:
            remaining.append(f"{profile}/{name}")
            continue
        recorded = item.get("observed")
        if recorded and _job_static(matches[0]) != recorded:
            remaining.append(f"{profile}/{name}")
            continue
        result = subprocess.run(
            [executable, "-p", profile, "cron", "delete", str(matches[0].get("id"))],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "HERMES_HOME": str(home)},
        )
        (removed if result.returncode == 0 else remaining).append(f"{profile}/{name}")
    for key, checksum in manifest.get("scripts", {}).items():
        profile, relative = key.split("/", 1)
        path = _profile_root(home, profile) / relative
        if path.is_file() and _sha256(path.read_bytes()) == checksum:
            path.unlink()
    if not remaining:
        (home / ROUTINES_MANAGED).unlink(missing_ok=True)
    return {**plan, "applied": True, "removed": sorted(removed), "remaining": sorted(remaining)}
