"""Hermes bootstrap is explicit, isolated, and ownership-safe."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
import yaml

from hermes_medical_research import quick_commands
from hermes_medical_research.answers import MAX_TOKENS, schema_digest
from hermes_medical_research.hermes import (
    MCP_SERVER_NAME,
    PROFILES,
    RETIRED_PROFILES,
    _hmr_path,
    _system_timezone,
    bootstrap_profiles,
    doctor,
    write_assignment,
)
from hermes_medical_research.search.models import ValidationError

SELECTED_SOURCE = (
    "model:\n"
    "  default: base-model\n"
    "  provider: selected\n"
    "providers:\n"
    "  selected: {base_url: 'http://localhost:8091'}\n"
    "custom_providers:\n"
    "  - {name: local, base_url: 'http://localhost:8091/v1', model: base-model}\n"
)


def fake_hermes(tmp_path: Path, monkeypatch) -> Path:
    executable = tmp_path / "bin" / "hermes"
    executable.parent.mkdir()
    executable.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'Hermes Agent vtest'; exit 0; fi\n"
        "name=$3\n"
        "mkdir -p \"$HERMES_HOME/profiles/$name\"\n"
        "exit 0\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(executable.parent) + os.pathsep + os.environ["PATH"])
    return executable


def test_bootstrap_is_a_nonmutating_dry_run(tmp_path):
    home = tmp_path / "hermes"
    result = bootstrap_profiles(hermes_home=home, store=tmp_path / "store")
    assert not result["applied"]
    assert {item["profile"] for item in result["profiles"]} == set(PROFILES)
    assert result["store"] == str(tmp_path / "store")
    assert result["retired_profiles"] == []
    assert not result["removing"]
    assert not home.exists()


def test_bootstrap_copies_only_the_selected_provider_definition(tmp_path):
    source = tmp_path / "source.yaml"
    source.write_text(
        "model:\n"
        "  default: test-model\n"
        "  provider: selected\n"
        "providers:\n"
        "  selected: {base_url: 'http://localhost:8091'}\n"
        "  unrelated: {base_url: 'https://example.invalid'}\n"
        "custom_providers:\n"
        "  - {name: local, base_url: 'http://localhost:8091/v1', model: test-model}\n"
        "  - {name: other, base_url: 'https://example.invalid/v1', model: other}\n"
        "secrets: {token: do-not-copy}\n"
    )
    result = bootstrap_profiles(
        hermes_home=tmp_path / "home", source_profile=source, store=tmp_path / "store"
    )
    selector = next(
        item for item in result["profiles"] if item["profile"] == "hmr-selector"
    )
    assert selector["files"] == [
        "SOUL.md",
        "config.yaml",
        "skills/medical-select/SKILL.md",
    ]
    from hermes_medical_research.hermes import _source_settings

    assert _source_settings(source, tmp_path / "home") == {
        "model": {"default": "test-model", "provider": "selected"},
        "providers": {"selected": {"base_url": "http://localhost:8091"}},
        "custom_providers": [
            {
                "name": "local",
                "base_url": "http://localhost:8091/v1",
                "model": "test-model",
            }
        ],
    }


def test_bootstrap_applies_clean_profiles_for_automatic_bot_discovery(
    tmp_path, monkeypatch
):
    executable = fake_hermes(tmp_path, monkeypatch)
    home = tmp_path / "hermes"
    store = tmp_path / "store"
    source = tmp_path / "source.yaml"
    source.write_text("model: test-model\nprovider: test-provider\nunrelated: secret\n")
    result = bootstrap_profiles(
        apply=True, hermes_home=home, source_profile=source, store=store
    )
    assert result["hermes"] == str(executable)
    assert result["bot_mode_discovery"] == "automatic"
    assert result["workflow"] == "user-invoked-steps"
    assert result["retired_profiles"] == []
    for name, spec in PROFILES.items():
        root = home / "profiles" / name
        config = yaml.safe_load((root / "config.yaml").read_text())
        expected = {
            "model": "test-model",
            "provider": "test-provider",
            "timezone": _system_timezone(),
            "tools": {"enabled_toolsets": list(spec.toolsets)},
        }
        if spec.max_turns:
            expected["agent"] = {"max_turns": spec.max_turns}
        if spec.kind:
            expected["mcp_servers"] = {
                MCP_SERVER_NAME: {
                    "command": _hmr_path(),
                    "args": [
                        "mcp",
                        "serve",
                        "--store",
                        str(store.resolve()),
                        "--role",
                        spec.role,
                        "--kind",
                        spec.kind,
                    ],
                    "enabled": True,
                    "timeout": 300,
                }
            }
        assert config == expected, name
        assert not set(config) & {"unrelated", "cron"}
        assert json.loads((root / "hmr-managed.json").read_text())["profile"] == name
        assert {
            path.parent.name for path in (root / "skills").glob("*/SKILL.md")
        } == set(spec.skills)
    checked = doctor(hermes_home=home, store=store)
    assert checked["provider_selection"] == {
        "configured": True,
        "timezone": _system_timezone(),
    }
    assert yaml.safe_load((home / "config.yaml").read_text()) == {
        "model": "test-model",
        "provider": "test-provider",
        "timezone": _system_timezone(),
    }


def test_bootstrap_never_replaces_an_existing_home_config(tmp_path, monkeypatch):
    fake_hermes(tmp_path, monkeypatch)
    home = tmp_path / "hermes"
    home.mkdir()
    root = home / "config.yaml"
    root.write_text("model: operator-model\nprovider: operator-provider\n")
    source = tmp_path / "source.yaml"
    source.write_text("model: copied-model\nprovider: copied-provider\n")

    bootstrap_profiles(
        apply=True, hermes_home=home, source_profile=source, store=tmp_path / "store"
    )

    assert root.read_text() == (
        "model: operator-model\nprovider: operator-provider\n"
    )


def test_bootstrap_refuses_unmanaged_and_edited_profiles(tmp_path, monkeypatch):
    fake_hermes(tmp_path, monkeypatch)
    home = tmp_path / "hermes"
    store = tmp_path / "store"
    unmanaged = home / "profiles" / "hmr-selector"
    unmanaged.mkdir(parents=True)
    (unmanaged / "personal.txt").write_text("keep me")
    with pytest.raises(ValidationError, match="unmanaged"):
        bootstrap_profiles(apply=True, hermes_home=home, store=store)
    assert (unmanaged / "personal.txt").read_text() == "keep me"

    (unmanaged / "personal.txt").unlink()
    unmanaged.rmdir()
    bootstrap_profiles(apply=True, hermes_home=home, store=store)
    soul = home / "profiles" / "hmr-selector" / "SOUL.md"
    soul.write_text("locally edited")
    with pytest.raises(ValidationError, match="edited outside"):
        bootstrap_profiles(apply=True, hermes_home=home, store=store)


def test_bootstrap_can_update_only_one_profile(tmp_path, monkeypatch):
    fake_hermes(tmp_path, monkeypatch)
    home = tmp_path / "hermes"
    store = tmp_path / "store"
    bootstrap_profiles(apply=True, hermes_home=home, store=store)
    selector = home / "profiles" / "hmr-selector" / "SOUL.md"
    selector.write_text("operator-owned drift")

    result = bootstrap_profiles(
        apply=True,
        hermes_home=home,
        store=store,
        profile="hmr-extractor",
    )
    assert [item["profile"] for item in result["profiles"]] == ["hmr-extractor"]
    config = yaml.safe_load(
        (home / "profiles" / "hmr-extractor" / "config.yaml").read_text()
    )
    assert config["agent"]["max_turns"] == PROFILES["hmr-extractor"].max_turns
    assert selector.read_text() == "operator-owned drift"

    with pytest.raises(ValidationError, match="unknown managed Hermes profile"):
        bootstrap_profiles(apply=True, hermes_home=home, store=store, profile="hmr-searcher")


def test_constrained_profile_hosts_its_submit_tool_and_forces_the_tool_call(
    tmp_path, monkeypatch
):
    fake_hermes(tmp_path, monkeypatch)
    home = tmp_path / "hermes"
    store = tmp_path / "store"
    source = tmp_path / "source.yaml"
    source.write_text(SELECTED_SOURCE)

    bootstrap_profiles(apply=True, hermes_home=home, source_profile=source, store=store)

    config = yaml.safe_load((home / "profiles" / "hmr-screen" / "config.yaml").read_text())
    assert config["mcp_servers"] == {
        MCP_SERVER_NAME: {
            "command": _hmr_path(),
            "args": [
                "mcp",
                "serve",
                "--store",
                str(store.resolve()),
                "--role",
                "selector",
                "--kind",
                "screening",
            ],
            "enabled": True,
            "timeout": 300,
        }
    }
    # ``required`` is what makes the model server compile the tool grammar from the first token,
    # and Hermes merges ``extra_body`` from the entry that ``model.provider`` names.
    assert config["providers"]["selected"]["extra_body"] == {"tool_choice": "required"}
    assert config["custom_providers"] == [
        {
            "name": "local",
            "base_url": "http://localhost:8091/v1",
            "model": "base-model",
            "extra_body": {"tool_choice": "required"},
        }
    ]
    assert config["model"]["max_tokens"] == MAX_TOKENS["screening"]
    assert config["agent"] == {"max_turns": 2}
    assert config["tools"] == {"enabled_toolsets": []}

    session = yaml.safe_load((home / "profiles" / "hmr-selector" / "config.yaml").read_text())
    assert MCP_SERVER_NAME not in (session.get("mcp_servers") or {})
    assert session["providers"] == {"selected": {"base_url": "http://localhost:8091"}}
    assert session["tools"] == {"enabled_toolsets": ["terminal", "file", "skills"]}
    assert "max_tokens" not in session["model"]

    entry = next(
        item
        for item in doctor(hermes_home=home, store=store)["profiles"]
        if item["profile"] == "hmr-screen"
    )
    assert entry["ready"] and not entry["problems"]
    assert entry["schema"] == {
        "kind": "screening",
        "tool": "submit_screening",
        "schema_digest": schema_digest("screening"),
        "tool_choice": "required",
        "mcp_server": True,
        "provider_entry": "providers.selected",
    }


def test_assignment_overrides_only_the_model_and_provider(tmp_path, monkeypatch):
    fake_hermes(tmp_path, monkeypatch)
    home = tmp_path / "hermes"
    store = tmp_path / "store"
    source = tmp_path / "source.yaml"
    source.write_text(SELECTED_SOURCE)
    with pytest.raises(ValidationError, match="constrained answer shape"):
        write_assignment(home, models={"audit": "big-model"})
    payload = write_assignment(
        home, models={"screening": "fast-model", "synthesis": "big-model@remote"}
    )
    assert payload["kinds"]["screening"] == {"model": "fast-model", "profile": "hmr-screen"}
    assert payload["kinds"]["synthesis"] == {
        "model": "big-model",
        "provider": "remote",
        "profile": "hmr-finding",
    }

    bootstrap_profiles(apply=True, hermes_home=home, source_profile=source, store=store)

    screen = yaml.safe_load((home / "profiles" / "hmr-screen" / "config.yaml").read_text())
    unassigned = yaml.safe_load((home / "profiles" / "hmr-cover" / "config.yaml").read_text())
    assert screen["model"] == {
        "default": "fast-model",
        "provider": "selected",
        "max_tokens": MAX_TOKENS["screening"],
    }
    assert unassigned["model"] == {
        "default": "base-model",
        "provider": "selected",
        "max_tokens": MAX_TOKENS["coverage"],
    }
    # Only the model choice moved: the forced tool call, the tool server and the copied provider
    # definition are identical to the profile that has no assignment.
    assert screen["providers"] == unassigned["providers"]
    assert screen["custom_providers"] == unassigned["custom_providers"]
    assert screen["timezone"] == unassigned["timezone"]
    assert screen["mcp_servers"][MCP_SERVER_NAME]["args"][-1] == "screening"

    finding = yaml.safe_load((home / "profiles" / "hmr-finding" / "config.yaml").read_text())
    assert finding["model"]["default"] == "big-model"
    assert finding["model"]["provider"] == "remote"

    # A session profile answers no constrained kind, so no assignment can reach it.
    session = yaml.safe_load((home / "profiles" / "hmr-selector" / "config.yaml").read_text())
    assert session["model"] == {"default": "base-model", "provider": "selected"}


def test_bootstrap_remove_deletes_only_the_files_it_installed(tmp_path, monkeypatch):
    fake_hermes(tmp_path, monkeypatch)
    home = tmp_path / "hermes"
    store = tmp_path / "store"
    bootstrap_profiles(apply=True, hermes_home=home, store=store)
    selector = home / "profiles" / "hmr-selector"
    (selector / "notes.md").write_text("operator note")

    result = bootstrap_profiles(apply=True, remove=True, hermes_home=home, store=store)

    assert result["applied"]
    assert {item["profile"] for item in result["removed"]} == set(PROFILES)
    assert {"profile": "hmr-selector", "removed": False, "kept": ["notes.md"]} in (
        result["removed"]
    )
    assert (selector / "notes.md").read_text() == "operator note"
    assert not (selector / "SOUL.md").exists()
    assert not (selector / "hmr-managed.json").exists()
    assert not (selector / "skills").exists()
    for name in PROFILES:
        if name != "hmr-selector":
            assert not (home / "profiles" / name).exists()


def test_retired_searcher_profile_is_reported_and_removed(tmp_path, monkeypatch):
    fake_hermes(tmp_path, monkeypatch)
    home = tmp_path / "hermes"
    store = tmp_path / "store"
    assert "hmr-searcher" in RETIRED_PROFILES and "hmr-searcher" not in PROFILES
    searcher = home / "profiles" / "hmr-searcher"
    searcher.mkdir(parents=True)
    soul = searcher / "SOUL.md"
    soul.write_text("the 0.5.x searcher")
    (searcher / "hmr-managed.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "profile": "hmr-searcher",
                "files": {"SOUL.md": hashlib.sha256(soul.read_bytes()).hexdigest()},
            }
        )
    )

    plan = bootstrap_profiles(hermes_home=home, store=store)
    assert plan["retired_profiles"] == [
        {"profile": "hmr-searcher", "root": str(searcher)}
    ]
    assert searcher.is_dir()

    applied = bootstrap_profiles(apply=True, hermes_home=home, store=store)
    assert applied["retired_profiles"] == [
        {"profile": "hmr-searcher", "removed": True, "kept": []}
    ]
    assert not searcher.exists()
    assert doctor(hermes_home=home, store=store)["retired_profiles"] == []


def test_doctor_reports_a_legacy_fleet_and_missing_quick_commands_as_not_ready(
    tmp_path, monkeypatch
):
    hermes_executable = fake_hermes(tmp_path, monkeypatch)
    home = tmp_path / "hermes"
    store = tmp_path / "store"
    source = tmp_path / "source.yaml"
    source.write_text(SELECTED_SOURCE)
    bootstrap_profiles(apply=True, hermes_home=home, source_profile=source, store=store)

    checked = doctor(hermes_home=home, store=store)
    assert checked["workflow"] == "user-invoked-steps"
    assert [item["profile"] for item in checked["profiles"] if not item["ready"]] == []
    assert checked["legacy_routines"] == {"installed": False, "jobs": [], "problems": []}
    assert not checked["quick_commands"]["installed"]
    assert not checked["ready"]

    # A 0.5.x cron fleet would claim work on a timer and race the operator's own step runner.
    cron = home / "profiles" / "hmr-selector" / "cron"
    cron.mkdir(parents=True)
    (cron / "jobs.json").write_text(
        json.dumps({"jobs": [{"id": "job-1", "name": "hmr-work-selector", "enabled": True}]})
    )
    (home / "hmr-routines.json").write_text(
        json.dumps(
            {"scripts": {}, "jobs": [{"profile": "hmr-selector", "name": "hmr-work-selector"}]}
        )
    )
    quick_commands.install(store=store, apply=True, hermes_home=home)

    checked = doctor(hermes_home=home, store=store)
    assert checked["quick_commands"]["installed"]
    assert checked["legacy_routines"]["installed"]
    assert checked["legacy_routines"]["jobs"] == ["hmr-selector/hmr-work-selector"]
    assert checked["legacy_routines"]["problems"] == [
        "legacy cron routines are still installed; run `hmr hermes routines --remove --apply`"
    ]
    assert not checked["ready"]

    (home / "hmr-routines.json").unlink()
    monkeypatch.setattr(
        "hermes_medical_research.hermes.shutil.which",
        lambda name: str(hermes_executable) if name == "hermes" else "/usr/local/bin/hmr",
    )
    ready = doctor(hermes_home=home, store=store)
    assert ready["ready"]
    assert ready["assignments"] == {"path": "hmr-steps.json", "kinds": {}, "problems": []}
