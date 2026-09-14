"""Fixed-destination gateway for Claude subscription Messages traffic."""

import argparse
import http.client
import http.server
import json
import os
import threading
import time

from .gateway import MAX_BODY, Server, validate_request


def emit(**fields):
    print(json.dumps({"observed_ns": time.time_ns(), **fields}), flush=True)


def forwarded_request_headers(headers):
    authorization = headers.get("Authorization")
    if (not isinstance(authorization, str) or not authorization.startswith("Bearer ")
            or len(authorization) > 16 * 1024):
        raise ValueError("A bounded bearer credential is required")
    forwarded = {
        "Authorization": authorization,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "anthropic-version": headers.get("anthropic-version", "2023-06-01"),
    }
    for name in ("anthropic-beta", "user-agent", "x-app"):
        value = headers.get(name)
        if value:
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
            headers = forwarded_request_headers(self.headers)
        except (ValueError, OSError, TypeError):
            self.fail(400, "Request rejected by experiment gateway")
            return
        with self.server.request_lock:
            if self.server.remaining <= 0:
                self.fail(429, "Experiment request limit reached")
                return
            self.server.remaining -= 1
        sent_headers = False
        upstream = http.client.HTTPSConnection("api.anthropic.com", timeout=60)
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
            if response.getheader("request-id"):
                self.send_header("request-id", response.getheader("request-id"))
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
    args = parser.parse_args()
    with Server("/relay/api.sock", Handler) as server:
        os.chmod("/relay/api.sock", 0o666)
        server.request_lock = threading.Lock()
        server.remaining = args.max_requests
        print("gateway ready", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
