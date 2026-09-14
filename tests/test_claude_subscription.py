import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from trace_lab.claude_auth import install_auth_cache, validate_auth_cache
from trace_lab.claude_subscription_gateway import Handler, forwarded_request_headers
from trace_lab.gateway import Server


class ClaudeSubscriptionAuthTests(unittest.TestCase):
    def value(self):
        return {"claudeAiOauth": {
            "accessToken": "access-test", "refreshToken": "refresh-test",
            "subscriptionType": "max",
        }}

    def test_private_cache_is_validated_and_copied(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "credentials.json"
            content = json.dumps(self.value()).encode()
            source.write_bytes(content)
            source.chmod(0o600)
            self.assertEqual(validate_auth_cache(source), source)
            destination = Path(temporary) / "home" / ".claude" / ".credentials.json"
            install_auth_cache(source, destination, uid=-1, gid=-1)
            self.assertEqual(destination.read_bytes(), content)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

    def test_cache_rejects_public_permissions_and_wrong_shape(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "credentials.json"
            source.write_text(json.dumps(self.value()))
            source.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, "group or others"):
                validate_auth_cache(source)
            source.chmod(0o600)
            source.write_text(json.dumps({"notOauth": {}}))
            with self.assertRaisesRegex(RuntimeError, "Claude.ai"):
                validate_auth_cache(source)


class ClaudeSubscriptionGatewayTests(unittest.TestCase):
    def test_only_bearer_auth_is_forwarded(self):
        headers = {"Authorization": "Bearer subscription-test", "anthropic-beta": "oauth-test"}
        forwarded = forwarded_request_headers(headers)
        self.assertEqual(forwarded["Authorization"], "Bearer subscription-test")
        self.assertEqual(forwarded["anthropic-beta"], "oauth-test")
        with self.assertRaises(ValueError):
            forwarded_request_headers({"x-api-key": "not-subscription"})

    def test_gateway_forwards_subscription_auth_only_to_anthropic(self):
        class Response:
            status = 200
            chunks = iter([b"event: message_stop\ndata: {}\n\n", b""])

            def getheader(self, name, default=None):
                return "text/event-stream" if name == "Content-Type" else default

            def read1(self, size):
                return next(self.chunks)

        body = json.dumps({"model": "claude-test", "messages": [], "max_tokens": 16}).encode()
        with tempfile.TemporaryDirectory(prefix="tl-claude-sub-", dir="/tmp") as temporary:
            endpoint = str(Path(temporary) / "api.sock")
            with patch("trace_lab.claude_subscription_gateway.http.client.HTTPSConnection") as connection:
                upstream = connection.return_value
                upstream.getresponse.return_value = Response()
                with Server(endpoint, Handler) as server:
                    server.request_lock = threading.Lock()
                    server.remaining = 1
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    try:
                        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                            client.settimeout(3)
                            client.connect(endpoint)
                            client.sendall((
                                "POST /v1/messages HTTP/1.1\r\nHost: local\r\n"
                                "Authorization: Bearer subscription-test\r\n"
                                f"Content-Length: {len(body)}\r\n\r\n"
                            ).encode() + body)
                            response = b"".join(iter(lambda: client.recv(65536), b""))
                    finally:
                        server.shutdown()
                        thread.join(timeout=3)
                connection.assert_called_once_with("api.anthropic.com", timeout=60)
                headers = upstream.request.call_args.kwargs["headers"]
                self.assertEqual(headers["Authorization"], "Bearer subscription-test")
                self.assertIn(b"200 OK", response)
                self.assertNotIn(b"subscription-test", response)


if __name__ == "__main__":
    unittest.main()
