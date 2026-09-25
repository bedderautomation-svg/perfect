"""Fixed-destination gateway for Codex ChatGPT-subscription Responses traffic."""

import argparse
import http.client
import http.server
import json
import os
import re
import threading
import time
from urllib.parse import parse_qs, urlsplit

from .gateway import Server
from .openai_gateway import MAX_BODY, MAX_OUTPUT_TOKENS, validate_client_tools


UPSTREAM_HOST = "chatgpt.com"
PREFIX = "/backend-api/codex"
ALLOWED_PATHS = {PREFIX + "/responses", PREFIX + "/responses/compact"}
MODELS_PATH = PREFIX + "/models"


def emit(**fields):
    print(json.dumps({"observed_ns": time.time_ns(), **fields}), flush=True)


def consume_request(server):
    with server.request_lock:
        if server.remaining is not None and server.remaining <= 0:
            return False
        if server.remaining is not None:
            server.remaining -= 1
        return True


def request_shape(body):
    try:
        data = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"json_object": False}
    if not isinstance(data, dict):
        return {"json_object": False}
    tools = data.get("tools", [])
    return {
        "json_object": True,
        "keys": sorted(str(key) for key in data),
        "model": data.get("model") if isinstance(data.get("model"), str) else None,
        "input_type": type(data.get("input")).__name__,
        "store": data.get("store"),
        "tool_types": [tool.get("type") for tool in tools if isinstance(tool, dict)]
        if isinstance(tools, list) else None,
    }


def validate_models_request(path):
    parsed = urlsplit(path)
    query = parse_qs(parsed.query, strict_parsing=True)
    versions = query.get("client_version", [])
    if (parsed.scheme or parsed.netloc or parsed.path != MODELS_PATH or set(query) != {"client_version"}
            or len(versions) != 1 or not re.fullmatch(r"\d+\.\d+\.\d+", versions[0])):
        raise ValueError("Endpoint is not available")
    return parsed.path + "?" + parsed.query


def validate_request(path, body, expected_model):
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.path not in ALLOWED_PATHS:
        raise ValueError("Endpoint is not available")
    if parsed.query or len(body) > MAX_BODY:
        raise ValueError("Request is not available")
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
    validate_client_tools(data.get("tools", []))
    return parsed.path


def forwarded_request_headers(headers):
    authorization = headers.get("Authorization")
    if (not isinstance(authorization, str) or not authorization.startswith("Bearer ")
            or len(authorization) > 16 * 1024):
        raise ValueError("A bounded bearer credential is required")
    forwarded = {
        "Authorization": authorization,
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Accept-Encoding": "identity",
    }
    exact = {"chatgpt-account-id", "openai-beta", "originator", "user-agent",
             "x-client-request-id", "traceparent", "tracestate"}
    for name, value in headers.items():
        lowered = name.lower()
        if lowered in exact or lowered.startswith("x-codex-") or lowered.startswith("x-openai-"):
            if len(value) > 16 * 1024:
                raise ValueError("Request header is too large")
            forwarded[name] = value
    return forwarded


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        # Never log subscription tokens, request headers, or request bodies.
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

    def do_GET(self):
        self.connection.settimeout(60)
        try:
            path = validate_models_request(self.path)
        except ValueError:
            self.fail(400, "Request rejected by experiment gateway")
            return
        try:
            headers = forwarded_request_headers(self.headers)
        except ValueError:
            self.fail(400, "Request rejected by experiment gateway")
            return
        if not consume_request(self.server):
            self.fail(429, "Experiment request limit reached")
            return
        headers["Accept"] = "application/json"
        upstream = http.client.HTTPSConnection(UPSTREAM_HOST, timeout=60)
        try:
            upstream.request("GET", path, headers=headers)
            response = upstream.getresponse()
            body = response.read(MAX_BODY + 1)
            if len(body) > MAX_BODY:
                self.fail(502, "Upstream response is too large")
                return
            if response.status in {401, 403}:
                self.fail(response.status, "Upstream subscription authentication failed")
                return
            self.send_response(response.status)
            self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            emit(kind="request", method="GET", path=MODELS_PATH, status=response.status)
        except (OSError, http.client.HTTPException):
            self.fail(502, "Upstream request failed")
        finally:
            upstream.close()
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
            headers = forwarded_request_headers(self.headers)
        except ValueError as exc:
            emit(kind="rejected", method="POST", reason=str(exc), **request_shape(locals().get("body", b"")))
            self.fail(400, "Request rejected by experiment gateway")
            return
        except (OSError, TypeError):
            self.fail(400, "Request rejected by experiment gateway")
            return
        if not consume_request(self.server):
            self.fail(429, "Experiment request limit reached")
            return
        sent_headers = False
        upstream = http.client.HTTPSConnection(UPSTREAM_HOST, timeout=60)
        try:
            upstream.request("POST", path, body=body, headers=headers)
            response = upstream.getresponse()
            if response.status in {401, 403}:
                response.read()
                self.fail(response.status, "Upstream subscription authentication failed")
                return
            self.send_response(response.status)
            self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
            self.send_header("Connection", "close")
            for name, value in response.getheaders():
                lowered = name.lower()
                if lowered in {"retry-after", "openai-processing-ms"} or \
                        lowered.startswith("x-codex-") or lowered.startswith("x-openai-"):
                    self.send_header(name, value)
            self.end_headers()
            sent_headers = True
            while chunk := response.read1(65536):
                self.wfile.write(chunk)
                self.wfile.flush()
            emit(kind="request", method="POST", path=path, status=response.status)
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
    with Server("/relay/api.sock", Handler) as server:
        os.chmod("/relay/api.sock", 0o666)
        server.expected_model = args.expected_model
        server.request_lock = threading.Lock()
        server.remaining = None if args.max_requests == 0 else args.max_requests
        print("gateway ready", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
