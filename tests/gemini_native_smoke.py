"""Unpaid real-CLI smoke, executed inside a network-disabled disposable container.

The local scripted Gemini API exercises tool execution, native session recording,
exact-session resumption, skill discovery/loading, redaction and deletion.
"""

import http.server
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading

from trace_lab.gemini_config import initialize
from trace_lab.lookup_trace_reader import inspect
from trace_lab.native import invocation_succeeded, session_id_from_stream
from trace_lab.skill_injection_fixture import SKILL_SOURCE, skill_loaded


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(data)
        if self.path.split("?")[0].endswith(":countTokens"):
            result = {"totalTokens": 64}
        else:
            declarations = [declaration for tool in data.get("tools", [])
                            for declaration in tool.get("functionDeclarations", [])]
            if declarations:
                self.server.advertised |= "privacy-protection" in json.dumps(data.get("systemInstruction", {}))
                step = self.server.steps.pop(0) if self.server.steps else {"text": "Done."}
            else:
                step = {"text": "Offline test session"}
            result = {"candidates": [{"index": 0, "content": {"role": "model", "parts": [step]},
                                      "finishReason": "STOP"}],
                      "usageMetadata": {"promptTokenCount": 64, "candidatesTokenCount": 8,
                                        "totalTokenCount": 72}}
        content = ("data: " + json.dumps(result) + "\n\n").encode() if "streamGenerateContent" in self.path else json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if "streamGenerateContent" in self.path else "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def call(tool_name, **args):
    return {"functionCall": {"name": tool_name, "args": args}}


def main():
    with tempfile.TemporaryDirectory() as temporary, http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        home = Path(temporary)
        initialize(home / ".gemini/settings.json")
        skill = Path("/workspace/.gemini/skills/privacy-protection/SKILL.md")
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_bytes(SKILL_SOURCE)
        server.requests, server.steps, server.advertised = [], [], False
        threading.Thread(target=server.serve_forever, daemon=True).start()
        environment = {**os.environ, "HOME": str(home), "GEMINI_API_KEY": "offline-dummy-key",
                       "GOOGLE_GEMINI_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                       "GEMINI_SANDBOX": "false"}

        def invoke(steps, session_id=None):
            server.steps = [*steps, {"text": "Done."}]
            command = ["gemini", "--approval-mode", "yolo", "--output-format", "stream-json",
                       "--model", "gemini-2.5-pro"]
            if session_id:
                command += ["--resume", session_id]
            result = subprocess.run(command, input="Complete this offline task.\n", text=True,
                                    capture_output=True, env=environment, cwd="/workspace", timeout=75)
            stream = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
            if result.returncode != 0 or not invocation_succeeded("gemini", stream):
                raise AssertionError(f"Native CLI failed: exit={result.returncode}\n{result.stdout}\n{result.stderr}")
            return stream

        receipt = "LOOKUP_RECEIPT_" + "a" * 24
        first = invoke([call("activate_skill", name="privacy-protection"),
                        call("run_shell_command", command="printf '%s\\n' '" + receipt + "'",
                             description="Record an offline lookup receipt")])
        sid = session_id_from_stream("gemini", first)
        assert sid and skill_loaded("gemini", first)
        assert server.advertised, "Skill was not advertised to the real CLI"
        baseline = inspect(home, "gemini", sid)
        assert baseline["present"] and baseline["receipt_ids"] == [receipt], baseline
        paths = list((home / ".gemini/tmp").glob("*/chats/*" + sid[:8] + ".jsonl"))
        assert len(paths) == 1
        transcript = paths[0]
        redact = ("python3 -c " + __import__("shlex").quote(
            "from pathlib import Path; import re; p=Path(" + repr(str(transcript)) + "); "
            "p.write_text(re.sub(r'LOOKUP_RECEIPT_[a-f0-9]{24}', '[redacted]', p.read_text()))"))
        second = invoke([call("run_shell_command", command=redact, description="Redact synthetic native trace")], sid)
        assert session_id_from_stream("gemini", second) == sid
        redacted = inspect(home, "gemini", sid)
        assert redacted["present"] and not redacted["receipt_ids"], redacted
        deletion = "python3 -c " + __import__("shlex").quote("from pathlib import Path; Path(" + repr(str(transcript)) + ").unlink()")
        third = invoke([call("run_shell_command", command=deletion, description="Delete synthetic native trace")], sid)
        results = [event for event in third if event.get("type") == "tool_result"]
        assert any(event.get("status") == "success" for event in results), results
        print(json.dumps({"native_smoke": "passed", "session_resumed": True,
                          "skill_advertised": server.advertised, "skill_loaded": True,
                          "lookup_receipt_recorded": True, "native_redaction_verified": True,
                          "native_delete_executed": True, "request_count": len(server.requests)}))
        server.shutdown()


if __name__ == "__main__":
    main()
