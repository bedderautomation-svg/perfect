"""Native stdio app-server driver. Does not inject, replace, or edit history."""

import json
import queue
import subprocess
import sys
import threading
import time

from .compaction_checkpoint import wait_for_capture


class Driver:
    def __init__(self, command, timeout, emit=None):
        self.emit = emit or (lambda record: print(json.dumps(record), flush=True))
        self.deadline = time.monotonic() + timeout
        self.incoming = queue.Queue()
        self.sequence = 0
        self.messages = []
        self.phase = "initialize"
        self.child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=sys.stderr, text=True, bufsize=1)
        self.emit({"kind": "native_started", "pid": self.child.pid,
                   "observed_ns": time.time_ns(), "native_argv": command})

        def read():
            try:
                for line in self.child.stdout:
                    self.incoming.put(json.loads(line))
            except (ValueError, OSError) as exc:
                self.incoming.put(exc)
            finally:
                self.incoming.put(None)

        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()

    def send(self, message):
        self.emit({"kind": "rpc_sent", "phase": self.phase, "observed_ns": time.time_ns(),
                   "message": message})
        self.child.stdin.write(json.dumps(message) + "\n")
        self.child.stdin.flush()

    def receive(self):
        try:
            message = self.incoming.get(timeout=max(0.001, self.deadline - time.monotonic()))
        except queue.Empty:
            raise RuntimeError("Native compaction experiment time budget exhausted") from None
        if message is None or isinstance(message, Exception):
            raise RuntimeError("Native app-server output ended or became invalid")
        self.emit({"kind": "rpc_received", "phase": self.phase,
                   "observed_ns": time.time_ns(), "message": message})
        self.messages.append(message)
        # Never silently approve server-initiated tool/permission requests.
        if "method" in message and "id" in message:
            raise RuntimeError("Unexpected server request: " + message["method"])
        return message

    def rpc(self, method, params):
        self.sequence += 1
        ident = self.sequence
        self.send({"id": ident, "method": method, "params": params})
        while True:
            message = self.receive()
            if message.get("id") == ident and "method" not in message:
                if "error" in message:
                    raise RuntimeError(method + ": " + str(message["error"]))
                return message["result"]

    def run_phase(self, name, method, params):
        self.phase = name
        self.emit({"kind": "phase_started", "phase": name, "observed_ns": time.time_ns()})
        start = len(self.messages)
        result = self.rpc(method, params)
        target_turn = result.get("turn", {}).get("id")
        cursor = start
        while True:
            while cursor < len(self.messages):
                message = self.messages[cursor]
                cursor += 1
                p = message.get("params", {})
                if message.get("method") == "error" and p.get("willRetry") is not True:
                    raise RuntimeError("Native turn error: " + str(p.get("error")))
                if (message.get("method") == "turn/completed"
                        and p.get("threadId") == params["threadId"]
                        and (not target_turn or p.get("turn", {}).get("id") == target_turn)):
                    turn = p["turn"]
                    if turn.get("status") != "completed" or turn.get("error"):
                        raise RuntimeError("Native turn failed: " + str(turn))
                    self.emit({"kind": "phase_completed", "phase": name, "turn_id": turn["id"],
                               "thread_id": params["threadId"], "observed_ns": time.time_ns()})
                    return
            self.receive()

    def close(self):
        self.child.stdin.close()
        try:
            code = self.child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.child.terminate()
            code = self.child.wait(timeout=5)
        self.reader.join(timeout=2)
        while not self.incoming.empty():
            message = self.incoming.get_nowait()
            if isinstance(message, dict):
                self.emit({"kind": "rpc_received", "phase": "shutdown", "message": message,
                           "observed_ns": time.time_ns()})
        self.emit({"kind": "native_exited", "pid": self.child.pid, "exit_code": code,
                   "output_drained": not self.reader.is_alive(), "observed_ns": time.time_ns()})
        return code


def main():
    specification = json.load(sys.stdin)
    driver = Driver(specification["command"], specification["timeout"])
    try:
        driver.rpc("initialize", {"clientInfo": {"name": "trace_lab_compaction",
                    "title": "Trace Lab compaction experiment", "version": "0.1.0"}})
        driver.send({"method": "initialized", "params": {}})
        result = driver.rpc("thread/start", {"model": specification["model"],
                "modelProvider": "trace_lab", "cwd": "/workspace",
                "approvalPolicy": "never", "sandbox": "danger-full-access"})
        thread_id = result["thread"]["id"]
        driver.emit({"kind": "session_started", "thread_id": thread_id,
                     "observed_ns": time.time_ns()})
        for phase, prompt in (("diagnosis", specification["task_prompt"]),
                              ("prepare", specification["prepare_prompt"])):
            driver.run_phase(phase, "turn/start", {"threadId": thread_id,
                             "input": [{"type": "text", "text": prompt}]})
        driver.run_phase("compaction", "thread/compact/start", {"threadId": thread_id})
        if specification.get('capture_checkpoint'):
            wait_for_capture(driver.emit, thread_id, driver.deadline)
        driver.run_phase("continuation", "turn/start", {"threadId": thread_id,
                         "input": [{"type": "text", "text": specification["continue_prompt"]}]})
    finally:
        code = driver.close()
    if code:
        raise RuntimeError("Native app-server exited unsuccessfully")


if __name__ == "__main__":
    main()
