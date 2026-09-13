"""Validate and install a private copy of a Codex ChatGPT login cache."""

import argparse
import json
import os
from pathlib import Path
import stat


REQUIRED_TOKEN_FIELDS = ("access_token", "account_id", "id_token", "refresh_token")


def validate_auth_cache(path):
    path = Path(path)
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise RuntimeError("Codex subscription auth must be a regular, non-symlink file")
    if metadata.st_mode & 0o077:
        raise RuntimeError("Codex subscription auth must not be readable by group or others")
    try:
        value = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Codex subscription auth is not valid JSON") from exc
    tokens = value.get("tokens") if isinstance(value, dict) else None
    if not isinstance(value, dict) or value.get("auth_mode") != "chatgpt" or not isinstance(tokens, dict):
        raise RuntimeError("Codex subscription auth is not a ChatGPT login cache")
    if any(not isinstance(tokens.get(field), str) or not tokens[field]
           for field in REQUIRED_TOKEN_FIELDS):
        raise RuntimeError("Codex subscription auth is missing required token fields")
    return path


def install_auth_cache(source, destination, uid=1000, gid=1000):
    source = validate_auth_cache(source)
    destination = Path(destination)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with source.open("rb") as incoming, os.fdopen(descriptor, "wb", closefd=False) as outgoing:
            while chunk := incoming.read(65536):
                outgoing.write(chunk)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        os.fchmod(descriptor, 0o600)
        os.fchown(descriptor, uid, gid)
    except Exception:
        try:
            destination.unlink()
        except OSError:
            pass
        raise
    finally:
        os.close(descriptor)
    os.chown(destination.parent, uid, gid)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    install_auth_cache(args.source, args.destination)
    print("Codex ChatGPT auth cache installed", flush=True)


if __name__ == "__main__":
    main()
