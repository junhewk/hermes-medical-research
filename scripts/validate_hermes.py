"""Check Hermes install scanning, activation, and skill access in temporary profiles.

Requires a committed plugin checkout and an external Hermes source checkout. The
installer clones the committed plugin tree locally, including its tests. No user
profile is changed, no scanner rules are patched, and no force option is used.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

SKILLS = {"medical-deep-research", "medical-literature-search"}
PLUGIN = "medical-deep-research-plugin"


def probe(enabled: bool) -> None:
    from hermes_cli.plugins import get_plugin_manager
    from tools.skills_tool import skill_view, skills_list

    listing = json.loads(skills_list(category="plugin"))
    assert listing["success"], listing
    skills = listing["skills"]
    if not enabled:
        assert not skills, listing
        print("Installed but disabled: no plugin skills exposed")
        return
    assert {s["name"].split(":")[-1] for s in skills} == SKILLS, listing
    assert len(skills) == len(SKILLS), listing
    loaded = get_plugin_manager()._plugins[PLUGIN]
    assert set(loaded.tools_registered) == {"medical_research_session", "medical_research_review"}
    assert loaded.hooks_registered == ["pre_tool_call"]

    for skill in skills:
        name = skill["name"]
        path = get_plugin_manager().find_plugin_skill(name)
        assert path and path.is_relative_to(Path(os.environ["HERMES_HOME"]) / "plugins"), path
        viewed = json.loads(skill_view(name))
        assert viewed["success"] and viewed["name"] == name, viewed
        assert path.read_text() in viewed["content"], name
        for reference in path.parent.glob("references/*.md"):
            relative = reference.relative_to(path.parent).as_posix()
            linked = json.loads(skill_view(name, file_path=relative))
            assert linked["success"] and reference.read_text() in linked["content"], linked
        print(f"Available and readable: {name}")


def probe_short_names() -> None:
    from agent.prompt_builder import build_skills_system_prompt
    from agent.skill_commands import build_skill_invocation_message, reload_skills
    from hermes_cli.config import load_config, save_config
    from tools.skills_tool import skill_view, skills_list

    # A portable install alone does not expose these names in the startup index.
    before = build_skills_system_prompt()
    assert all(f"- {name}:" not in before for name in SKILLS), before
    reload_skills()
    home = Path(os.environ["HERMES_HOME"])
    root = home / "plugins" / PLUGIN / "skills"
    config = load_config()
    config.setdefault("skills", {}).setdefault("external_dirs", []).append(
        f"plugins/{PLUGIN}/skills"
    )
    save_config(config)
    # No process restart or manual cache clearing: exercise supported reload behavior.
    reloaded = reload_skills()
    assert {entry["name"] for entry in reloaded["added"]} >= SKILLS, reloaded
    prompt = build_skills_system_prompt()
    listing = json.loads(skills_list())
    assert listing["success"], listing
    assert {entry["name"] for entry in listing["skills"]} >= SKILLS, listing
    for name in sorted(SKILLS):
        assert f"- {name}:" in prompt, name
        viewed = json.loads(skill_view(name))
        assert viewed["success"] and Path(viewed["skill_dir"]) == root / name, viewed
        assert (root / name / "SKILL.md").read_text() in viewed["content"], name
        for reference in (root / name / "references").glob("*.md"):
            linked = json.loads(skill_view(name, file_path=f"references/{reference.name}"))
            assert linked["success"] and reference.read_text() in linked["content"], linked
        invoked = build_skill_invocation_message(f"/{name}", "Run the requested research task.")
        assert invoked and str(root / name) in invoked, name
        assert "Run the requested research task." in invoked, name
        print(f"Indexed, readable by short name, and invocable as /{name}")


def validate(plugin_root: Path, hermes_source: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="mdr-hermes-validation-") as temporary:
        # Set before importing Hermes: each child is a new session in this profile.
        os.environ["HERMES_HOME"] = temporary
        os.environ["HERMES_ENABLE_PROJECT_PLUGINS"] = "0"
        from hermes_cli.plugins_cmd import (
            _install_plugin_core,
            _scan_on_install_enabled,
            cmd_enable,
        )
        from tools.plugin_guard import (
            PLUGIN_SCANNER_VERSION,
            scan_plugin,
            should_allow_plugin_install,
        )

        assert _scan_on_install_enabled(), "Installation scanning must be enabled"
        target, manifest, name = _install_plugin_core(plugin_root.as_uri(), force=False)
        assert name == PLUGIN and manifest["version"], manifest
        # Expose the unchanged upstream verdict as evidence, in addition to the
        # installer's gate above. This scans the complete installed source tree.
        scan = scan_plugin(target, source=plugin_root.as_uri())
        allowed, reason = should_allow_plugin_install(scan, force=False)
        assert allowed is True, reason
        print(
            json.dumps(
                {
                    "scanner": PLUGIN_SCANNER_VERSION,
                    "verdict": scan.verdict,
                    "allowed": allowed,
                    "findings": dict(Counter(f.severity for f in scan.findings)),
                }
            ),
            flush=True,
        )
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--hermes-source",
            str(hermes_source),
            "--probe",
        ]
        subprocess.run([*command, "disabled"], check=True, cwd=temporary)
        cmd_enable(name, allow_tool_override=False)
        subprocess.run([*command, "enabled"], check=True, cwd=temporary)
        subprocess.run([*command, "indexed"], check=True, cwd=temporary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-source", type=Path, required=True)
    parser.add_argument("--plugin-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--probe", choices=("disabled", "enabled", "indexed"), help=argparse.SUPPRESS
    )
    args = parser.parse_args()
    source = args.hermes_source.resolve(strict=True)
    sys.path.insert(0, str(source))
    if args.probe == "indexed":
        probe_short_names()
    elif args.probe:
        probe(args.probe == "enabled")
    else:
        validate(args.plugin_root.resolve(strict=True), source)
