"""Hermes bootstrap is explicit, isolated, and ownership-safe."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from hermes_medical_research.hermes import (
    PROFILE_SKILLS,
    _system_timezone,
    bootstrap_profiles,
    doctor,
)
from hermes_medical_research.search.models import ValidationError


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
    result = bootstrap_profiles(hermes_home=home)
    assert not result["applied"]
    assert {item["profile"] for item in result["profiles"]} == set(PROFILE_SKILLS)
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
        hermes_home=tmp_path / "home", source_profile=source
    )
    selector = next(
        item for item in result["profiles"] if item["profile"] == "mdr-selector"
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
    source = tmp_path / "source.yaml"
    source.write_text("model: test-model\nprovider: test-provider\nunrelated: secret\n")
    result = bootstrap_profiles(
        apply=True, hermes_home=home, source_profile=source
    )
    assert result["hermes"] == str(executable)
    assert result["bot_mode_discovery"] == "automatic"
    assert result["workflow"] == "cron-routines"
    for name, skills in PROFILE_SKILLS.items():
        root = home / "profiles" / name
        config = yaml.safe_load((root / "config.yaml").read_text())
        assert config == {
            "model": "test-model",
            "provider": "test-provider",
            "timezone": _system_timezone(),
            "cron": {
                "model": "test-model",
                "model_provider": "test-provider",
            },
            "tools": {"enabled_toolsets": ["terminal", "file", "skills"]},
        }
        assert not set(config) & {"unrelated"}
        assert json.loads((root / "mdr-managed.json").read_text())["profile"] == name
        assert {
            path.parent.name for path in (root / "skills").glob("*/SKILL.md")
        } == set(skills)
    checked = doctor(hermes_home=home)
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

    bootstrap_profiles(apply=True, hermes_home=home, source_profile=source)

    assert root.read_text() == (
        "model: operator-model\nprovider: operator-provider\n"
    )


def test_bootstrap_refuses_unmanaged_and_edited_profiles(tmp_path, monkeypatch):
    fake_hermes(tmp_path, monkeypatch)
    home = tmp_path / "hermes"
    unmanaged = home / "profiles" / "mdr-selector"
    unmanaged.mkdir(parents=True)
    (unmanaged / "personal.txt").write_text("keep me")
    with pytest.raises(ValidationError, match="unmanaged"):
        bootstrap_profiles(apply=True, hermes_home=home)
    assert (unmanaged / "personal.txt").read_text() == "keep me"

    (unmanaged / "personal.txt").unlink()
    unmanaged.rmdir()
    bootstrap_profiles(apply=True, hermes_home=home)
    soul = home / "profiles" / "mdr-selector" / "SOUL.md"
    soul.write_text("locally edited")
    with pytest.raises(ValidationError, match="edited outside"):
        bootstrap_profiles(apply=True, hermes_home=home)


def test_doctor_requires_managed_cron_routines_not_bot_roster_metadata(
    tmp_path, monkeypatch
):
    fake_hermes(tmp_path, monkeypatch)
    home = tmp_path / "hermes"
    bootstrap_profiles(apply=True, hermes_home=home)
    checked = doctor(hermes_home=home)
    assert not checked["ready"]
    assert checked["routines"]["problems"] == [
        "managed cron routines are not installed"
    ]
    (home / "profiles" / "mdr-coordinator" / "profile.yaml").write_text(
        "ui_meta:\n  hermes-bots: {}\n"
    )
    assert not doctor(hermes_home=home)["ready"]
