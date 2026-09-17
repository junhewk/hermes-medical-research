"""The v0.5 distribution is a normal Python tool with four Hermes skills."""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

from hermes_medical_research import __version__
from hermes_medical_research.cli import parser
from hermes_medical_research.hermes import PROFILE_SKILLS

ROOT = Path(__file__).parents[1]


def test_distribution_has_one_executable_and_new_identity():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["name"] == "hermes-medical-research"
    assert project["version"] == __version__ == "0.5.9"
    assert project["scripts"] == {"mdr": "hermes_medical_research.cli:main"}
    assert set(parser()._subparsers._group_actions[0].choices) == {
        "run",
        "search",
        "select",
        "extract",
        "synthesize",
        "audit",
        "source",
        "finalize",
        "review",
        "work",
        "hermes",
    }


def test_exactly_four_small_hermes_skills_are_checked_in():
    skills = sorted((ROOT / "skills").glob("*/SKILL.md"))
    assert [path.parent.name for path in skills] == [
        "medical-extract",
        "medical-search",
        "medical-select",
        "medical-synthesize",
    ]
    for path in skills:
        text = path.read_text()
        _, header, body = text.split("---", 2)
        metadata = yaml.safe_load(header)
        assert metadata["name"] == path.parent.name
        assert metadata["metadata"]["hermes"]["requires_toolsets"] == [
            "terminal",
            "file",
        ]
        assert "run_id" in body and "task_id" in body and len(text.encode()) < 4096
        packaged = (
            ROOT
            / "src/hermes_medical_research/resources/skills"
            / path.parent.name
            / "SKILL.md"
        )
        assert packaged.read_text() == text


def test_profiles_cover_every_role_without_a_plugin_lifecycle():
    assert set(PROFILE_SKILLS) == {
        "mdr-coordinator",
        "mdr-searcher",
        "mdr-selector",
        "mdr-extractor",
        "mdr-synthesizer",
        "mdr-auditor",
    }
    assert not any((ROOT / ".codex-plugin").glob("*"))
    assert not any((ROOT / ".claude-plugin").glob("*"))
    assert not (ROOT / "plugin.json").exists()
    assert not (ROOT / "plugin.yaml").exists()
