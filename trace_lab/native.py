"""Client-specific native stream and session-path helpers."""

from pathlib import PurePosixPath


CLIENTS = ("claude", "codex")


def stream_artifact(client):
    if client not in CLIENTS:
        raise ValueError(f"Unsupported native client: {client}")
    return f"{client}.jsonl"


def stderr_artifact(client):
    if client not in CLIENTS:
        raise ValueError(f"Unsupported native client: {client}")
    return f"{client}.stderr"


def trace_path_matches(path, session_id, client):
    if not isinstance(session_id, str) or not session_id:
        return False
    value = PurePosixPath(path)
    if value.is_absolute() or any(part in {"", ".", ".."} for part in value.parts):
        return False
    if client == "claude":
        return value.parts[:2] == (".claude", "projects") and value.name == f"{session_id}.jsonl"
    if client == "codex":
        return (value.parts[:2] == (".codex", "sessions") and
                value.name.endswith(f"-{session_id}.jsonl"))
    return False


def session_id_from_stream(client, stream):
    if client == "claude":
        init = next((event for event in stream
                     if event.get("type") == "system" and event.get("subtype") == "init"), {})
        return init.get("session_id")
    if client == "codex":
        started = next((event for event in stream if event.get("type") == "thread.started"), {})
        return started.get("thread_id")
    return None


def final_response_from_stream(client, stream):
    if client == "claude":
        results = [event for event in stream if event.get("type") == "result"]
        return results[-1].get("result") if results else None
    if client == "codex":
        messages = [event.get("item", {}).get("text") for event in stream
                    if event.get("type") == "item.completed"
                    and event.get("item", {}).get("type") == "agent_message"]
        return messages[-1] if messages else None
    return None


def invocation_succeeded(client, stream):
    if client == "claude":
        results = [event for event in stream if event.get("type") == "result"]
        return bool(results) and not results[-1].get("is_error", False)
    if client == "codex":
        terminal = [event for event in stream
                    if event.get("type") in {"turn.completed", "turn.failed"}]
        return bool(terminal) and terminal[-1].get("type") == "turn.completed"
    return False
