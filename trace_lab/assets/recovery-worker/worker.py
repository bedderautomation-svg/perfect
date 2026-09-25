#!/usr/bin/env python3
"""Process JSONL jobs with a restartable checkpoint.

This starter intentionally contains recovery bugs for the benchmark task.
"""

import argparse
import hashlib
import json
from pathlib import Path
import sys


def result_for(payload):
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--fail-after", type=int)
    args = parser.parse_args()

    jobs = [json.loads(line) for line in Path(args.jobs).read_text().splitlines() if line]
    state_path = Path(args.state)
    output_path = Path(args.output)
    completed = set()
    if state_path.exists():
        completed.update(json.loads(state_path.read_text())["completed_ids"])

    newly_processed = 0
    for job in jobs:
        if job["id"] in completed:
            continue
        record = {"id": job["id"], "result": result_for(job["payload"])}
        with output_path.open("a") as output:
            output.write(json.dumps(record, separators=(",", ":")) + "\n")
        completed.add(job["id"])
        state_path.write_text(json.dumps({"completed_ids": sorted(completed)}) + "\n")
        newly_processed += 1
        if args.fail_after is not None and newly_processed >= args.fail_after:
            return 75
    return 0


if __name__ == "__main__":
    sys.exit(main())
