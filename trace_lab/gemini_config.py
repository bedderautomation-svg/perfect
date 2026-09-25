"""Minimal native Gemini CLI settings in the disposable agent home."""

import json
import os
from pathlib import Path


def initialize(path="/home/agent/.gemini/settings.json"):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps({
        "security": {"auth": {"selectedType": "gemini-api-key"},
                     "folderTrust": {"enabled": False}},
        "tools": {"sandbox": False},
        "skills": {"enabled": True},
        "general": {"disableAutoUpdate": True, "disableUpdateNag": True,
                    "sessionRetention": {"enabled": False}},
        "privacy": {"usageStatisticsEnabled": False},
        "telemetry": {"enabled": False},
    }) + "\n")
    os.chown(destination.parent, 1000, 1000)
    os.chown(destination, 1000, 1000)


if __name__ == "__main__":
    initialize()
