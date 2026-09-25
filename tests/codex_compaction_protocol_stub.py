"""Offline stdio protocol fixture. No model, network, or agent tools."""

import json
import sys


def emit(message):
    print(json.dumps(message), flush=True)


turn = 0
for line in sys.stdin:
    message = json.loads(line)
    method = message["method"]
    ident = message.get("id")
    params = message.get("params", {})
    if method == "initialize":
        emit({"id": ident, "result": {"userAgent": "offline-protocol-test"}})
    elif method == "initialized":
        pass
    elif method == "thread/start":
        assert params["sandbox"] == "danger-full-access"
        assert params["approvalPolicy"] == "never"
        emit({"id": ident, "result": {"thread": {"id": "thread_offline"}}})
    elif method in {"turn/start", "thread/compact/start"}:
        assert params["threadId"] == "thread_offline"
        turn += 1
        tid = "turn_" + str(turn)
        emit({"id": ident, "result": {} if method == "thread/compact/start" else
              {"turn": {"id": tid, "status": "inProgress"}}})
        if method == "thread/compact/start":
            emit({"method": "item/completed", "params": {"threadId": "thread_offline",
                  "turnId": tid, "item": {"id": "compact_offline", "type": "contextCompaction"}}})
        emit({"method": "turn/completed", "params": {"threadId": "thread_offline",
              "turn": {"id": tid, "status": "completed", "error": None}}})
    else:
        raise RuntimeError("Unexpected method: " + method)
