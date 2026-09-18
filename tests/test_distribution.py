"""The v0.5 distribution is a normal Python tool with three Hermes skills."""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

from hermes_medical_research import __version__
from hermes_medical_research.cli import parser
from hermes_medical_research.hermes import PROFILES

ROOT = Path(__file__).parents[1]


def test_distribution_has_one_executable_and_new_identity():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["name"] == "hermes-medical-research"
    assert project["version"] == __version__
    assert project["scripts"] == {"hmr": "hermes_medical_research.cli:main"}
    assert set(parser()._subparsers._group_actions[0].choices) == {
        "search",
        "select",
        "extract",
        "synthesize",
        "audit",
        "source",
        "finalize",
        "review",
        "run",
        "work",
        "hermes",
        "mcp",
        "step",
    }


def test_exactly_three_small_hermes_skills_are_checked_in():
    skills = sorted((ROOT / "skills").glob("*/SKILL.md"))
    assert [path.parent.name for path in skills] == [
        "medical-extract",
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
    assert set(PROFILES) == {
        "hmr-coordinator",
        "hmr-selector",
        "hmr-extractor",
        "hmr-synthesizer",
        "hmr-auditor",
        "hmr-screen",
        "hmr-cover",
        "hmr-link",
        "hmr-finding",
    }
    # A constrained profile's whole tool surface is its one submit tool: no skill to read, no
    # shell toolset to run, and two turns — the tool call and the turn that sees its result.
    constrained = {name: spec for name, spec in PROFILES.items() if spec.kind}
    assert set(constrained) == {"hmr-screen", "hmr-cover", "hmr-link", "hmr-finding"}
    for name, spec in constrained.items():
        assert spec.skills == () and spec.toolsets == (), name
        assert spec.max_turns == 2, name
    assert not any((ROOT / ".codex-plugin").glob("*"))
    assert not any((ROOT / ".claude-plugin").glob("*"))
    assert not (ROOT / "plugin.json").exists()
    assert not (ROOT / "plugin.yaml").exists()
