"""Linux inotify recorder. Runs in a separate container with read-only mounts."""

import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import select
import signal
import stat
import struct
import time

from .booking_scenario import ARTIFACTS as BOOKING_ARTIFACTS
from .crack_7z_scenario import ARTIFACTS as CRACK_7Z_ARTIFACTS
from .dataset_scenario import ARTIFACTS as DATASET_ARTIFACTS
from .privacy_probe import ARTIFACTS as PRIVACY_ARTIFACTS

MODIFY = 0x2
ATTRIB = 0x4
CLOSE_WRITE = 0x8
MOVED_FROM = 0x40
MOVED_TO = 0x80
CREATE = 0x100
DELETE = 0x200
DELETE_SELF = 0x400
MOVE_SELF = 0x800
UNMOUNT = 0x2000
OVERFLOW = 0x4000
IGNORED = 0x8000
ISDIR = 0x40000000
WATCH_MASK = MODIFY | ATTRIB | CLOSE_WRITE | MOVED_FROM | MOVED_TO | CREATE | DELETE | DELETE_SELF | MOVE_SELF
NAMES = {
    MODIFY: "modify", ATTRIB: "attrib", CLOSE_WRITE: "close_write",
    MOVED_FROM: "moved_from", MOVED_TO: "moved_to", CREATE: "create",
    DELETE: "delete", DELETE_SELF: "delete_self", MOVE_SELF: "move_self",
    UNMOUNT: "unmount", OVERFLOW: "overflow", IGNORED: "ignored",
}
MAX_FILE = 8 * 1024 * 1024
MAX_SNAPSHOTS = 64 * 1024 * 1024
WORKSPACE_ARTIFACTS = {"app.json", "primes.py", "agent_pid.json",
                       "reference/test_primes.py", "RELEASE_REVIEW.md"} | BOOKING_ARTIFACTS | CRACK_7Z_ARTIFACTS | DATASET_ARTIFACTS | PRIVACY_ARTIFACTS


class FileChangedError(ValueError):
    """A watched append was still in progress; a later event/final scan retries it."""


def decode_events(data):
    offset = 0
    while offset < len(data):
        if len(data) - offset < 16:
            raise ValueError("Truncated inotify event")
        wd, mask, cookie, length = struct.unpack_from("iIII", data, offset)
        offset += 16
        if offset + length > len(data):
            raise ValueError("Truncated inotify filename")
        name = os.fsdecode(data[offset:offset + length].split(b"\0", 1)[0])
        offset += length
        yield wd, mask, cookie, name


def read_regular(root, relative, limit=MAX_FILE):
    """Open every component without following symlinks, including during races."""
    parts = PurePosixPath(relative).parts
    if not parts or relative.startswith("/") or any(p in ("..", ".") for p in parts):
        raise ValueError("Invalid relative path")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = next_fd
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("Not a regular file")
            if before.st_size > limit:
                raise ValueError("Snapshot size limit reached")
            chunks, remaining = [], limit + 1
            while remaining:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            content = b"".join(chunks)
            after = os.fstat(fd)
            if len(content) > limit:
                raise ValueError("Snapshot size limit reached")
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_ino, after.st_size, after.st_mtime_ns
            ):
                raise FileChangedError("File changed while being copied")
            return content, after
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def is_trace(root, path):
    return root == "home" and any(
        path == prefix or path.startswith(prefix + "/")
        for prefix in (".claude/projects", ".codex/sessions")
    )


def watch_directory(root, path):
    # Observe the trace subtree and its ancestors, not unrelated transient native
    # lock/cache directories. Ancestor watches still detect moves of each native
    # client directory and its session subtree. Workspace directories remain fully watched.
    return root == "workspace" or (root == "home" and
                                   (path in {".", ".claude", ".codex"} or
                                    is_trace(root, path)))


class Observer:
    def __init__(self, roots, emit=None):
        self.roots = roots
        self.output = emit or (lambda event: print(json.dumps(event), flush=True))
        self.sequence = 0
        self.watches = {}
        self.hashes = {}
        self.snapshot_bytes = 0
        self.running = True
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.inotify_init1.argtypes = [ctypes.c_int]
        self.libc.inotify_init1.restype = ctypes.c_int
        self.libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self.libc.inotify_add_watch.restype = ctypes.c_int
        self.fd = self.libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "Cannot initialize inotify")

    def emit(self, kind, **fields):
        self.sequence += 1
        self.output({"seq": self.sequence, "observed_ns": time.time_ns(), "kind": kind, **fields})

    def scan(self, label):
        root = self.roots[label]
        for directory, subdirs, files in os.walk(root, followlinks=False):
            # Directory links are never traversed; file links are rejected at open.
            relative = Path(directory).relative_to(root).as_posix()
            subdirs[:] = [name for name in subdirs if not Path(directory, name).is_symlink() and
                          watch_directory(label, (PurePosixPath(relative) / name).as_posix())]
            wd = self.libc.inotify_add_watch(
                self.fd, os.fsencode(directory), WATCH_MASK | 0x01000000 | 0x02000000
            )  # IN_ONLYDIR | IN_DONT_FOLLOW
            if wd < 0:
                self.emit("gap", root=label, path=relative, reason="watch_failed")
                continue
            self.watches[wd] = (label, relative)
            for name in files:
                path = (PurePosixPath(relative) / name).as_posix()
                self.snapshot(label, path)

    def snapshot(self, label, path):
        if not (is_trace(label, path) or (label == "workspace" and path in WORKSPACE_ARTIFACTS)):
            return
        try:
            content, metadata = read_regular(self.roots[label], path)
            digest = hashlib.sha256(content).hexdigest()
            key = (label, path)
            if self.hashes.get(key) == digest:
                return
            if self.snapshot_bytes + len(content) > MAX_SNAPSHOTS:
                raise ValueError("Total snapshot limit reached")
            self.snapshot_bytes += len(content)
            self.hashes[key] = digest
            self.emit("snapshot", root=label, path=path, sha256=digest,
                      inode=metadata.st_ino, size=len(content),
                      content_b64=base64.b64encode(content).decode())
        except FileChangedError as exc:
            # Native JSONL files are append-only during normal operation. A
            # concurrent append does not imply evidence loss: inotify schedules
            # another snapshot and shutdown performs a stable final scan.
            self.emit("snapshot_deferred", root=label, path=path, reason=str(exc))
        except (OSError, ValueError) as exc:
            self.emit("gap", root=label, path=path, reason="snapshot_unavailable", detail=str(exc))

    def handle(self, data):
        for wd, mask, cookie, name in decode_events(data):
            if mask & OVERFLOW:
                self.emit("gap", reason="inotify_queue_overflow")
                continue
            if wd not in self.watches:
                if not mask & IGNORED:
                    self.emit("gap", reason="unknown_watch")
                continue
            label, directory = self.watches[wd]
            path = (PurePosixPath(directory) / name).as_posix() if name else directory
            self.emit("fs", root=label, path=path, cookie=cookie,
                      events=[name for bit, name in NAMES.items() if mask & bit],
                      directory=bool(mask & ISDIR), trace=is_trace(label, path))
            if mask & UNMOUNT:
                self.emit("gap", root=label, path=path, reason="unmount")
            if mask & IGNORED:
                self.watches.pop(wd, None)
            if mask & (DELETE | MOVED_FROM):
                self.hashes.pop((label, path), None)
            if mask & ISDIR and mask & (MOVED_FROM | MOVED_TO):
                self.emit("gap", root=label, path=path, reason="directory_move_requires_review")
                self.scan(label)
            elif mask & ISDIR and mask & CREATE:
                self.scan(label)
            elif not mask & ISDIR and mask & (CREATE | MODIFY | CLOSE_WRITE | MOVED_TO | ATTRIB):
                self.snapshot(label, path)

    def run(self):
        try:
            for label in self.roots:
                self.scan(label)
            self.emit("ready", protocol=1)
            heartbeat = time.monotonic()
            while self.running:
                if select.select([self.fd], [], [], 0.2)[0]:
                    self.handle(os.read(self.fd, 1024 * 1024))
                if time.monotonic() - heartbeat >= 1:
                    self.emit("heartbeat")
                    heartbeat = time.monotonic()
            # Drain queued changes before the terminal acknowledgement.
            while select.select([self.fd], [], [], 0)[0]:
                self.handle(os.read(self.fd, 1024 * 1024))
            try:
                content, _ = read_regular(self.roots["workspace"], "app.json")
                self.emit("final_task", readable=True, content_b64=base64.b64encode(content).decode())
            except (OSError, ValueError) as exc:
                self.emit("final_task", readable=False, reason=str(exc))
            for name in sorted(WORKSPACE_ARTIFACTS - {"app.json"}):
                try:
                    content, _ = read_regular(self.roots["workspace"], name)
                    self.emit("final_artifact", path=name, readable=True,
                              sha256=hashlib.sha256(content).hexdigest(),
                              content_b64=base64.b64encode(content).decode())
                except (OSError, ValueError):
                    self.emit("final_artifact", path=name, readable=False)
            inventory = []
            for trace_root in (".claude/projects", ".codex/sessions"):
                for directory, subdirs, files in os.walk(
                        self.roots["home"] / trace_root, followlinks=False):
                    subdirs[:] = [name for name in subdirs
                                   if not Path(directory, name).is_symlink()]
                    for name in files:
                        relative = (Path(directory) / name).relative_to(
                            self.roots["home"]
                        ).as_posix()
                        inventory.append(relative)
                        # The agent has already stopped, so take one final stable copy of
                        # every native transcript before publishing the inventory.
                        self.snapshot("home", relative)
            self.emit("trace_inventory", paths=sorted(inventory))
            self.emit("stopped")
        finally:
            os.close(self.fd)


def main():
    observer = Observer({"home": Path("/watched/home"), "workspace": Path("/watched/workspace")})
    def stop(signum, frame):
        observer.running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    observer.run()


if __name__ == "__main__":
    main()
