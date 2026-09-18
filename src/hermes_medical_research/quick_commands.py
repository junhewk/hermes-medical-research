"""Install the step slash commands as Hermes quick commands.

A ``type: exec`` quick command runs a shell command with no model in the path, on the Hermes CLI,
the TUI and messaging platforms alike.  Two of its properties shape every command string here: it
is killed after 30 seconds, so a step only ever starts a detached worker and returns; and it
receives no arguments, so each string is self-contained and the Review comes from the store.

The host config belongs to the operator, so the edit is a marked-region splice rather than a YAML
round trip, which would reorder keys and drop comments.  Ownership is a digest manifest, the file is
backed up once before the first change, and the result is re-parsed and compared against the
original before it is kept: anything outside the managed members must be byte-identical in meaning,
or the original bytes go back.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from hermes_medical_research import __version__

from .steps import quick_command_specs
from .validation import ValidationError

MANIFEST = "hmr-quick-commands.json"
REGION_START = "# >>> hmr managed quick commands (hmr hermes commands) >>>"
REGION_END = "# <<< hmr managed quick commands <<<"
CONFIG = "config.yaml"


def _home(hermes_home: Path | None) -> Path:
    from .hermes import profile_home

    return profile_home(hermes_home)


def _digest(entry: dict[str, str]) -> str:
    from hashlib import sha256

    return sha256(
        json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _manifest(home: Path) -> dict[str, Any]:
    try:
        data = json.loads((home / MANIFEST).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _load(home: Path) -> tuple[Path, str, dict[str, Any]]:
    path = home / CONFIG
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    parsed = yaml.safe_load(text) if text.strip() else {}
    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        raise ValidationError(f"{path} is not a mapping; refusing to edit it")
    return path, text, parsed


def _render(specs: dict[str, dict[str, str]], indent: str = "  ") -> str:
    lines = [REGION_START]
    for name, entry in specs.items():
        lines.append(f"{indent}{name}:")
        for key, value in entry.items():
            lines.append(f"{indent}{indent}{key}: {json.dumps(value)}")
    lines.append(REGION_END)
    return "\n".join(lines)


def status(*, hermes_home: Path | None = None, store: Path | None = None,
           review: str | None = None) -> dict[str, Any]:
    """Which managed commands are installed, owned by this package, and unchanged."""
    home = _home(hermes_home)
    path, text, parsed = _load(home)
    recorded = _manifest(home).get("commands") or {}
    installed = parsed.get("quick_commands") or {}
    desired = quick_command_specs(store, review=review) if store is not None else {}
    commands = []
    for name in sorted(set(desired) | set(recorded)):
        entry = installed.get(name)
        commands.append({
            "name": name,
            "present": isinstance(entry, dict),
            "managed": name in recorded,
            "digest_ok": isinstance(entry, dict) and _digest(entry) == recorded.get(name),
            "command": (entry or {}).get("command") if isinstance(entry, dict) else None,
        })
    present = [item for item in commands if item["present"]]
    return {
        "path": str(path),
        "installed": bool(recorded) and len(present) == len(recorded),
        "region": REGION_START in text,
        "commands": commands,
        "problems": [
            f"quick command was edited outside hmr: {item['name']}"
            for item in commands
            if item["managed"] and item["present"] and not item["digest_ok"]
        ],
    }


def install(
    *,
    store: Path,
    apply: bool = False,
    remove: bool = False,
    hermes_home: Path | None = None,
    review: str | None = None,
) -> dict[str, Any]:
    """Plan, write, or remove the managed quick-command region."""
    home = _home(hermes_home)
    path, text, parsed = _load(home)
    specs = quick_command_specs(store, review=review)
    recorded = _manifest(home).get("commands") or {}
    installed = parsed.get("quick_commands") or {}
    if not isinstance(installed, dict):
        raise ValidationError("quick_commands in the Hermes config is not a mapping")
    conflicts = [
        f"refusing to replace an unmanaged quick command: {name}"
        for name in (specs if not remove else recorded)
        if name in installed and name not in recorded
    ]
    edited = [
        f"quick command was edited outside hmr: {name}"
        for name, checksum in recorded.items()
        if isinstance(installed.get(name), dict) and _digest(installed[name]) != checksum
    ]
    region = _render(specs)
    plan = {
        "applied": False,
        "path": str(path),
        "store": str(Path(store).resolve()),
        "review": review,
        "commands": sorted(specs),
        "region": region,
        "problems": conflicts + edited,
        "removing": remove,
    }
    if not apply:
        return plan
    if plan["problems"]:
        raise ValidationError("; ".join(plan["problems"]))
    original = text
    if original:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        (home / f"{CONFIG}.hmr-backup-{stamp}").write_text(original, encoding="utf-8")
    updated = _splice(original, region if not remove else None)
    home.mkdir(parents=True, exist_ok=True)
    path.write_text(updated, encoding="utf-8")
    try:
        # This package owns what it is about to write and everything it recorded earlier. Passing
        # only the new specs made its own retired commands look foreign, so shrinking the surface
        # was refused as tampering.
        _verify(original, updated, {} if remove else specs,
                managed=set(specs) | set(recorded))
    except ValidationError:
        path.write_text(original, encoding="utf-8")
        raise
    if remove:
        (home / MANIFEST).unlink(missing_ok=True)
    else:
        (home / MANIFEST).write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "package": "hermes-medical-research",
                    "version": __version__,
                    "store": str(Path(store).resolve()),
                    "review": review,
                    "commands": {name: _digest(entry) for name, entry in specs.items()},
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    return {**plan, "applied": True, "region": region if not remove else None}


def _splice(text: str, region: str | None) -> str:
    """Replace, insert, or drop the managed region, leaving every other byte alone."""
    if REGION_START in text and REGION_END in text:
        head, rest = text.split(REGION_START, 1)
        _, tail = rest.split(REGION_END, 1)
        if region is None:
            merged = head.rstrip("\n") + ("\n" + tail.lstrip("\n") if tail.strip() else "\n")
            return _drop_empty_key(merged)
        return f"{head}{region}{tail}"
    if region is None:
        return text
    block = f"quick_commands:\n{region}\n"
    if "quick_commands:" in text:
        lines = text.splitlines(keepends=True)
        for index, line in enumerate(lines):
            if line.startswith("quick_commands:"):
                lines.insert(index + 1, region + "\n")
                return "".join(lines)
    separator = "" if not text or text.endswith("\n") else "\n"
    return f"{text}{separator}{block}"


def _drop_empty_key(text: str) -> str:
    """Remove a ``quick_commands:`` line this package added and then emptied."""
    parsed = yaml.safe_load(text) if text.strip() else {}
    if isinstance(parsed, dict) and parsed.get("quick_commands") in (None, {}):
        kept = [line for line in text.splitlines(keepends=True)
                if not line.startswith("quick_commands:")]
        return "".join(kept)
    return text


def _verify(
    original: str, updated: str, specs: dict[str, dict[str, str]], *, managed: set[str]
) -> None:
    """Everything outside the managed members must survive the edit unchanged.

    ``managed`` is the full set of names this package owns, which on a removal is wider than
    ``specs``: those entries are meant to disappear, so they are excluded from the comparison and
    ``specs`` then says what must be present afterwards.
    """
    before = yaml.safe_load(original) if original.strip() else {}
    after = yaml.safe_load(updated) if updated.strip() else {}
    before = before if isinstance(before, dict) else {}
    if not isinstance(after, dict):
        raise ValidationError("the edited Hermes config no longer parses as a mapping")
    before_rest = {key: value for key, value in before.items() if key != "quick_commands"}
    after_rest = {key: value for key, value in after.items() if key != "quick_commands"}
    if before_rest != after_rest:
        raise ValidationError("the edit changed configuration outside quick_commands")
    before_commands = before.get("quick_commands") or {}
    after_commands = after.get("quick_commands") or {}
    kept_before = {k: v for k, v in before_commands.items() if k not in managed}
    kept_after = {k: v for k, v in after_commands.items() if k not in managed}
    if kept_before != kept_after:
        raise ValidationError("the edit changed a quick command hmr does not manage")
    for name, entry in specs.items():
        if after_commands.get(name) != entry:
            raise ValidationError(f"quick command {name} was not written as intended")
