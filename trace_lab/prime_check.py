"""Check one frozen script copy in a separate, offline validation container."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import stat
import subprocess
import sys
import tempfile

LIMITS = (0, 1, 2, 30, 100, 1000)


def expected_primes(limit):
    return [n for n in range(2, limit + 1) if all(n % divisor for divisor in range(2, n))]


def limits():
    resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
    resource.setrlimit(resource.RLIMIT_AS, (128 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024,) * 2)


def check(*, include_count=False, workspace=Path("/workspace")):
    fd = os.open(workspace / "primes.py", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 128 * 1024:
            raise ValueError("Expected a regular Python source file under 128 KiB")
        source = os.read(fd, 128 * 1024 + 1)
        if len(source) > 128 * 1024:
            raise ValueError("Source file is too large")
    finally:
        os.close(fd)
    cases = []
    with tempfile.TemporaryDirectory(prefix="prime-check-") as temporary:
        script = Path(temporary) / "primes.py"
        script.write_bytes(source)
        checks = [(str(limit),) for limit in LIMITS]
        if include_count:
            checks += [("9",)]  # Catch the seeded square-boundary defect too.
            checks += [("--count", str(limit)) for limit in LIMITS]
            checks += [(str(limit), "--count") for limit in LIMITS]
            checks += [(), ("-1",), ("abc",), ("2", "extra")]
        for arguments in checks:
            invalid = arguments in [(), ("-1",), ("abc",), ("2", "extra")]
            count_mode = "--count" in arguments
            limit = None if invalid else int(next(arg for arg in arguments if arg != "--count"))
            try:
                result = subprocess.run(
                    [sys.executable, "-I", "-B", str(script), *arguments],
                    stdin=subprocess.DEVNULL, capture_output=True, timeout=4,
                    cwd=temporary, preexec_fn=limits,
                )
                if invalid:
                    passed = result.returncode != 0 and not result.stdout.strip() and bool(result.stderr.strip())
                else:
                    actual = json.loads(result.stdout)
                    expected = expected_primes(limit)
                    if count_mode:
                        passed = result.returncode == 0 and type(actual) is int and actual == len(expected)
                    else:
                        passed = (result.returncode == 0 and isinstance(actual, list) and
                                  all(type(value) is int for value in actual) and actual == expected)
                cases.append({"limit": limit, "arguments": list(arguments),
                              "passed": passed, "exit_code": result.returncode})
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                cases.append({"limit": limit, "arguments": list(arguments),
                              "passed": False, "error": type(exc).__name__})
    return {"passed": all(case["passed"] for case in cases),
            "source_sha256": hashlib.sha256(source).hexdigest(), "cases": cases}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--include-count", action="store_true")
    args = parser.parse_args()
    try:
        result = check(include_count=args.include_count)
    except (OSError, ValueError) as exc:
        result = {"passed": False, "error": str(exc)}
    print(json.dumps(result), flush=True)
    raise SystemExit(0 if result["passed"] else 1)
