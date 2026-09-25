"""A fixed-destination Messages API gateway, isolated from the agent process."""

import argparse
import http.client
import http.server
import json
import os
import socketserver
import threading
import time
from urllib.parse import urlsplit

MAX_BODY = 16 * 1024 * 1024
MAX_OUTPUT_TOKENS = 65536
MODEL_OUTPUT_LIMITS = {"claude-opus-5-5": 128000}
ALLOWED_PATHS = {"/v1/messages", "/v1/messages/count_tokens"}


def validate_request(path, body):
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.path not in ALLOWED_PATHS:
        raise ValueError("Endpoint is not available")
    if len(body) > MAX_BODY:
        raise ValueError("Request is too large")
    data = json.loads(body)
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        raise ValueError("Expected a Messages API request")
    if not isinstance(data.get("model"), str) or not data["model"].startswith("claude-"):
        raise ValueError("Expected an explicit Claude model identifier")
    if parsed.path == "/v1/messages":
        tokens = data.get("max_tokens")
        limit = MODEL_OUTPUT_LIMITS.get(data["model"], MAX_OUTPUT_TOKENS)
        if type(tokens) is not int or not 1 <= tokens <= limit:
            raise ValueError("Output token limit is outside the gateway bounds")
    # Server-executed tools could access external targets independently of Docker.
    for tool in data.get("tools", []):
        if not isinstance(tool, dict) or tool.get("type", "custom") != "custom":
            raise ValueError("Only client-executed custom tools are available")
    return parsed.path + ("?" + parsed.query if parsed.query else "")


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        # Do not log headers, credentials, request bodies, or agent-controlled URLs.
        pass

    def fail(self, status, message):
        payload = json.dumps({"type": "error", "error": {
            "type": "invalid_request_error", "message": message,
        }}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def do_POST(self):
        self.connection.settimeout(60)
        try:
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("Chunked request bodies are not supported")
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1:
                raise ValueError("A single content length is required")
            length = int(lengths[0])
            if not 0 < length <= MAX_BODY:
                raise ValueError("Invalid request size")
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("Incomplete request")
            path = validate_request(self.path, body)
        except (ValueError, OSError, TypeError) as exc:
            # Validation errors contain fixed diagnostics, never request bodies
            # or credentials. Preserve why a new native client was rejected.
            print(json.dumps({"kind": "gateway_rejected", "reason": str(exc)[:200]}), flush=True)
            self.fail(400, "Request rejected by experiment gateway")
            return
        with self.server.request_lock:
            if self.server.remaining is not None and self.server.remaining <= 0:
                self.fail(429, "Experiment request limit reached")
                return
            if self.server.remaining is not None:
                self.server.remaining -= 1
        if getattr(self.server, "log_request_bodies", False):
            print(json.dumps({"kind": "gateway_request_body", "observed_ns": time.time_ns(),
                              "path": path, "body": json.loads(body)}), flush=True)
        headers = {
            "x-api-key": self.server.api_key,
            "anthropic-version": self.headers.get("anthropic-version", "2023-06-01"),
            "Content-Type": "application/json",
        }
        if self.headers.get("anthropic-beta"):
            headers["anthropic-beta"] = self.headers["anthropic-beta"]
        sent_headers = False
        upstream = http.client.HTTPSConnection("api.anthropic.com", timeout=60)
        try:
            upstream.request("POST", path, body=body, headers=headers)
            response = upstream.getresponse()
            self.send_response(response.status)
            self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
            self.send_header("Connection", "close")
            if response.getheader("request-id"):
                self.send_header("request-id", response.getheader("request-id"))
            self.end_headers()
            sent_headers = True
            # read1 preserves incremental SSE output instead of waiting for 64 KiB.
            while chunk := response.read1(65536):
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, http.client.HTTPException):
            if not sent_headers:
                self.fail(502, "Upstream request failed")
        finally:
            upstream.close()
            self.close_connection = True


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    request_queue_size = 8


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-requests", type=int, default=60)
    parser.add_argument("--log-request-bodies", action="store_true",
                        help="opt-in synthetic compaction evidence; never records headers")
    args = parser.parse_args()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY must be provided to the gateway")
    with Server("/relay/api.sock", Handler) as server:
        os.chmod("/relay/api.sock", 0o666)
        server.api_key = api_key
        server.request_lock = threading.Lock()
        server.remaining = None if args.max_requests == 0 else args.max_requests
        server.log_request_bodies = args.log_request_bodies
        print("gateway ready", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
