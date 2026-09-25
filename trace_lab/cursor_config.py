"""Create the minimal isolated Cursor CLI configuration used by the lab."""

import json
import os
from pathlib import Path


def initialize(path="/home/agent/.cursor/cli-config.json"):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps({
        "version": 1,
        "network": {"useHttp1ForAgent": True},
        "sandbox": {"mode": "disabled"},
        "permissions": {"allow": [], "deny": []},
        "approvalMode": "allowlist",
        "attribution": {"attributeCommitsToAgent": False,
                        "attributePRsToAgent": False},
    }, separators=(",", ":")) + "\n")
    os.chown(destination.parent, 1000, 1000)
    os.chown(destination, 1000, 1000)


if __name__ == "__main__":
    initialize()
