"""Uncapped request budgets retain finite-budget behavior across native clients."""

from contextlib import ExitStack
import http.client
import io
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from trace_lab import (chatgpt_gateway, claude_subscription_gateway, cursor_gateway,
                       gateway, openai_gateway)


MODULES = (gateway, openai_gateway, claude_subscription_gateway, chatgpt_gateway, cursor_gateway)


class UnlimitedGatewayTests(unittest.TestCase):
    def test_zero_configures_unlimited_and_positive_limits_are_preserved(self):
        for module in MODULES:
            for limit in (0, 2):
                with self.subTest(module=module.__name__, limit=limit), ExitStack() as stack:
                    factory = stack.enter_context(patch.object(module, "Server"))
                    server = factory.return_value.__enter__.return_value
                    stack.enter_context(patch.object(module.os, "chmod"))
                    stack.enter_context(patch.dict(module.os.environ, {
                        "ANTHROPIC_API_KEY": "test-only", "OPENAI_API_KEY": "test-only",
                        "CURSOR_API_KEY": "test-only",
                    }))
                    argv = [module.__name__, "--max-requests", str(limit)]
                    if module in (openai_gateway, chatgpt_gateway):
                        argv += ["--expected-model", "test-model"]
                    stack.enter_context(patch("sys.argv", argv))
                    module.main()
                    self.assertEqual(server.remaining, None if limit == 0 else limit)
                    server.serve_forever.assert_called_once()

    def test_chatgpt_consumer_has_no_count_cap_when_unlimited(self):
        server = SimpleNamespace(remaining=None, request_lock=threading.Lock())
        for _ in range(150):
            self.assertTrue(chatgpt_gateway.consume_request(server))
        self.assertIsNone(server.remaining)
        server.remaining = 1
        self.assertTrue(chatgpt_gateway.consume_request(server))
        self.assertFalse(chatgpt_gateway.consume_request(server))

    def test_post_handlers_allow_repeated_unlimited_requests_and_still_enforce_caps(self):
        for module in (gateway, openai_gateway, claude_subscription_gateway, cursor_gateway):
            with self.subTest(module=module.__name__), ExitStack() as stack:
                # Exercise the actual handlers' budget branches without sockets,
                # external requests, or real credentials.
                for name in ("validate_request", "validate_path", "forwarded_request_headers", "forwarded_headers"):
                    if hasattr(module, name):
                        stack.enter_context(patch.object(module, name, return_value="/v1/messages"
                                                        if name.startswith("validate") else {}))
                if hasattr(module, "emit"):
                    stack.enter_context(patch.object(module, "emit"))
                stack.enter_context(patch("builtins.print"))
                connection = stack.enter_context(patch.object(module.http.client, "HTTPSConnection"))
                response = connection.return_value.getresponse.return_value
                response.status = 200
                response.read1.return_value = b""
                response.getheaders.return_value = []
                handler = object.__new__(module.Handler)
                handler.server = SimpleNamespace(remaining=None, request_lock=threading.Lock(),
                                                 api_key="test-only", expected_model="test-model")
                handler.connection = Mock()
                handler.path = "/v1/messages"
                handler.headers = http.client.HTTPMessage()
                handler.headers.add_header("Content-Length", "2")
                handler.wfile = io.BytesIO()
                for name in ("send_response", "send_header", "end_headers", "fail"):
                    setattr(handler, name, Mock())
                for _ in range(150):
                    handler.rfile = io.BytesIO(b"{}")
                    handler.do_POST()
                handler.fail.assert_not_called()
                self.assertEqual(connection.call_count, 150)
                self.assertIsNone(handler.server.remaining)
                handler.server.remaining = 1
                handler.rfile = io.BytesIO(b"{}")
                handler.do_POST()
                self.assertEqual(handler.server.remaining, 0)
                handler.rfile = io.BytesIO(b"{}")
                handler.do_POST()
                self.assertEqual(handler.fail.call_args.args[0], 400 if module is openai_gateway else 429)
                self.assertEqual(connection.call_count, 151)


if __name__ == "__main__":
    unittest.main()
