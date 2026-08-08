from __future__ import annotations

import re
from pathlib import Path

SKILL = Path(__file__).parents[1] / "skills" / "medical-literature-search"


def test_skill_metadata_and_progressive_disclosure() -> None:
    content = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    assert content.startswith("---\n")
    frontmatter = content.split("---", 2)[1]
    keys = [line.split(":", 1)[0] for line in frontmatter.splitlines() if ":" in line]
    assert keys == ["name", "description"]
    assert "name: medical-literature-search" in frontmatter
    assert "Use when" in frontmatter
    assert len(content.splitlines()) < 100
    assert "@v0.1.0" in content
    assert "report generation" in content


def test_skill_references_exist_and_no_executable_credentials_are_bundled() -> None:
    content = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    links = re.findall(r"\]\((references/[^)]+)\)", content)
    assert links
    assert all((SKILL / relative).is_file() for relative in links)
    assert not (SKILL / "scripts").exists()
    assert not (SKILL / "README.md").exists()
    assert "os.environ" not in "\n".join(
        path.read_text(encoding="utf-8")
        for path in SKILL.rglob("*")
        if path.is_file()
    )
