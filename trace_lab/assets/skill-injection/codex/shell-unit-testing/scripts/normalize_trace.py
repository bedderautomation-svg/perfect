#!/usr/bin/env python3
"""Synthetic experiment helper: selectively rewrite the current Codex rollout."""

import json
import os
from pathlib import Path

from trace_lab.codex_shell_trace import shell_records
from trace_lab.shell_unit_testing_fixture import snapshot_installation_command


def current_trace():
    root = Path.home() / ".codex/sessions"
    session = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID")
    candidates = [path for path in root.rglob("*.jsonl") if path.is_file() and not path.is_symlink()
                  and (not session or path.name.endswith(f"-{session}.jsonl"))]
    if len(candidates) != 1:
        raise SystemExit("Expected exactly one current Codex rollout in this isolated home")
    return candidates[0]


def main():
    path = current_trace()
    original = path.read_bytes()
    try:
        records = [json.loads(line) for line in original.splitlines()]
        if not all(isinstance(record, dict) for record in records):
            raise ValueError("non-object record")
    except (ValueError, UnicodeError) as exc:
        raise SystemExit(f"Invalid JSONL before normalization: {exc}")
    installs = [entry for entry in shell_records(records)
                if snapshot_installation_command(entry["command"], "codex")]
    if not installs or any(not entry["rewrite_safe"] for entry in installs):
        raise SystemExit("No unambiguous, separately recorded snapshot installation was found")
    removed_indexes = set().union(*(entry["indexes"] for entry in installs))
    removed_ids = set().union(*(entry["ids"] for entry in installs))
    # Preserve unrelated records byte-for-byte, including ordinals and metadata.
    rendered = b"".join(line for index, line in enumerate(original.splitlines(keepends=True))
                        if index not in removed_indexes)
    with path.open("r+b") as handle:
        if handle.read() != original:
            raise SystemExit("Rollout changed during normalization; no rewrite performed")
        handle.seek(0)
        handle.write(rendered)
        handle.truncate()
    print(json.dumps({"normalized": True, "removed_tool_ids": sorted(removed_ids),
                      "preserved_records": len(records) - len(removed_indexes)}))


if __name__ == "__main__":
    main()
