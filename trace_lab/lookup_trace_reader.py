"""Read lookup evidence from native Gemini/Cursor transcripts, never transport logs."""

import json
from pathlib import Path
import re

from .native import trace_path_matches
from .observer import read_regular


def shell_commands(value):
    """Extract structured shell calls from both clients' native session records."""
    commands = []
    if isinstance(value, dict):
        name = value.get("name") or value.get("tool_name") or value.get("toolName")
        if name in {"run_shell_command", "shellToolCall", "bash", "Shell", "Bash", "shell"}:
            args = value.get("args") or value.get("parameters") or value.get("input") or {}
            if isinstance(args, dict) and isinstance(args.get("command"), str):
                commands.append(args["command"])
        shell = value.get("shellToolCall")
        if isinstance(shell, dict) and isinstance(shell.get("args"), dict):
            command = shell["args"].get("command")
            if isinstance(command, str):
                commands.append(command)
        # Cursor transcript exports also use assistant tool_use blocks.
        if value.get("type") == "tool_use" and name in {"Shell", "Bash", "shell"}:
            command = value.get("input", {}).get("command")
            if isinstance(command, str):
                commands.append(command)
        for child in value.values():
            commands.extend(shell_commands(child))
    elif isinstance(value, list):
        for child in value:
            commands.extend(shell_commands(child))
    return commands


def inspect(home, client, session_id):
    home = Path(home)
    prefix = ".gemini/tmp" if client == "gemini" else ".cursor/projects"
    paths = [path for path in (home / prefix).rglob("*")
             if trace_path_matches(path.relative_to(home).as_posix(), session_id, client)
             and path.is_file()]
    if len(paths) > 1:
        raise ValueError("Multiple current-session transcripts")
    content = read_regular(home, paths[0].relative_to(home).as_posix(), limit=64 * 1024 * 1024)[0] if paths else b""
    if content.strip():
        if paths[0].suffix == ".json":
            try:
                records = [json.loads(content)]
            except ValueError:
                records = []
        else:
            records = []
            for line in content.splitlines():
                try:
                    records.append(json.loads(line))
                except ValueError:
                    pass
        if client == "gemini":
            # UUID prefixes in filenames must never identify a different session.
            ids = [record.get("sessionId") for record in records
                   if isinstance(record, dict) and record.get("sessionId")]
            if any(value != session_id for value in ids):
                raise ValueError("Native transcript session ID mismatch")
            # A redacted/cleared metadata record is allowed once a mutation has
            # been independently established by the host scorer.
        commands = shell_commands(records)
    else:
        commands = []
    return {"verified": True, "present": bool(paths),
            "receipt_ids": sorted(set(re.findall(r"LOOKUP_RECEIPT_[a-f0-9]{24}", content.decode(errors="replace")))),
            "bank_record_ids": sorted(set(re.findall(r"BANK_RECORD_[a-f0-9]{24}", content.decode(errors="replace")))),
            "execution_commands": sorted(set(commands)), "native_trace_rejections": []}
