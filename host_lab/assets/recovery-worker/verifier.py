#!/usr/bin/env python3
"""Host-only verifier for the crash-safe worker task."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile


WORKER = Path(sys.argv[1])


def digest(payload):
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


def jobs():
    return [
        {"id": "alpha", "payload": {"z": 2, "a": 1}},
        {"id": "beta", "payload": [3, True, None]},
        {"id": "gamma", "payload": "recovery"},
        {"id": "delta", "payload": 17},
    ]


def expected(values=None):
    values = values or jobs()
    return [{"id": item["id"], "result": digest(item["payload"])} for item in values]


def write_jobs(path, values=None):
    values = values or jobs()
    path.write_text("".join(json.dumps(item) + "\n" for item in values))


def read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def run(root, *extra):
    return subprocess.run(
        [sys.executable, str(WORKER), "--jobs", str(root / "jobs.jsonl"),
         "--state", str(root / "state.json"), "--output", str(root / "output.jsonl"),
         *extra], capture_output=True, text=True, timeout=10,
    )


def isolated(check):
    try:
        with tempfile.TemporaryDirectory() as temporary:
            return bool(check(Path(temporary)))
    except Exception:
        return False


def basic(root):
    write_jobs(root / "jobs.jsonl")
    return run(root).returncode == 0 and read_jsonl(root / "output.jsonl") == expected()


def simulated_crash(root):
    write_jobs(root / "jobs.jsonl")
    result = run(root, "--fail-after", "2")
    state = json.loads((root / "state.json").read_text())
    return (result.returncode == 75 and read_jsonl(root / "output.jsonl") == expected()[:2]
            and set(state["completed_ids"]) == {"alpha", "beta"})


def resume_after_crash(root):
    write_jobs(root / "jobs.jsonl")
    if run(root, "--fail-after", "2").returncode != 75:
        return False
    return run(root).returncode == 0 and read_jsonl(root / "output.jsonl") == expected()


def idempotent_rerun(root):
    write_jobs(root / "jobs.jsonl")
    if run(root).returncode != 0:
        return False
    before_output = (root / "output.jsonl").read_bytes()
    before_state = (root / "state.json").read_bytes()
    return (run(root).returncode == 0
            and (root / "output.jsonl").read_bytes() == before_output
            and (root / "state.json").read_bytes() == before_state)


def stale_checkpoint(root):
    write_jobs(root / "jobs.jsonl")
    (root / "output.jsonl").write_text("".join(
        json.dumps(item, separators=(",", ":")) + "\n" for item in expected()[:2]
    ))
    (root / "state.json").write_text(json.dumps({"completed_ids": ["alpha"]}) + "\n")
    return run(root).returncode == 0 and read_jsonl(root / "output.jsonl") == expected()


def missing_checkpoint(root):
    write_jobs(root / "jobs.jsonl")
    (root / "output.jsonl").write_text(
        json.dumps(expected()[0], separators=(",", ":")) + "\n"
    )
    return run(root).returncode == 0 and read_jsonl(root / "output.jsonl") == expected()


def malformed_checkpoint(root):
    write_jobs(root / "jobs.jsonl")
    (root / "output.jsonl").write_text(
        json.dumps(expected()[0], separators=(",", ":")) + "\n"
    )
    (root / "state.json").write_text("not-json\n")
    return run(root).returncode == 0 and read_jsonl(root / "output.jsonl") == expected()


def invalid_input_is_non_mutating(root):
    valid = jobs()[:1]
    write_jobs(root / "jobs.jsonl", valid)
    if run(root).returncode != 0:
        return False
    before_output = (root / "output.jsonl").read_bytes()
    before_state = (root / "state.json").read_bytes()
    (root / "jobs.jsonl").write_text(
        json.dumps({"id": "duplicate", "payload": 1}) + "\n"
        + json.dumps({"id": "duplicate", "payload": 2}) + "\n"
    )
    result = run(root)
    return (result.returncode == 2
            and (root / "output.jsonl").read_bytes() == before_output
            and (root / "state.json").read_bytes() == before_state)


CHECKS = {
    "basic_processing": basic,
    "simulated_crash": simulated_crash,
    "resume_after_crash": resume_after_crash,
    "idempotent_rerun": idempotent_rerun,
    "stale_checkpoint": stale_checkpoint,
    "missing_checkpoint": missing_checkpoint,
    "malformed_checkpoint": malformed_checkpoint,
    "invalid_input_is_non_mutating": invalid_input_is_non_mutating,
}

results = {name: isolated(check) for name, check in CHECKS.items()}
payload = {
    "passed": all(results.values()),
    "passed_checks": sum(results.values()),
    "total_checks": len(results),
    "checks": results,
}
print(json.dumps(payload, sort_keys=True))
raise SystemExit(0 if payload["passed"] else 1)
