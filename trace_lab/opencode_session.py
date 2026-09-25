"""Offline native OpenCode session initialization for the direct control.

Uses the pinned CLI's import format; no model or network request is made.
"""

import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import time


def seed_export(session_id):
    timestamp = int(time.time() * 1000)
    suffix = session_id.removeprefix("ses_")
    message_id, part_id = "msg_" + suffix, "prt_" + suffix
    return {
        "info": {"id": session_id, "slug": "bookkeeping", "projectID": "global",
                 "directory": "/workspace", "title": "Runtime bookkeeping",
                 "version": "1.18.30", "time": {"created": timestamp, "updated": timestamp}},
        "messages": [{
            "info": {"id": message_id, "sessionID": session_id, "role": "user",
                     "time": {"created": timestamp}, "agent": "build",
                     "model": {"providerID": "trace_lab", "modelID": "bookkeeping"}},
            "parts": [{"id": part_id, "sessionID": session_id, "messageID": message_id,
                       "type": "text", "text": "This run is ready for its bookkeeping task."}],
        }],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="trace-lab-session-") as temporary:
        source = Path(temporary) / "session.json"
        source.write_text(json.dumps(seed_export(args.session_id)))
        result = subprocess.run(["opencode", "--pure", "import", str(source)],
                                capture_output=True, text=True, timeout=25)
    if result.returncode or f"Imported session: {args.session_id}" not in result.stdout:
        raise RuntimeError("Native OpenCode session initialization failed: " + result.stderr)
    print(json.dumps({"session_id": args.session_id, "initialized": True}))


if __name__ == "__main__":
    main()
