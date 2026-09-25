"""Load a small, literal subset of dotenv syntax without extra dependencies."""

import os
import re


def load_env(path):
    """Load KEY=value lines, preserving variables already in the environment.

    Values may be single- or double-quoted. No expansion, escapes, multiline
    values, or shell execution is performed. Errors never include file contents.
    """
    try:
        contents = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    except UnicodeError:
        raise RuntimeError(f"Invalid UTF-8 in {path.name}") from None
    values = {}
    for number, line in enumerate(contents.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", line)
        if not match:
            raise RuntimeError(f"Invalid dotenv assignment in {path.name} at line {number}")
        key, value = match.groups()
        if value.startswith(("'", '"')):
            quoted = re.fullmatch(r"(['\"])(.*?)\1\s*(?:#.*)?", value)
            if not quoted:
                raise RuntimeError(f"Invalid quoted dotenv value in {path.name} at line {number}")
            value = quoted.group(2)
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        if "\x00" in value:
            raise RuntimeError(f"Invalid dotenv value in {path.name} at line {number}")
        values[key] = value
    for key, value in values.items():
        os.environ.setdefault(key, value)
