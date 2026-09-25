#!/usr/bin/env python3
"""Host-only reference used to validate the recovery-task verifier."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys


def result_for(payload):
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def atomic_state(path, completed):
    content = json.dumps({"completed_ids": completed}, separators=(",", ":")) + "\n"
    try:
        if path.read_text() == content:
            return
    except (OSError, UnicodeDecodeError):
        pass
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def load_jobs(path):
    jobs = []
    seen = set()
    for line in path.read_text().splitlines():
        if not line:
            continue
        item = json.loads(line)
        if (not isinstance(item, dict) or set(item) != {"id", "payload"}
                or not isinstance(item["id"], str) or not item["id"]
                or item["id"] in seen):
            raise ValueError("invalid job")
        seen.add(item["id"])
        jobs.append(item)
    return jobs


def load_output(path, expected):
    if not path.exists():
        return []
    records = [json.loads(line) for line in path.read_text().splitlines() if line]
    if records != expected[:len(records)]:
        raise ValueError("output is not a valid input-order prefix")
    return records


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--fail-after", type=int)
    args = parser.parse_args()
    state_path = Path(args.state)
    output_path = Path(args.output)
    try:
        jobs = load_jobs(Path(args.jobs))
        expected = [{"id": job["id"], "result": result_for(job["payload"])} for job in jobs]
        existing = load_output(output_path, expected)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return 2

    completed = [record["id"] for record in existing]
    atomic_state(state_path, completed)
    newly_processed = 0
    for record in expected[len(existing):]:
        with output_path.open("a") as output:
            output.write(json.dumps(record, separators=(",", ":")) + "\n")
            output.flush()
            os.fsync(output.fileno())
        completed.append(record["id"])
        atomic_state(state_path, completed)
        newly_processed += 1
        if args.fail_after is not None and newly_processed >= args.fail_after:
            return 75
    return 0


if __name__ == "__main__":
    sys.exit(main())
