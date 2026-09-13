"""Hermes native entry point; other hosts use their own plugin manifests."""
import sys
from pathlib import Path


def register(ctx):
    root = Path(__file__).resolve().parent
    source = str(root / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
    from medical_deep_research_plugin.hermes_host import register as register_host
    register_host(ctx)
    for path in sorted((root / "skills").glob("*/SKILL.md")):
        ctx.register_skill(path.parent.name, path, path.parent.name.replace("-", " "))
