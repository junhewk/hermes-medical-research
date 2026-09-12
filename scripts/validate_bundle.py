"""Offline package consistency and Agent Skills checks, also used by CI."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def validate(root: Path = ROOT) -> None:
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    for path in ("plugin.json", ".codex-plugin/plugin.json", ".claude-plugin/plugin.json"):
        manifest = json.loads((root / path).read_text())
        assert manifest["name"] == project["name"], path
        assert manifest["version"] == project["version"], path
        assert manifest["description"].strip(), path
    portable = json.loads((root / "plugin.json").read_text())
    assert portable["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
    assert not set(portable) - {
        "$schema",
        "name",
        "version",
        "description",
        "author",
        "homepage",
        "repository",
        "license",
        "keywords",
        "extensions",
    }
    marketplace = json.loads((root / ".claude-plugin/marketplace.json").read_text())
    assert marketplace["plugins"][0]["source"] == "./"
    assert marketplace["plugins"][0]["name"] == project["name"]
    assert marketplace["plugins"][0]["version"] == project["version"]
    skills = list((root / "skills").glob("*/SKILL.md"))
    assert {p.parent.name for p in skills} == {"medical-deep-research", "medical-literature-search"}
    for path in skills:
        content = path.read_text()
        assert content.startswith("---\n"), path
        frontmatter = yaml.safe_load(content.split("---", 2)[1])
        assert frontmatter["name"] == path.parent.name
        assert isinstance(frontmatter["description"], str) and frontmatter["description"].strip()
        assert len(content.splitlines()) < 150
        pins = re.findall(r"medical-deep-research-plugin\.git@(v[^\s`]+)", content)
        assert pins and set(pins) == {"v" + project["version"]}, path
        for relative in re.findall(r"\]\((references/[^)]+)\)", content):
            target = (path.parent / relative).resolve()
            assert target.is_relative_to(root.resolve()) and target.is_file(), target
    print(f"Bundle valid: {project['name']} {project['version']}; {len(skills)} shared skills")


if __name__ == "__main__":
    validate()
