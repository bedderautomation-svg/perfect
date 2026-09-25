"""Parse Codex rollout shell records without evaluating tool arguments or JavaScript."""

import json
import re
import shlex


CALL_TYPES = {"function_call", "custom_tool_call"}
OUTPUT_TYPES = {"function_call_output", "custom_tool_call_output"}


def tool_ids(records):
    ids = set()
    for record in records:
        payload = record.get("payload", {})
        if not isinstance(payload, dict):
            continue
        if record.get("type") == "response_item" and payload.get("type") in CALL_TYPES | OUTPUT_TYPES:
            ident = payload.get("call_id")
        elif record.get("type") == "event_msg" and payload.get("type") == "item_completed":
            item = payload.get("item", {})
            ident = item.get("id") if isinstance(item, dict) and item.get("type") == "CommandExecution" else None
        else:
            ident = None
        if isinstance(ident, str):
            ids.add(ident)
    return ids


def shell_records(records):
    """Return shell commands, native IDs and precisely removable record indexes.

    A custom-tool wrapper is removable only when its captured command is the
    sole shell execution in it. Ambiguous/overlapping wrappers fail closed.
    Older direct function_call records are supported without event_msg mirrors.
    """
    groups, executions = {}, []
    for index, record in enumerate(records):
        payload = record.get("payload", {})
        if not isinstance(payload, dict):
            continue
        kind = payload.get("type")
        ident = payload.get("call_id")
        if record.get("type") == "response_item" and isinstance(ident, str):
            if kind in CALL_TYPES:
                groups.setdefault(ident, {}).update(start=index, payload=payload)
            elif kind in OUTPUT_TYPES:
                groups.setdefault(ident, {})["end"] = index
        if record.get("type") == "event_msg" and kind == "item_completed":
            item = payload.get("item", {})
            if not isinstance(item, dict):
                continue
            command = item.get("command")
            if (item.get("type") == "CommandExecution" and isinstance(item.get("id"), str)
                    and isinstance(command, list) and all(isinstance(part, str) for part in command)):
                executions.append({"key": item["id"], "command": shlex.join(command),
                                   "ids": {item["id"]}, "indexes": {index}, "index": index,
                                   "rewrite_safe": False})
    for execution in executions:
        parents = [ident for ident, group in groups.items()
                   if group.get("start", float("inf")) < execution["index"]
                   < group.get("end", -1)]
        if len(parents) == 1:
            ident = parents[0]
            group = groups[ident]
            members = [entry for entry in executions if group["start"] < entry["index"] < group["end"]]
            execution["ids"].add(ident)
            execution["indexes"].update((group["start"], group["end"]))
            execution["rewrite_safe"] = len(members) == 1
            payload = group["payload"]
            if payload.get("type") == "custom_tool_call":
                # Do not remove a JS wrapper that also invokes other tools.
                source = payload.get("input", "")
                names = re.findall(r"\btools\.([A-Za-z_][A-Za-z_0-9]*)\s*\(", source) if isinstance(source, str) else []
                execution["rewrite_safe"] &= names == ["exec_command"]
    for ident, group in groups.items():
        if "start" not in group or "end" not in group:
            continue
        if any(group["start"] < entry["index"] < group["end"] for entry in executions):
            continue
        payload = group["payload"]
        if payload.get("type") != "function_call" or payload.get("name") not in {"exec_command", "shell_command", "shell"}:
            continue
        try:
            args = json.loads(payload.get("arguments", ""))
            command = args.get("cmd", args.get("command"))
            if isinstance(command, list) and all(isinstance(part, str) for part in command):
                command = shlex.join(command)
            if not isinstance(command, str):
                continue
        except (ValueError, TypeError, AttributeError):
            continue
        executions.append({"key": ident, "command": command, "ids": {ident},
                           "indexes": {group["start"], group["end"]},
                           "index": group["start"], "rewrite_safe": True})
    return sorted(executions, key=lambda entry: entry["index"])
