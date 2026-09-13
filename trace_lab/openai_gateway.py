"""A fixed-destination Responses API gateway isolated from the agent process."""

import argparse
import http.client
import http.server
import json
import os
import threading
from urllib.parse import urlsplit

from .gateway import Server


MAX_BODY = 16 * 1024 * 1024
MAX_OUTPUT_TOKENS = 65536
ALLOWED_PATHS = {"/v1/responses", "/v1/responses/compact"}
CLIENT_TOOL_TYPES = {"custom", "function", "local_shell"}


def validate_client_tools(tools):
    if not isinstance(tools, list) or len(tools) > 256:
        raise ValueError("Tool list is outside the gateway bounds")
    for tool in tools:
        if not isinstance(tool, dict):
            raise ValueError("Only client-executed tools are available")
        kind = tool.get("type")
        if kind in CLIENT_TOOL_TYPES:
            continue
        if kind == "namespace":
            children = tool.get("tools")
            if not isinstance(children, list) or not children or len(children) > 256:
                raise ValueError("Tool namespace is outside the gateway bounds")
            if any(not isinstance(child, dict) or child.get("type") not in CLIENT_TOOL_TYPES
                   for child in children):
                raise ValueError("Only client-executed namespaced tools are available")
            continue
        raise ValueError("Only client-executed tools are available")


def validate_request(path, body, expected_model):
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.path not in ALLOWED_PATHS:
        raise ValueError("Endpoint is not available")
    if len(body) > MAX_BODY:
        raise ValueError("Request is too large")
    data = json.loads(body)
    if not isinstance(data, dict) or not isinstance(data.get("input"), (str, list)):
        raise ValueError("Expected a Responses API request")
    if data.get("model") != expected_model:
        raise ValueError("Request model differs from the configured experiment model")
    tokens = data.get("max_output_tokens")
    if tokens is not None and (type(tokens) is not int or not 1 <= tokens <= MAX_OUTPUT_TOKENS):
        raise ValueError("Output token limit is outside the gateway bounds")
    if data.get("store") is True:
        raise ValueError("Server-side response storage is unavailable")
    # Codex executes these tool calls locally. Hosted tools would escape Docker's
    # network boundary through the upstream API and are therefore rejected.
    validate_client_tools(data.get("tools", []))
    return parsed.path + ("?" + parsed.query if parsed.query else "")


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def fail(self, status, message):
        payload = json.dumps({"error": {
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
            path = validate_request(self.path, body, self.server.expected_model)
        except (ValueError, OSError, TypeError):
            self.fail(400, "Request rejected by experiment gateway")
            return
        with self.server.request_lock:
            if self.server.remaining <= 0:
                self.fail(429, "Experiment request limit reached")
                return
            self.server.remaining -= 1
        headers = {
            "Authorization": "Bearer " + self.server.api_key,
            "Content-Type": "application/json",
            "Accept": self.headers.get("Accept", "text/event-stream"),
        }
        if self.headers.get("OpenAI-Beta"):
            headers["OpenAI-Beta"] = self.headers["OpenAI-Beta"]
        sent_headers = False
        upstream = http.client.HTTPSConnection("api.openai.com", timeout=60)
        try:
            upstream.request("POST", path, body=body, headers=headers)
            response = upstream.getresponse()
            if response.status in {401, 403}:
                response.read()
                self.fail(response.status, "Upstream authentication failed")
                return
            self.send_response(response.status)
            self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
            self.send_header("Connection", "close")
            if response.getheader("x-request-id"):
                self.send_header("x-request-id", response.getheader("x-request-id"))
            self.end_headers()
            sent_headers = True
            while chunk := response.read1(65536):
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, http.client.HTTPException):
            if not sent_headers:
                self.fail(502, "Upstream request failed")
        finally:
            upstream.close()
            self.close_connection = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-requests", type=int, default=60)
    parser.add_argument("--expected-model", required=True)
    args = parser.parse_args()
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY must be provided to the gateway")
    with Server("/relay/api.sock", Handler) as server:
        os.chmod("/relay/api.sock", 0o666)
        server.api_key = api_key
        server.expected_model = args.expected_model
        server.request_lock = threading.Lock()
        server.remaining = args.max_requests
        print("gateway ready", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
