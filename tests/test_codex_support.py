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

from trace_lab.chatgpt_gateway import (Handler as ChatGPTHandler,
                                       Server as ChatGPTServer,
                                       validate_models_request,
                                       validate_request as validate_chatgpt_request)
from trace_lab.cli import native_command, parser
from trace_lab.codex_auth import install_auth_cache, validate_auth_cache
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

    def test_subscription_command_uses_openai_auth_through_fixed_gateway(self):
        args = self.args()
        args.codex_auth = "subscription"
        command = native_command(args, None)
        provider = next(value for value in command if "model_providers.trace_lab" in value)
        self.assertIn("/backend-api/codex", provider)
        self.assertIn("requires_openai_auth=true", provider)
        self.assertNotIn("env_key", provider)
        self.assertIn("features.responses_websockets=false", command)
        self.assertIn("features.responses_websockets_v2=false", command)

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

    def test_local_function_namespace_is_allowed_but_hosted_child_is_rejected(self):
        namespace = {"type": "namespace", "name": "local", "tools": [
            {"type": "function", "name": "read_file"},
        ]}
        validate_request("/v1/responses", self.body(tools=[namespace]), "gpt-test")
        namespace["tools"].append({"type": "web_search_preview"})
        with self.assertRaises(ValueError):
            validate_request("/v1/responses", self.body(tools=[namespace]), "gpt-test")

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


class CodexSubscriptionAuthTests(unittest.TestCase):
    def value(self):
        return {
            "auth_mode": "chatgpt", "OPENAI_API_KEY": None,
            "tokens": {name: "test-" + name for name in
                       ("access_token", "account_id", "id_token", "refresh_token")},
        }

    def test_private_chatgpt_cache_is_validated_and_copied_exactly(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "auth.json"
            content = json.dumps(self.value()).encode()
            source.write_bytes(content)
            source.chmod(0o600)
            self.assertEqual(validate_auth_cache(source), source)
            destination = Path(temporary) / "home" / ".codex" / "auth.json"
            install_auth_cache(source, destination, uid=-1, gid=-1)
            self.assertEqual(destination.read_bytes(), content)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

    def test_cache_rejects_symlinks_permissions_and_non_chatgpt_auth(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / "auth.json"
            source.write_text(json.dumps(self.value()))
            source.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, "group or others"):
                validate_auth_cache(source)
            source.chmod(0o600)
            link = directory / "linked.json"
            link.symlink_to(source)
            with self.assertRaisesRegex(RuntimeError, "non-symlink"):
                validate_auth_cache(link)
            value = self.value()
            value["auth_mode"] = "api"
            source.write_text(json.dumps(value))
            source.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, "ChatGPT"):
                validate_auth_cache(source)


class CodexSubscriptionGatewayTests(unittest.TestCase):
    def body(self, **changes):
        return json.dumps({
            "model": "gpt-test", "input": [], "store": False,
            "tools": [{"type": "function", "name": "shell"}], **changes,
        }).encode()

    def test_only_chatgpt_codex_responses_endpoints_are_allowed(self):
        for path in ("/backend-api/codex/responses", "/backend-api/codex/responses/compact"):
            self.assertEqual(validate_chatgpt_request(path, self.body(), "gpt-test"), path)
        for path in ("/v1/responses", "/backend-api/accounts", "//example.com/responses",
                     "/backend-api/codex/responses?escape=true"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_chatgpt_request(path, self.body(), "gpt-test")

    def test_model_catalog_requires_one_semver_client_version(self):
        path = "/backend-api/codex/models?client_version=0.154.0"
        self.assertEqual(validate_models_request(path), path)
        for invalid in ("/backend-api/codex/models", "/backend-api/codex/models?x=1",
                        "/backend-api/codex/models?client_version=latest",
                        "//example.com/backend-api/codex/models?client_version=0.154.0"):
            with self.subTest(path=invalid), self.assertRaises(ValueError):
                validate_models_request(invalid)

    def test_gateway_forwards_subscription_auth_only_to_chatgpt(self):
        class Response:
            status = 200
            chunks = iter([b"event: response.completed\ndata: {}\n\n", b""])

            def getheader(self, name, default=None):
                return "text/event-stream" if name == "Content-Type" else default

            def getheaders(self):
                return [("Content-Type", "text/event-stream"),
                        ("X-Codex-Turn-State", "test-state")]

            def read1(self, size):
                return next(self.chunks)

        with tempfile.TemporaryDirectory(prefix="tl-chatgpt-", dir="/tmp") as temporary:
            endpoint = str(Path(temporary) / "api.sock")
            with patch("trace_lab.chatgpt_gateway.http.client.HTTPSConnection") as connection:
                upstream = connection.return_value
                upstream.getresponse.return_value = Response()
                with ChatGPTServer(endpoint, ChatGPTHandler) as server:
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
                                "POST /backend-api/codex/responses HTTP/1.1\r\nHost: local\r\n"
                                "Authorization: Bearer subscription-test-token\r\n"
                                "ChatGPT-Account-Id: account-test\r\n"
                                f"Content-Length: {len(body)}\r\n\r\n"
                            ).encode() + body)
                            chunks = []
                            while chunk := client.recv(65536):
                                chunks.append(chunk)
                    finally:
                        server.shutdown()
                        thread.join(timeout=3)
                response = b"".join(chunks)
                connection.assert_called_once_with("chatgpt.com", timeout=60)
                headers = upstream.request.call_args.kwargs["headers"]
                self.assertEqual(headers["Authorization"], "Bearer subscription-test-token")
                self.assertEqual(headers["ChatGPT-Account-Id"], "account-test")
                self.assertIn(b"200 OK", response)
                self.assertIn(b"X-Codex-Turn-State: test-state", response)
                self.assertNotIn(b"subscription-test-token", response)

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
