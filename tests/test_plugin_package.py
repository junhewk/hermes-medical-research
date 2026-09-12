from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plugin_bundle_and_reproducible_archive(tmp_path):
    script("validate_bundle").validate(ROOT)
    package = script("package_plugin").package
    first = package(ROOT, tmp_path / "first.zip")
    second = package(ROOT, tmp_path / "second.zip")
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        names = archive.namelist()
        assert any(n.endswith("/.claude-plugin/marketplace.json") for n in names)
        assert any(n.endswith("/skills/medical-deep-research/SKILL.md") for n in names)
        for name in names:
            assert not {".board", ".env", "runs", ".git", ".venv", "__pycache__"} & set(
                Path(name).parts
            )


def test_headless_cli_creates_protocol_and_child_plan(tmp_path):
    def cli(*args):
        result = subprocess.run(
            [sys.executable, "-m", "medical_deep_research_plugin", *map(str, args)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        return json.loads(result.stdout)

    run = tmp_path / "research"
    result = cli("research", "init", ROOT / "examples/research-protocol.json", "--output", run)
    assert result["limits"] == {"records_per_source": 100, "fulltexts": 30}
    result = cli(
        "plan",
        run / "question.json",
        "--research-run",
        run,
        "--mode",
        "quick",
        "--sources",
        "europe-pmc",
        "--limit-per-source",
        "75",
        "--no-mesh",
        "--json",
    )
    assert result["strategy"]["question"]["filters"]["from_date"] is None
    assert result["strategy"]["question"]["search_components"] == ["population", "intervention"]
    assert cli("research", "status", run)["allocated_by_source"] == {"europe-pmc": 75}
