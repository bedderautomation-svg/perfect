#!/usr/bin/env python3
"""Validate setup and fixture tests without Docker, credentials, or live models.

Only test_*.py is discovered. Python audit guards allow local IPC but reject
outbound sockets and real native clients. Local Python children inherit those
guards; the isolated prime fixture is checked before its -I invocation.
"""

import ast
import contextlib
import fnmatch
import importlib
import io
import ipaddress
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
KEYS = {
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY",
    "OPENROUTER_API_KEY", "MUSE_OPENROUTER_API_KEY", "CURSOR_API_KEY",
    "GEMINI_API_KEY",
}
NATIVE = {"claude", "codex", "opencode", "cursor-agent", "gemini", "muse",
          "grok", "zcode", "kimi", "antigravity"}
_installed = False


def local_address(address):
    if isinstance(address, (str, bytes)):  # Unix-domain socket.
        return True
    try:
        return ipaddress.ip_address(address[0].split("%", 1)[0]).is_loopback
    except (ValueError, IndexError, TypeError):
        return False


def install_guards():
    """Install the same guards in the runner and its ordinary Python children."""
    global _installed
    if _installed:
        return
    _installed = True
    guard_dir = os.environ["TRACE_LAB_OFFLINE_GUARD_DIR"]
    from trace_lab.release_scenario import STARTER_SOURCE
    test_tree = ast.parse((ROOT / "tests/test_release_scenario.py").read_text())
    repaired_source = next(ast.literal_eval(node.value) for node in test_tree.body
                           if isinstance(node, ast.Assign) and any(
                               isinstance(target, ast.Name) and target.id == "CORRECT_SOURCE"
                               for target in node.targets))
    isolated_sources = {STARTER_SOURCE, repaired_source}

    def audit(event, args):
        if event in {"socket.connect", "socket.bind", "socket.sendto"}:
            if not local_address(args[-1]):
                raise RuntimeError("Offline validation blocked a non-loopback socket")
        elif event == "socket.getaddrinfo":
            host = args[0]
            if host != "localhost" and not local_address((host, 0)):
                raise RuntimeError("Offline validation blocked external name resolution")
        elif event in {"os.system", "os.posix_spawn", "os.posix_spawnp"}:
            raise RuntimeError("Offline validation requires reviewed subprocess commands")
        elif event == "subprocess.Popen":
            executable, argv, _, environment = args
            command = Path(os.fsdecode(executable)).name
            environment = os.environ if environment is None else environment
            if command in NATIVE:
                target = shutil.which(os.fsdecode(executable), path=environment.get("PATH", ""))
                if not target or not Path(target).resolve().is_relative_to(Path(tempfile.gettempdir())):
                    raise RuntimeError("Offline validation blocked a real native model client")
                source = Path(target).read_text()
                if not source.startswith("#!") or "python" not in source.splitlines()[0]:
                    raise RuntimeError("Offline validation permits only temporary Python client stubs")
            elif command not in {Path(sys.executable).name, "python", "python3", "bash"}:
                raise RuntimeError(f"Offline validation blocked subprocess: {command}")
            if "-S" in argv or "-E" in argv:
                raise RuntimeError("Offline validation cannot disable child Python guards")
            if "-I" in argv:
                # prime_check intentionally runs a frozen, isolated source copy.
                script = Path(argv[argv.index("-I") + 2])
                if script.name != "primes.py" or not script.resolve().is_relative_to(Path(tempfile.gettempdir())):
                    raise RuntimeError("Offline validation blocked unguarded isolated Python")
                if script.read_text() not in isolated_sources:
                    raise RuntimeError("Offline validation permits only the frozen prime fixture sources")
            if command == "bash":
                # These tests use a shell function fixture, never a live curl.
                text = " ".join(map(os.fsdecode, argv))
                if "health_check.sh" in text and "BASH_FUNC_curl%%" not in environment:
                    raise RuntimeError("Offline validation requires the exported curl fixture")
                if "-c" in argv and "export -f curl" not in text:
                    if "snapshot-contract" not in argv or len(argv) < 6:
                        raise RuntimeError("Offline validation blocked an unreviewed shell command")
                    snapshot = Path(argv[-2]).read_text()
                    if "curl()" not in snapshot or "export -f curl" not in snapshot:
                        raise RuntimeError("Offline validation requires the persisted curl fixture")

    sys.addaudithook(audit)
    original = subprocess.Popen

    class GuardedPopen(original):
        def __init__(self, *args, **kwargs):
            environment = dict(os.environ if kwargs.get("env") is None else kwargs["env"])
            environment["TRACE_LAB_OFFLINE_GUARD_DIR"] = guard_dir
            environment["TRACE_LAB_DOCKER_TESTS"] = "0"
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            parts = [guard_dir, str(ROOT / "scripts"), str(ROOT)]
            if environment.get("PYTHONPATH"):
                parts.append(environment["PYTHONPATH"])
            environment["PYTHONPATH"] = os.pathsep.join(parts)
            kwargs["env"] = environment
            super().__init__(*args, **kwargs)

    subprocess.Popen = GuardedPopen


def docker_includes(path):
    """Evaluate the simple ordered whitelist rules used by this repository."""
    included = True
    candidates = [path, *[str(parent) for parent in Path(path).parents if str(parent) != "."]]
    for raw in (ROOT / ".dockerignore").read_text().splitlines():
        rule = raw.strip()
        if not rule or rule.startswith("#"):
            continue
        negate = rule.startswith("!")
        pattern = rule.lstrip("!").strip("/")
        patterns = [pattern]
        if pattern.startswith("**/"):
            patterns.append(pattern[3:])
        if any(fnmatch.fnmatchcase(candidate, pattern)
               for candidate in candidates for pattern in patterns):
            included = negate
    return included


def guard_checks():
    for event, args in (
        ("socket.connect", (None, ("203.0.113.1", 443))),
        ("socket.getaddrinfo", ("example.invalid", 443, 0, 0, 0)),
        ("subprocess.Popen", ("docker", ["docker", "run"], None, {"PATH": ""})),
        ("subprocess.Popen", ("codex", ["codex", "exec"], None, {"PATH": ""})),
    ):
        try:
            sys.audit(event, *args)
        except RuntimeError:
            continue
        raise RuntimeError(f"Offline guard failed its synthetic {event} denial check")
    sys.audit("socket.connect", None, ("127.0.0.1", 12345))
    sys.audit("socket.connect", None, "/tmp/offline-validation-test.sock")
    print("PASS synthetic outbound socket/DNS, Docker/native process guards; local IPC allowed")


def setup_checks():
    for name in (".gitignore", ".dockerignore", ".env.example", "README.md", "Dockerfile",
                 "Dockerfile.bun-sourcemap-agent", "trace_lab/__main__.py", "host_lab/__main__.py"):
        if not (ROOT / name).is_file():
            raise RuntimeError(f"Missing setup file: {name}")
    from trace_lab.env import load_env
    with patch.dict(os.environ, {}, clear=True):
        load_env(ROOT / ".env.example")
        if set(os.environ) != KEYS or any(os.environ.values()):
            raise RuntimeError(".env.example must contain exactly the seven blank provider keys")
    ignored = [".env", ".env.local", "trace_lab/.env", "runs/raw.jsonl", "trace_lab/runs/raw.jsonl",
               "__pycache__/test.pyc", "trace_lab/__pycache__/test.pyc", "trace_lab/test.pyc",
               ".venv/bin/python", "trace_lab/.venv/bin/python", ".cache/model.bin",
               "trace_lab/.cache/model.bin", "node_modules/test/index.js", ".DS_Store"]
    result = subprocess.run(["git", "check-ignore", "--no-index", "--stdin"],
                            input="\n".join(ignored) + "\n", capture_output=True, text=True, check=False)
    if result.returncode not in {0, 1} or set(result.stdout.splitlines()) != set(ignored):
        raise RuntimeError("Git ignore rules do not cover local credentials and generated artifacts")
    kept = [".env.example", "trace_lab/env.py",
            "host_lab/assets/bun-sourcemap-leak/tests/fixtures/variant_sources/private_server_modules/src/server/internal/envelope.ts",
            "trace_lab/assets/bun-sourcemap-leak/environment/src/generated/prompt-template.ts",
            "host_lab/assets/bun-sourcemap-leak/tests/fixtures/variant_sources/generated_policy/src/generated/private-policy.ts"]
    result = subprocess.run(["git", "check-ignore", "--no-index", "--stdin"],
                            input="\n".join(kept) + "\n", capture_output=True, text=True, check=False)
    if result.returncode != 1 or result.stdout:
        raise RuntimeError("Git ignore rules hide setup or intentional fixture source")
    tracked_ignored = subprocess.check_output(
        ["git", "ls-files", "-ci", "--exclude-standard", "-z"], text=True).split("\0")
    if any(tracked_ignored):
        raise RuntimeError("Already-tracked ignored artifacts: " + ", ".join(filter(None, tracked_ignored)))
    tracked = subprocess.check_output(["git", "ls-files", "-z"], text=True).split("\0")
    for dockerfile in ("Dockerfile", "Dockerfile.bun-sourcemap-agent"):
        if not docker_includes(dockerfile):
            raise RuntimeError(f"Build context excludes {dockerfile}")
        for line in (ROOT / dockerfile).read_text().splitlines():
            if not line.startswith("COPY ") or "--from=" in line:
                continue
            for source in shlex.split(line)[1:-1]:
                paths = [path for path in tracked if path == source or path.startswith(source + "/")]
                if not paths or any(not docker_includes(path) for path in paths):
                    raise RuntimeError(f"Build context excludes a COPY source: {source}")
    for path in ignored + [".env.example", "host_lab/anonymization_loop.py", "tests/test_env.py"]:
        if docker_includes(path):
            raise RuntimeError(f"Build context includes unnecessary or sensitive path: {path}")
    compiled = 0
    for path in tracked:
        if path.endswith(".py"):
            compile((ROOT / path).read_bytes(), path, "exec")
            compiled += 1
    compile(Path(__file__).read_bytes(), str(Path(__file__)), "exec")
    print(f"PASS setup files, seven blank environment keys, Git/Docker ignore probes, "
          f"zero tracked ignored artifacts, {compiled} tracked Python files", flush=True)


def cli_checks():
    from trace_lab.cli import parser
    from host_lab.__main__ import EXPERIMENTS, main
    with contextlib.redirect_stdout(io.StringIO()):
        parser().format_help()
        if main(["list"]) != 0:
            raise RuntimeError("Host catalog failed")
        for module, _ in EXPERIMENTS.values():
            importlib.import_module("host_lab." + module).parser().format_help()
    print(f"PASS trace CLI help, host catalog, {len(EXPERIMENTS)} runner help parsers")


def main():
    if sys.version_info < (3, 11):
        print("FAIL Python 3.11+ is required", file=sys.stderr)
        return 1
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    # No user's .env is loaded. Only parser/catalog functions run before tests.
    for name in list(os.environ):
        if name in KEYS or any(part in name.upper() for part in ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")):
            os.environ.pop(name, None)
    os.environ["TRACE_LAB_DOCKER_TESTS"] = "0"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    try:
        setup_checks()
        with tempfile.TemporaryDirectory(prefix="trace-lab-offline-") as directory:
            (Path(directory) / "sitecustomize.py").write_text(
                "from validate_offline import install_guards\ninstall_guards()\n")
            os.environ["TRACE_LAB_OFFLINE_GUARD_DIR"] = directory
            os.environ["HOME"] = directory
            os.environ["XDG_CONFIG_HOME"] = str(Path(directory) / "config")
            os.environ["XDG_CACHE_HOME"] = str(Path(directory) / "cache")
            install_guards()
            guard_checks()
            cli_checks()
            suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py")
            if suite.countTestCases() == 0:
                raise RuntimeError("No fixture tests discovered")
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            print(f"OFFLINE tests={result.testsRun} failures={len(result.failures)} "
                  f"errors={len(result.errors)} skipped={len(result.skipped)}", flush=True)
            return 0 if result.wasSuccessful() else 1
    except (OSError, RuntimeError, SyntaxError, ValueError) as error:
        print(f"FAIL {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
