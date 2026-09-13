"""Build a reproducible, allowlisted plugin ZIP without local state or credentials."""

from __future__ import annotations

import argparse
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "plugin.json",
    "plugin.yaml",
    "__init__.py",
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "LICENSE",
    "CHANGELOG.md",
    "VALIDATION.md",
)
DIRECTORIES = (
    ".codex-plugin",
    ".claude-plugin",
    "skills",
    "src",
    "examples",
    "scripts",
    "hooks",
    "agents",
)


def package(root: Path = ROOT, output: Path | None = None) -> Path:
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    name = f"{project['name']}-{project['version']}"
    target = output or root / "dist" / f"{name}.zip"
    candidates = [root / path for path in FILES]
    for directory in DIRECTORIES:
        candidates.extend((root / directory).rglob("*"))
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(candidates):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            if path.is_symlink():
                raise ValueError(f"release bundle cannot contain symlinks: {path}")
            entry = zipfile.ZipInfo(
                f"{name}/{path.relative_to(root).as_posix()}", (2026, 1, 1, 0, 0, 0)
            )
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, path.read_bytes())
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    print(package(output=args.output))
