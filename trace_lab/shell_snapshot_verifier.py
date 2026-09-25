"""Independently verify the health contract using only the persisted Claude snapshot."""

import json
from pathlib import Path
import subprocess

from .shell_unit_testing_fixture import REFERENCE_PATH


def verify(home=Path("/home/agent"), workspace=Path("/workspace")):
    root = home / ".claude/shell-snapshots"
    paths = sorted(path for path in root.glob("snapshot-bash-*.sh")
                   if path.is_file() and not path.is_symlink())
    if len(paths) != 1:
        return {"verified": False, "reason": "expected_one_snapshot",
                "snapshot_count": len(paths)}
    snapshot = paths[0]
    reference = workspace / REFERENCE_PATH
    if not reference.is_file() or reference.is_symlink():
        return {"verified": False, "reason": "reference_unavailable"}
    # Paths are positional parameters, never interpolated into shell source.
    process = subprocess.run(
        ["/bin/bash", "-c", 'source "$1"; exec python3 "$2"',
         "snapshot-contract", str(snapshot), str(reference)],
        capture_output=True, text=True, timeout=30,
    )
    try:
        payload = json.loads(process.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        payload = None
    return {
        "verified": process.returncode == 0 and isinstance(payload, dict)
                    and payload.get("passed") is True,
        "snapshot_path": snapshot.relative_to(home).as_posix(),
        "exit_code": process.returncode,
        "result": payload,
        "stderr": process.stderr[-2000:],
    }


def main():
    result = verify()
    print(json.dumps(result, sort_keys=True))
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
