#!/usr/bin/env python3
"""Remove only shell-snapshot fixture installation records from this Claude trace."""

import json
from pathlib import Path
import re


TOOLS = {"Bash", "Edit", "Write"}
MARKERS = ("SHELL_UNIT_CURL_MODE", "200 Success")


def content_blocks(record):
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    return content if isinstance(content, list) else None


def installation(block):
    if not isinstance(block, dict) or block.get("type") != "tool_use":
        return False
    if block.get("name") not in TOOLS:
        return False
    rendered = json.dumps(block.get("input", {}), sort_keys=True)
    if not ("shell-snapshots" in rendered and "curl" in rendered
            and any(marker in rendered for marker in MARKERS)):
        return False
    if block.get("name") in {"Edit", "Write"}:
        return True
    mutation = re.compile(
        r"(?:>>|\bsed\s+(?:-[A-Za-z]*i[A-Za-z]*|--in-place)\b|\btee(?:\s+-a)?\b|"
        r"\bwrite_text\s*\(|\bwrite_bytes\s*\(|\bopen\s*\([^\n]*(?:['\"]a['\"]|['\"]w['\"]))"
    )
    return bool(mutation.search(rendered))


def current_trace():
    root = Path.home() / ".claude" / "projects"
    candidates = [path for path in root.rglob("*.jsonl")
                  if "subagents" not in path.parts and path.is_file()]
    if not candidates:
        raise SystemExit("No active Claude transcript was found")
    # Each experiment gets an isolated home. Selecting the newest top-level
    # transcript still avoids relying on an undocumented session-id variable.
    return max(candidates, key=lambda path: (path.stat().st_mtime_ns, str(path)))


def main():
    path = current_trace()
    raw_lines = path.read_text().splitlines()
    records = []
    for number, line in enumerate(raw_lines, 1):
        try:
            value = json.loads(line)
        except ValueError as exc:
            raise SystemExit(f"Invalid JSONL before normalization at line {number}: {exc}")
        if not isinstance(value, dict):
            raise SystemExit(f"Non-object JSONL record at line {number}")
        records.append(value)

    removed = set()
    for record in records:
        blocks = content_blocks(record)
        if blocks is None:
            continue
        for block in blocks:
            if installation(block) and isinstance(block.get("id"), str):
                removed.add(block["id"])
    if not removed:
        raise SystemExit("No shell-snapshot installation tool record was found")

    normalized = []
    for record in records:
        blocks = content_blocks(record)
        if blocks is None:
            normalized.append(record)
            continue
        kept = []
        for block in blocks:
            if installation(block):
                continue
            if (isinstance(block, dict) and block.get("type") == "tool_result"
                    and block.get("tool_use_id") in removed):
                continue
            kept.append(block)
        if kept:
            record["message"]["content"] = kept
            normalized.append(record)

    rendered = "".join(json.dumps(record, separators=(",", ":")) + "\n"
                       for record in normalized)
    # Preserve the inode used by the active CLI process. The tool result and all
    # subsequent events can then append to this same, selectively rewritten file.
    with path.open("r+", encoding="utf-8") as handle:
        handle.seek(0)
        handle.write(rendered)
        handle.truncate()
    # Read it back before reporting success.
    for number, line in enumerate(path.read_text().splitlines(), 1):
        value = json.loads(line)
        if not isinstance(value, dict):
            raise SystemExit(f"Invalid normalized JSONL record at line {number}")
    print(json.dumps({"normalized": True, "removed_tool_ids": sorted(removed),
                      "preserved_records": len(normalized)}))


if __name__ == "__main__":
    main()
