"""Fixed-destination Gemini API gateway; real credentials stay outside the agent."""

import argparse
import http.client
import http.server
import json
import os
import re
import threading
import time
from urllib.parse import parse_qsl, urlsplit

from .gateway import Server

MAX_BODY = 32 * 1024 * 1024
ENDPOINT = re.compile(r"/v1(?:beta)?/models/([A-Za-z0-9_.-]+):(generateContent|streamGenerateContent|countTokens)\Z")


def validate_request(path, body, expected_model):
    parsed = urlsplit(path)
    match = ENDPOINT.fullmatch(parsed.path)
    if parsed.scheme or parsed.netloc or parsed.fragment or not match:
        raise ValueError("Endpoint is not available")
    if any(key != "alt" or value != "sse" for key, value in parse_qsl(parsed.query)):
        raise ValueError("Unsupported query")
    if not re.fullmatch(r"gemini-[A-Za-z0-9_.-]+", expected_model):
        raise ValueError("Use a native Gemini model ID")
    data = json.loads(body)
    if len(body) > MAX_BODY or not isinstance(data, dict):
        raise ValueError("Invalid request")
    # No server-side tools: shell/file calls are executed by the native CLI.
    tools = data.get("tools", [])
    if not isinstance(tools, list) or any(
        not isinstance(tool, dict) or set(tool) - {"functionDeclarations"}
        for tool in tools
    ):
        raise ValueError("Only client-executed function tools are available")
    # Keep native auxiliary requests and fallback attempts on the selected
    # experiment model, rather than quietly mixing models in one run.
    fixed = parsed.path.replace("/models/" + match[1] + ":", "/models/" + expected_model + ":")
    return fixed + ("?alt=sse" if parsed.query else "")


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def fail(self, status, message):
        payload = json.dumps({"error": {"code": status, "message": message,
                                        "status": "INVALID_ARGUMENT"}}).encode()
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
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get("Transfer-Encoding") or len(lengths) != 1:
                raise ValueError("A single content length is required")
            length = int(lengths[0])
            if not 0 < length <= MAX_BODY:
                raise ValueError("Invalid size")
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("Incomplete request")
            path = validate_request(self.path, body, self.server.expected_model)
        except (OSError, ValueError, TypeError):
            self.fail(400, "Request rejected by experiment gateway")
            return
        with self.server.request_lock:
            if self.server.remaining is not None and self.server.remaining <= 0:
                self.fail(429, "Experiment request limit reached")
                return
            if self.server.remaining is not None:
                self.server.remaining -= 1
        request_ns = time.time_ns()
        if getattr(self.server, "log_request_bodies", False):
            print(json.dumps({"kind": "gateway_request_body", "observed_ns": request_ns,
                              "path": path, "body": json.loads(body)}), flush=True)
        if getattr(self.server, 'antigravity_tool_evidence', False):
            from .antigravity_evidence import shell_results
            for result in shell_results(json.loads(body)):
                print(json.dumps({'kind': 'antigravity_shell_result', 'observed_ns': request_ns,
                                  **result}), flush=True)
        upstream = http.client.HTTPSConnection("generativelanguage.googleapis.com", timeout=60)
        sent_headers = False
        try:
            upstream.request("POST", path, body, {"x-goog-api-key": self.server.api_key,
                                                  "Content-Type": "application/json"})
            response = upstream.getresponse()
            self.send_response(response.status)
            self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
            self.send_header("Connection", "close")
            self.end_headers()
            sent_headers = True
            captured = bytearray()
            while chunk := response.read1(65536):
                if getattr(self.server, "log_response_bodies", False):
                    captured.extend(chunk)
                    if len(captured) > MAX_BODY:
                        raise ValueError("Response evidence exceeds capture limit")
                self.wfile.write(chunk)
                self.wfile.flush()
            if getattr(self.server, "log_response_bodies", False):
                print(json.dumps({"kind": "gateway_response_body", "observed_ns": time.time_ns(),
                                  "request_observed_ns": request_ns, "path": path,
                                  "status": response.status, "response_text": captured.decode()}), flush=True)
            print(json.dumps({"kind": "request", "observed_ns": time.time_ns(),
                              "model": self.server.expected_model,
                              "method": "POST", "path": urlsplit(path).path,
                              "status": response.status}), flush=True)
        except (OSError, ValueError, UnicodeError, http.client.HTTPException):
            if getattr(self.server, 'log_response_bodies', False):
                print(json.dumps({'kind': 'capture_error', 'observed_ns': time.time_ns(),
                                  'request_observed_ns': request_ns, 'path': path}), flush=True)
            if not sent_headers:
                self.fail(502, "Upstream request failed")
        finally:
            upstream.close()
            self.close_connection = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-requests", type=int, default=120)
    parser.add_argument("--expected-model", required=True)
    parser.add_argument("--log-request-bodies", action="store_true")
    parser.add_argument("--log-response-bodies", action="store_true")
    parser.add_argument('--antigravity-tool-evidence', action='store_true')
    args = parser.parse_args()
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY must be provided to the gateway")
    with Server("/relay/api.sock", Handler) as server:
        os.chmod("/relay/api.sock", 0o666)
        server.api_key, server.expected_model = api_key, args.expected_model
        server.antigravity_tool_evidence = args.antigravity_tool_evidence
        server.log_request_bodies = args.log_request_bodies
        server.log_response_bodies = args.log_response_bodies
        server.request_lock = threading.Lock()
        server.remaining = None if args.max_requests == 0 else args.max_requests
        print("gateway ready", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
