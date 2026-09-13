"""Host lifecycle entry point; invoked through the existing plugin Python environment."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from medical_deep_research_plugin.native_hooks import deny, handle

if __name__ == "__main__":
    event = json.load(sys.stdin)
    host = "codex" if os.environ.get("PLUGIN_ROOT") else "claude-code"
    try:
        registry = (
            os.environ.get("MDR_NATIVE_REGISTRY_DIR")
            or os.environ.get("PLUGIN_DATA")
            or os.environ.get("CLAUDE_PLUGIN_DATA")
        )
        if not registry:
            raise RuntimeError(
                "Host did not expose stable plugin data storage for shared accounting"
            )
        print(json.dumps(handle(event, host, registry)))
    except Exception as exc:
        # A malformed receipt never becomes a pass. Pre-tool errors block the operation;
        # observer errors are surfaced without asking the reviewer to loop indefinitely.
        value = (
            deny(f"Medical native bridge: {exc}")
            if event.get("hook_event_name") == "PreToolUse"
            else {"systemMessage": f"Medical native bridge: {exc}"}
        )
        print(json.dumps(value))
