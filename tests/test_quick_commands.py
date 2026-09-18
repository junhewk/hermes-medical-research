"""The managed quick-command region in the operator's own Hermes config."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from hermes_medical_research import quick_commands
from hermes_medical_research.search.models import ValidationError

ORIGINAL = """# my hermes config
model:
  default: qwen3.8-flash-next
  provider: basic

quick_commands:
  deploy:
    type: exec
    command: scripts/deploy.sh
"""


def home_with(tmp_path: Path, text: str) -> Path:
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "config.yaml").write_text(text)
    return home


def test_a_dry_run_prints_the_region_and_writes_nothing(tmp_path):
    home = home_with(tmp_path, ORIGINAL)

    plan = quick_commands.install(store=tmp_path / "store", hermes_home=home)

    assert plan["applied"] is False
    assert "hmr-selector" in plan["commands"] and "hmr-status" in plan["commands"]
    assert quick_commands.REGION_START in plan["region"]
    assert (home / "config.yaml").read_text() == ORIGINAL
    assert not (home / quick_commands.MANIFEST).exists()


def test_apply_keeps_comments_key_order_and_foreign_quick_commands(tmp_path):
    home = home_with(tmp_path, ORIGINAL)

    quick_commands.install(store=tmp_path / "store", hermes_home=home, apply=True)

    text = (home / "config.yaml").read_text()
    assert text.startswith("# my hermes config")
    parsed = yaml.safe_load(text)
    assert parsed["model"]["default"] == "qwen3.8-flash-next"
    assert parsed["quick_commands"]["deploy"] == {"type": "exec",
                                                  "command": "scripts/deploy.sh"}
    assert parsed["quick_commands"]["hmr-selector"]["type"] == "exec"
    assert "step select" in parsed["quick_commands"]["hmr-selector"]["command"]
    recorded = json.loads((home / quick_commands.MANIFEST).read_text())
    assert set(recorded["commands"]) == set(parsed["quick_commands"]) - {"deploy"}
    assert sorted(path.name for path in home.glob("config.yaml.hmr-backup-*"))


def test_every_command_is_self_contained_with_no_arguments(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: f"/opt/bin/{name}")
    home = home_with(tmp_path, ORIGINAL)

    quick_commands.install(store=tmp_path / "store", hermes_home=home, apply=True)

    installed = yaml.safe_load((home / "config.yaml").read_text())["quick_commands"]
    for name, entry in installed.items():
        if not name.startswith("hmr-"):
            continue
        # A quick command receives no arguments, so a placeholder would reach the shell verbatim.
        assert entry["command"].startswith("/opt/bin/hmr ")
        assert "--store" in entry["command"]
        assert "NAME" not in entry["command"] and "<" not in entry["command"]


def test_a_baked_review_name_is_quoted(tmp_path):
    home = home_with(tmp_path, ORIGINAL)

    quick_commands.install(store=tmp_path / "store", hermes_home=home, apply=True,
                           review="ai med ed")

    installed = yaml.safe_load((home / "config.yaml").read_text())["quick_commands"]
    assert "--review 'ai med ed'" in installed["hmr-selector"]["command"]


def test_an_unmanaged_command_of_the_same_name_is_refused(tmp_path):
    home = home_with(tmp_path, ORIGINAL + "  hmr-selector:\n    type: exec\n    command: mine\n")

    plan = quick_commands.install(store=tmp_path / "store", hermes_home=home)
    assert any("unmanaged quick command" in problem for problem in plan["problems"])
    with pytest.raises(ValidationError):
        quick_commands.install(store=tmp_path / "store", hermes_home=home, apply=True)
    assert "command: mine" in (home / "config.yaml").read_text()


def test_a_managed_entry_edited_outside_the_package_is_refused(tmp_path):
    home = home_with(tmp_path, ORIGINAL)
    quick_commands.install(store=tmp_path / "store", hermes_home=home, apply=True)
    text = (home / "config.yaml").read_text().replace("step select", "step select --limit 1")
    (home / "config.yaml").write_text(text)

    report = quick_commands.status(hermes_home=home, store=tmp_path / "store")

    assert any("edited outside hmr" in problem for problem in report["problems"])
    with pytest.raises(ValidationError):
        quick_commands.install(store=tmp_path / "store", hermes_home=home, apply=True)


def test_remove_restores_the_original_bytes(tmp_path):
    home = home_with(tmp_path, ORIGINAL)
    quick_commands.install(store=tmp_path / "store", hermes_home=home, apply=True)

    quick_commands.install(store=tmp_path / "store", hermes_home=home, apply=True, remove=True)

    assert (home / "config.yaml").read_text() == ORIGINAL
    assert not (home / quick_commands.MANIFEST).exists()


def test_install_into_a_config_without_quick_commands(tmp_path):
    home = home_with(tmp_path, "model:\n  default: qwen3.8-flash-next\n")

    quick_commands.install(store=tmp_path / "store", hermes_home=home, apply=True)
    parsed = yaml.safe_load((home / "config.yaml").read_text())
    assert "hmr-status" in parsed["quick_commands"]

    quick_commands.install(store=tmp_path / "store", hermes_home=home, apply=True, remove=True)
    assert (home / "config.yaml").read_text() == "model:\n  default: qwen3.8-flash-next\n"


def test_a_config_that_is_not_a_mapping_is_refused_without_writing(tmp_path):
    home = home_with(tmp_path, "- one\n- two\n")

    with pytest.raises(ValidationError):
        quick_commands.install(store=tmp_path / "store", hermes_home=home, apply=True)
    assert (home / "config.yaml").read_text() == "- one\n- two\n"
