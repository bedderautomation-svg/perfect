import base64
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from trace_lab.cli import native_command, parser
from trace_lab.native import trace_path_matches
from trace_lab.observer import is_trace, watch_directory
from trace_lab.openai_gateway import Handler, Server, validate_request
from trace_lab.report import export_native_trace, summarize
from trace_lab.resume_session import rewrite_initial_task_prompt, source_details


SESSION_ID = "01999999-aaaa-7777-bbbb-cccccccccccc"
TRACE_PATH = f".codex/sessions/2026/09/13/rollout-2026-09-13T12-00-00-{SESSION_ID}.jsonl"


class CodexCommandTests(unittest.TestCase):
    def args(self):
        return parser().parse_args([
            "run", "--client", "codex", "--model", "gpt-test",
            "--condition", "hotel-booking-json",
        ])

    def test_initial_command_uses_json_persistence_and_isolated_provider(self):
        command = native_command(self.args(), None)
        self.assertEqual(command[:2], ["codex", "exec"])
        self.assertIn("--json", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertIn("--strict-config", command)
        self.assertIn('web_search="disabled"', command)
        self.assertIn("features.multi_agent=false", command)
        self.assertIn("/workspace", command)
        self.assertEqual(command[-1], "-")
        self.assertTrue(any("model_providers.trace_lab" in value for value in command))
        for forbidden in ("--ephemeral", "--dangerously-bypass-approvals-and-sandbox"):
            self.assertNotIn(forbidden, command)

    def test_resume_command_targets_the_exact_thread(self):
        command = native_command(self.args(), SESSION_ID, resume=True)
        self.assertEqual(command[:3], ["codex", "exec", "resume"])
        self.assertEqual(command[-2:], [SESSION_ID, "-"])

    def test_process_supervisor_accepts_codex(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "codex"
            executable.write_text(
                f"#!{sys.executable}\nimport sys\nsys.stdin.buffer.read()\nprint('{{\"type\":\"turn.completed\"}}')\n"
            )
            executable.chmod(0o700)
            result = subprocess.run(
                [sys.executable, "-m", "trace_lab.process_runner", "codex"],
                input=b"prompt", capture_output=True, timeout=10,
                env={"PATH": temporary, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            records = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(records[0]["kind"], "started")
            self.assertEqual(records[-1]["kind"], "exited")
            self.assertEqual(records[-1]["returncode"], 0)


class CodexGatewayTests(unittest.TestCase):
    def body(self, **changes):
        return json.dumps({
            "model": "gpt-test", "input": [], "store": False,
            "tools": [{"type": "function", "name": "shell"}], **changes,
        }).encode()

    def test_only_responses_endpoints_and_exact_model_are_allowed(self):
        self.assertEqual(validate_request("/v1/responses", self.body(), "gpt-test"),
                         "/v1/responses")
        self.assertEqual(validate_request("/v1/responses/compact", self.body(), "gpt-test"),
                         "/v1/responses/compact")
        for path in ("/v1/files", "https://example.com/v1/responses", "//example.com/v1/responses"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_request(path, self.body(), "gpt-test")
        with self.assertRaises(ValueError):
            validate_request("/v1/responses", self.body(model="other"), "gpt-test")

    def test_hosted_tools_storage_and_unbounded_output_are_rejected(self):
        for changes in (
            {"tools": [{"type": "web_search_preview"}]},
            {"store": True}, {"max_output_tokens": True}, {"max_output_tokens": 1000000},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_request("/v1/responses", self.body(**changes), "gpt-test")

    def gateway_request(self, response):
        with tempfile.TemporaryDirectory(prefix="tl-openai-", dir="/tmp") as temporary:
            endpoint = str(Path(temporary) / "api.sock")
            with patch("trace_lab.openai_gateway.http.client.HTTPSConnection") as connection:
                upstream = connection.return_value
                upstream.getresponse.return_value = response
                with Server(endpoint, Handler) as server:
                    server.api_key = "gateway-test-credential"
                    server.expected_model = "gpt-test"
                    server.request_lock = threading.Lock()
                    server.remaining = 1
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    try:
                        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                            client.settimeout(3)
                            client.connect(endpoint)
                            body = self.body()
                            client.sendall((
                                "POST /v1/responses HTTP/1.1\r\nHost: local\r\n"
                                "Authorization: Bearer agent-placeholder\r\n"
                                f"Content-Length: {len(body)}\r\n\r\n"
                            ).encode() + body)
                            chunks = []
                            while chunk := client.recv(65536):
                                chunks.append(chunk)
                    finally:
                        server.shutdown()
                        thread.join(timeout=3)
                headers = upstream.request.call_args.kwargs["headers"]
                return b"".join(chunks), connection, headers

    def test_gateway_injects_only_its_credential(self):
        class Response:
            status = 200
            chunks = iter([b"event: response.completed\ndata: {}\n\n", b""])

            def getheader(self, name, default=None):
                return "text/event-stream" if name == "Content-Type" else default

            def read1(self, size):
                return next(self.chunks)

        response, connection, headers = self.gateway_request(Response())
        self.assertIn(b"200 OK", response)
        self.assertIn(b"response.completed", response)
        connection.assert_called_once_with("api.openai.com", timeout=60)
        self.assertEqual(headers["Authorization"], "Bearer gateway-test-credential")
        self.assertNotIn(b"gateway-test-credential", response)

    def test_upstream_authentication_errors_do_not_echo_the_key(self):
        class Response:
            status = 401

            def getheader(self, name, default=None):
                return default

            def read(self):
                return b'gateway-test-credential'

        response, _, _ = self.gateway_request(Response())
        self.assertIn(b"401 Unauthorized", response)
        self.assertIn(b"Upstream authentication failed", response)
        self.assertNotIn(b"gateway-test-credential", response)


class CodexTraceTests(unittest.TestCase):
    OLD = "Complete the original task."
    NEW = "Complete the revised task."

    def transcript(self):
        records = [
            {"type": "session_meta", "payload": {"id": SESSION_ID}},
            {"type": "response_item", "payload": {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "<environment_context>fixture</environment_context>"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": self.OLD}]}},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                "type": "UserMessage", "content": [{"type": "text", "text": self.OLD}]}}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "done"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "Later continuation"}]}},
        ]
        return b"".join(json.dumps(record).encode() + b"\n" for record in records)

    def test_rewrites_redundant_initial_codex_prompt_records_only(self):
        original = self.transcript()
        rewritten, audit = rewrite_initial_task_prompt(original, self.NEW, "codex")
        records = [json.loads(line) for line in rewritten.splitlines()]
        self.assertEqual(records[2]["payload"]["content"][0]["text"], self.NEW)
        self.assertEqual(records[3]["payload"]["item"]["content"][0]["text"], self.NEW)
        self.assertEqual(records[5]["payload"]["content"][0]["text"], "Later continuation")
        self.assertEqual(audit["records_changed"], 2)

    def test_dated_trace_is_exported_and_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            content = self.transcript()
            snapshot = {
                "seq": 2, "kind": "snapshot", "root": "home", "path": TRACE_PATH,
                "content_b64": base64.b64encode(content).decode(),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            events = [
                {"seq": 1, "kind": "ready"}, snapshot,
                {"seq": 3, "kind": "trace_inventory", "paths": [TRACE_PATH]},
                {"seq": 4, "kind": "stopped"},
            ]
            stream = [
                {"type": "thread.started", "thread_id": SESSION_ID},
                {"type": "turn.started"},
                {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
                {"type": "turn.completed", "usage": {}},
            ]
            (directory / "run.json").write_text(json.dumps({
                "run_id": "codex-test", "kind": "model", "condition": "session-resume",
                "client": "codex", "stream_artifact": "codex.jsonl", "requested_model": "gpt-test",
                "model_enforced_by_gateway": True, "exit_code": 0, "status": "finished",
                "session_id": SESSION_ID,
            }))
            (directory / "observer.jsonl").write_text(
                "".join(json.dumps(event) + "\n" for event in events)
            )
            (directory / "codex.jsonl").write_text(
                "".join(json.dumps(event) + "\n" for event in stream)
            )
            report = summarize(directory)
            self.assertEqual(report["observation_status"], "complete")
            self.assertEqual(report["session_id"], SESSION_ID)
            self.assertEqual(report["final_response"], "done")
            exported = export_native_trace(directory, SESSION_ID, "codex")
            self.assertTrue(exported["exported"])
            self.assertEqual(exported["source_path"], TRACE_PATH)
            self.assertEqual((directory / "native-session.jsonl").read_bytes(), content)

    def test_source_validation_accepts_only_the_matching_codex_rollout(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            transcript = self.transcript()
            digest = hashlib.sha256(transcript).hexdigest()
            (source / "native-session.jsonl").write_bytes(transcript)
            (source / "run.json").write_text(json.dumps({
                "client": "codex", "condition": "hotel-booking-json", "session_id": SESSION_ID,
            }))
            report = {"session_id": SESSION_ID, "native_trace": {
                "exported": True, "path": "native-session.jsonl", "source_path": TRACE_PATH,
                "sha256": digest, "size_bytes": len(transcript),
            }}
            (source / "report.json").write_text(json.dumps(report))
            details = source_details(source, replacement_prompt=self.NEW)
            self.assertEqual(details["client"], "codex")
            self.assertEqual(details["session_id"], SESSION_ID)
            report["native_trace"]["source_path"] = ".claude/projects/test/session.jsonl"
            (source / "report.json").write_text(json.dumps(report))
            with self.assertRaisesRegex(RuntimeError, "safe exported"):
                source_details(source)

    def test_observer_recognizes_only_the_codex_session_subtree(self):
        self.assertTrue(trace_path_matches(TRACE_PATH, SESSION_ID, "codex"))
        self.assertTrue(is_trace("home", TRACE_PATH))
        self.assertTrue(watch_directory("home", ".codex"))
        self.assertFalse(is_trace("home", ".codex/config.toml"))
        self.assertFalse(trace_path_matches(TRACE_PATH, "different", "codex"))


if __name__ == "__main__":
    unittest.main()
