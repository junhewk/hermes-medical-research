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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-source", type=Path, required=True)
    parser.add_argument("--plugin-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--probe", choices=("disabled", "enabled"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    source = args.hermes_source.resolve(strict=True)
    sys.path.insert(0, str(source))
    if args.probe:
        probe(args.probe == "enabled")
    else:
        validate(args.plugin_root.resolve(strict=True), source)
