"""Fixed-destination gateway for the Cursor Agent API.

The native process receives a placeholder API key. This gateway exchanges the
real host key with Cursor and returns the resulting short-lived access token to
the native client, without exposing the long-lived API key to the agent.
"""

import argparse
import http.client
import http.server
import json
import os
import re
import threading
import time
from urllib.parse import urlsplit

from .gateway import Server


MAX_BODY = 32 * 1024 * 1024
MAX_HEADER = 16 * 1024
UPSTREAM_HOST = "api2.cursor.sh"
PLACEHOLDER = "cursor-trace-lab-placeholder"
ALLOWED_PATH = re.compile(
    r"/(?:auth/(?:exchange_user_api_key|refresh)|(?:aiserver|agent)\.v1\.[A-Za-z0-9_.]+/[A-Za-z0-9_]+)\Z"
)
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
}


def validate_path(path):
    if not isinstance(path, str) or len(path) > 4096:
        raise ValueError("Invalid Cursor endpoint")
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.fragment or not ALLOWED_PATH.fullmatch(parsed.path):
        raise ValueError("Cursor endpoint is not available")
    return parsed.path + (("?" + parsed.query) if parsed.query else "")


def forwarded_headers(headers, path, api_key):
    forwarded = {}
    for name, value in headers.items():
        lowered = name.casefold()
        if lowered in HOP_BY_HOP or len(name) + len(value) > MAX_HEADER:
            continue
        forwarded[name] = value
    if path == "/auth/exchange_user_api_key":
        authorization = headers.get("Authorization")
        if authorization != "Bearer " + PLACEHOLDER:
            raise ValueError("Expected the experiment placeholder credential")
        forwarded["Authorization"] = "Bearer " + api_key
    elif path.startswith(("/aiserver.v1.", "/agent.v1.")):
        authorization = headers.get("Authorization")
        if authorization:
            if not authorization.startswith("Bearer ") or len(authorization) > MAX_HEADER:
                raise ValueError("Invalid Cursor access token header")
            forwarded["Authorization"] = authorization
    forwarded["Accept-Encoding"] = "identity"
    return forwarded


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def fail(self, status, message):
        payload = json.dumps({"error": message}).encode()
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
            if len(lengths) > 1:
                raise ValueError("At most one content length is allowed")
            length = int(lengths[0]) if lengths else 0
            if not 0 <= length <= MAX_BODY:
                raise ValueError("Invalid request size")
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("Incomplete request")
            path = validate_path(self.path)
            headers = forwarded_headers(self.headers, urlsplit(path).path, self.server.api_key)
        except (ValueError, OSError, TypeError):
            self.fail(400, "Request rejected by experiment gateway")
            return
        with self.server.request_lock:
            if self.server.remaining is not None and self.server.remaining <= 0:
                self.fail(429, "Experiment request limit reached")
                return
            if self.server.remaining is not None:
                self.server.remaining -= 1
        sent_headers = False
        upstream = http.client.HTTPSConnection(UPSTREAM_HOST, timeout=60)
        try:
            upstream.request("POST", path, body=body, headers=headers)
            response = upstream.getresponse()
            self.send_response(response.status)
            for name, value in response.getheaders():
                if name.casefold() not in HOP_BY_HOP:
                    self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            sent_headers = True
            while chunk := response.read1(65536):
                self.wfile.write(chunk)
                self.wfile.flush()
            print(json.dumps({
                "kind": "request", "observed_ns": time.time_ns(),
                "method": "POST", "path": urlsplit(path).path,
                "status": response.status,
            }), flush=True)
        except (OSError, http.client.HTTPException):
            if not sent_headers:
                self.fail(502, "Upstream request failed")
        finally:
            upstream.close()
            self.close_connection = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-requests", type=int, default=120)
    args = parser.parse_args()
    api_key = os.environ.get("CURSOR_API_KEY")
    if not api_key:
        raise SystemExit("CURSOR_API_KEY must be provided to the gateway")
    with Server("/relay/api.sock", Handler) as server:
        os.chmod("/relay/api.sock", 0o666)
        server.api_key = api_key
        server.request_lock = threading.Lock()
        server.remaining = None if args.max_requests == 0 else args.max_requests
        print("gateway ready", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
