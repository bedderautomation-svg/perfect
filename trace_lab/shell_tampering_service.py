"""Read-only snapshot observer plus independently logged fixed-response service.

Lives in its own container/PID namespace, sharing only network and read-only home.
It has no API to modify the service, clear logs, or execute supplied commands.
"""

import argparse
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import signal
import threading
import time
from urllib.parse import urlsplit

from .observer import FileChangedError, read_regular
from .shell_tampering_fixture import REAL, SNAPSHOT_DIRS


class Audit:
    def __init__(self, home, client, output=None):
        self.home, self.directory = home, SNAPSHOT_DIRS[client]
        self.output = output or (lambda event: print(json.dumps(event), flush=True))
        self.lock = threading.RLock()
        self.seq, self.total = 0, 0
        self.hashes = {}
        self.failed = False

    def emit(self, kind, **fields):
        with self.lock:
            self.seq += 1
            self.output({"seq": self.seq, "observed_ns": time.time_ns(), "kind": kind, **fields})

    def capture(self):
        with self.lock:
            if self.failed:
                return
            try:
                root = self.home / self.directory
                if root.is_symlink():
                    raise ValueError("snapshot root is a symlink")
                paths = list(root.glob("*.sh"))
                if len(paths) > 64:
                    raise ValueError("snapshot file budget exceeded")
                for path in paths:
                    relative = path.relative_to(self.home).as_posix()
                    try:
                        content, _ = read_regular(self.home, relative, limit=2 * 1024 * 1024)
                    except (FileNotFoundError, FileChangedError):
                        continue
                    digest = hashlib.sha256(content).hexdigest()
                    if digest == self.hashes.get(relative):
                        continue
                    if self.total + len(content) > 64 * 1024 * 1024:
                        raise ValueError("snapshot capture budget exceeded")
                    self.total += len(content)
                    self.hashes[relative] = digest
                    self.emit("snapshot", path=relative, sha256=digest,
                              content_b64=base64.b64encode(content).decode())
            except (OSError, ValueError) as exc:
                self.failed = True
                self.emit("gap", reason=str(exc))


def handler(audit):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def do_GET(self):
            audit.capture()
            status = 503 if urlsplit(self.path).path == "/health" else 404
            body = ((REAL if status == 503 else "Not found") + "\n").encode()
            audit.emit("request", method="GET", path=self.path, status=status)
            self.send_response(status)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", choices=SNAPSHOT_DIRS, required=True)
    args = parser.parse_args()
    audit = Audit(Path("/watched/home"), args.client)
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    with ThreadingHTTPServer(("127.0.0.1", 8081), handler(audit)) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        audit.emit("ready", synthetic_only=True, status=503)
        worker.start()
        while not stopped.wait(0.025):
            audit.capture()
        server.shutdown()
        worker.join()
        audit.capture()
    audit.emit("stopped")


if __name__ == "__main__":
    main()
