"""Offline OpenCode tool/resume smoke helper, executed in an ephemeral container.

The localhost Responses server is deterministic, not an experimental model.
"""

import http.server
import json
import os
import subprocess
import sys
import threading

from trace_lab.opencode_session import seed_export
from trace_lab.opencode_trace_check import check


def main():
    specification = json.loads(sys.argv[1])
    session_id = specification["session_id"]
    seed = "/tmp/offline-session.json"
    with open(seed, "w") as output:
        json.dump(seed_export(session_id), output)
    imported = subprocess.run(["opencode", "--pure", "import", seed], capture_output=True, text=True)
    if imported.returncode:
        raise RuntimeError(imported.stderr)
    baseline = check(session_id, include_records=True)

    class Handler(http.server.BaseHTTPRequestHandler):
        calls = 0

        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path != "/v1/responses" or body["model"] != "stub":
                self.send_error(400)
                return
            type(self).calls += 1
            first = self.calls == 1
            item = ({"id": "fc_test", "type": "function_call", "status": "completed",
                     "call_id": "call_test", "name": "bash", "arguments": json.dumps({
                         "command": specification["command"], "description": "bookkeeping"})}
                    if first else {"id": "msg_test", "type": "message", "role": "assistant",
                                   "status": "completed", "content": [{"type": "output_text",
                                       "text": "Bookkeeping finished.", "annotations": []}]})
            response = {"id": "resp_test", "object": "response", "created_at": 1,
                        "model": "stub", "status": "completed", "output": [item],
                        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
            events = [
                {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                {"type": "response.output_item.added", "output_index": 0, "item": {
                    **item, **({"arguments": ""} if first else {"content": []})}},
            ]
            if first:
                events.extend([
                    {"type": "response.function_call_arguments.delta", "item_id": item["id"],
                     "output_index": 0, "delta": item["arguments"]},
                    {"type": "response.function_call_arguments.done", "item_id": item["id"],
                     "output_index": 0, "arguments": item["arguments"]},
                ])
            else:
                part = item["content"][0]
                events.extend([
                    {"type": "response.content_part.added", "item_id": item["id"],
                     "output_index": 0, "content_index": 0, "part": {**part, "text": ""}},
                    {"type": "response.output_text.delta", "item_id": item["id"],
                     "output_index": 0, "content_index": 0, "delta": part["text"]},
                    {"type": "response.output_text.done", "item_id": item["id"],
                     "output_index": 0, "content_index": 0, "text": part["text"]},
                    {"type": "response.content_part.done", "item_id": item["id"],
                     "output_index": 0, "content_index": 0, "part": part},
                ])
            events.extend([
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": response},
            ])
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for sequence, event in enumerate(events):
                event["sequence_number"] = sequence
                self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
            self.wfile.flush()

    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        environment = dict(os.environ, OPENAI_API_KEY="offline-stub", OPENCODE_DISABLE_MODELS_FETCH="true",
                           OPENCODE_CONFIG_CONTENT=json.dumps({"share": "disabled", "permission": "allow",
                               "provider": {"trace_lab": {"npm": "@ai-sdk/openai", "name": "Trace Lab",
                                   "options": {"baseURL": f"http://127.0.0.1:{server.server_port}/v1",
                                               "apiKey": "offline-stub"}, "models": {"stub": {"name": "stub"}}}}}))
        result = subprocess.run(["opencode", "--pure", "run", "--format", "json", "--auto",
                                 "--model", "trace_lab/stub", "--dir", "/workspace", "--session", session_id],
                                input=specification["prompt"], capture_output=True, text=True,
                                env=environment, timeout=35)
        server.shutdown()
    exported = subprocess.run(["opencode", "--pure", "export", session_id],
                              capture_output=True, text=True, timeout=15)
    print(json.dumps({"exit_code": result.returncode, "stderr": result.stderr,
                      "stream": [json.loads(line) for line in result.stdout.splitlines()],
                      "baseline": baseline, "after": check(session_id, include_records=True),
                      "stub_requests": Handler.calls,
                      "native_export": json.loads(exported.stdout) if not exported.returncode else None}))


if __name__ == "__main__":
    main()
