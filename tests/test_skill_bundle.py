from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

from hermes_medical_search import __version__
from hermes_medical_search.cli import build_parser

SKILL = Path(__file__).parents[1] / "skills" / "medical-literature-search"
SKILL_MD = SKILL / "SKILL.md"
PLACEHOLDERS = {
    "<run-dir>": "runs/example",
    "<sha256>": "0" * 64,
    "TOKEN": "0123456789abcdef",
}


def skill_commands() -> list[list[str]]:
    """Every `hermes-medical-search ...` invocation the skill tells an agent to run."""
    commands: list[list[str]] = []
    for line in SKILL_MD.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if "hermes-medical-search" not in stripped or stripped.startswith(("-", "#", "|")):
            continue
        for placeholder, value in PLACEHOLDERS.items():
            stripped = stripped.replace(placeholder, value)
        tokens = shlex.split(stripped)
        # Match the executable exactly, and take the last occurrence: `uvx --from <src>` puts a
        # source ahead of it that can itself end in "hermes-medical-search" (a local checkout).
        indices = [i for i, token in enumerate(tokens) if token == "hermes-medical-search"]
        if not indices:
            continue
        argv = tokens[indices[-1] + 1 :]
        if not argv or argv[0].startswith("-"):  # bare --help/--version carry no subcommand
            continue
        commands.append(argv)
    return commands


def test_skill_metadata_and_progressive_disclosure() -> None:
    content = SKILL_MD.read_text(encoding="utf-8")
    assert content.startswith("---\n")
    frontmatter = content.split("---", 2)[1]
    keys = [line.split(":", 1)[0] for line in frontmatter.splitlines() if ":" in line]
    assert keys == ["name", "description"]
    assert "name: medical-literature-search" in frontmatter
    assert "Use when" in frontmatter
    assert len(content.splitlines()) < 100
    assert "approve <run-dir>" in content
    assert "report generation" in content


def test_skill_pins_the_current_release() -> None:
    """The pin must track __version__ so a release bump cannot leave the skill on a stale tag."""
    content = SKILL_MD.read_text(encoding="utf-8")
    pins = set(re.findall(r"hermes-medical-search\.git@(v[^\s]+)", content))
    assert pins == {f"v{__version__}"}


def test_skill_documents_only_real_commands() -> None:
    """Guards the skill against a CLI rename: every documented invocation must still parse."""
    commands = skill_commands()
    assert len(commands) >= 5
    parser = build_parser()
    for argv in commands:
        parser.parse_args(argv)


def test_skill_covers_every_step_of_the_review_workflow() -> None:
    documented = {argv[0] for argv in skill_commands()}
    assert {"run", "plan", "approve", "preflight", "search", "doctor"} <= documented


def test_skill_references_exist_and_no_executable_credentials_are_bundled() -> None:
    content = SKILL_MD.read_text(encoding="utf-8")
    links = re.findall(r"\]\((references/[^)]+)\)", content)
    assert links
    assert all((SKILL / relative).is_file() for relative in links)
    assert not (SKILL / "scripts").exists()
    assert not (SKILL / "README.md").exists()
    assert "os.environ" not in "\n".join(
        path.read_text(encoding="utf-8") for path in SKILL.rglob("*") if path.is_file()
    )


@pytest.mark.parametrize("flag", ["--limit-per-source", "--variant", "--strategy-digest", "--json"])
def test_flags_quoted_in_the_skill_still_exist(flag: str) -> None:
    content = SKILL_MD.read_text(encoding="utf-8") + (
        SKILL / "references" / "artifacts-and-sources.md"
    ).read_text(encoding="utf-8")
    if flag not in content:
        pytest.skip(f"{flag} is not referenced by the skill")
    actions = {
        option
        for action in build_parser()._subparsers._group_actions  # noqa: SLF001
        for parser in getattr(action, "choices", {}).values()
        for option in parser._option_string_actions  # noqa: SLF001
    }
    assert flag in actions
