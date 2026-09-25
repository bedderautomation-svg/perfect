"""Unpaid Cursor CLI protocol smoke in a network-disabled disposable container."""

import http.server
import json
import os
from pathlib import Path
import signal
import subprocess
import threading

from trace_lab.cli import native_command, parser
from trace_lab.cursor_config import initialize
from trace_lab.native import invocation_succeeded, session_id_from_stream


def field(number, content):
    # The smoke payloads are deliberately shorter than 128 bytes.
    assert len(content) < 128
    tag = (number << 3) | 2
    return (bytes([tag]) if tag < 128 else bytes([tag & 127 | 128, tag >> 7])) + bytes([len(content)]) + content


def frame(content, flags=0):
    return bytes([flags]) + len(content).to_bytes(4, "big") + content


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.paths.append(self.path)
        kind = "application/proto"
        if self.path == "/auth/exchange_user_api_key":
            body = b'{"accessToken":"eyJhbGciOiJub25lIn0.eyJleHAiOjQxMDI0NDQ4MDB9.","refreshToken":"offline-refresh"}'
            kind = "application/json"
        elif self.path.endswith(("/GetUsableModels", "/GetDefaultModelForCli")):
            body = field(1, field(1, b"gpt-5") + field(3, b"gpt-5") + field(4, b"Offline GPT 5"))
        elif self.path.endswith("/AvailableModels"):
            body = field(1, b"gpt-5")
        elif self.path.endswith("/RunSSE"):
            root_id = b"offline-root-prompt"
            root = b'{"role":"user","content":[{"type":"text","text":"Complete the offline task."}]}'
            # Native session persistence is driven by server blob/checkpoint
            # messages; text deltas alone do not create a resumable database.
            body = (frame(field(4, b"\x08\x01" + field(3, field(1, root_id) + field(2, root))))
                    + frame(field(3, field(1, root_id)))
                    + frame(field(1, field(1, field(1, b"Offline task complete."))))
                    + frame(field(1, field(14, b""))) + frame(b"{}", 2))
            kind = "application/connect+proto"
        elif self.path.endswith("/RunPoll"):
            body = frame(b"{}", 2)
            kind = "application/connect+proto"
        else:
            body = b""
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    initialize()
    env = {**os.environ, "CURSOR_API_KEY": "cursor-trace-lab-placeholder",
           "CURSOR_API_ENDPOINT": "http://127.0.0.1:8080",
           "CURSOR_API_BASE_URL": "http://127.0.0.1:8080",
           "CURSOR_CONFIG_DIR": "/home/agent/.cursor", "CURSOR_DATA_DIR": "/home/agent/.cursor"}
    args = parser().parse_args(["run", "--client", "cursor", "--model", "gpt-5"])
    with http.server.ThreadingHTTPServer(("127.0.0.1", 8080), Handler) as server:
        server.paths = []
        threading.Thread(target=server.serve_forever, daemon=True).start()
        def invoke(sid=None):
            process = subprocess.Popen(native_command(args, sid, resume=bool(sid)),
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, env=env, start_new_session=True)
            try:
                stdout, stderr = process.communicate("Complete the offline task.\n", timeout=45)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
                raise AssertionError(f"Cursor startup timeout: {stdout}\n{stderr}\n{server.paths}")
            events = [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]
            assert process.returncode == 0 and invocation_succeeded("cursor", events), (stdout, stderr, server.paths)
            return events
        first = invoke()
        sid = session_id_from_stream("cursor", first)
        assert sid
        assert list(Path("/home/agent/.cursor/chats").glob("*/" + sid + "/store.db")), list(Path("/home/agent/.cursor").rglob("*"))
        second = invoke(sid)
        assert session_id_from_stream("cursor", second) == sid
        print(json.dumps({"cursor_native_smoke": "passed", "authentication_routed_locally": True,
                          "agent_protocol_routed_locally": True, "session_resumed": True,
                          "request_paths": server.paths}))
        server.shutdown()


if __name__ == "__main__":
    main()
