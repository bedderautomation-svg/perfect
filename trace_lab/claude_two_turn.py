"""Feed two user turns to one Claude print-mode process using streaming JSON input."""

import argparse
import json
import subprocess
import sys


def user_message(text):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": text},
    ]}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max"))
    args = parser.parse_args()
    try:
        request = json.load(sys.stdin)
        first, followup = request["first_prompt"], request["followup_prompt"]
        if not all(isinstance(value, str) and value.strip() for value in (first, followup)):
            raise ValueError("Both prompts must be non-empty strings")
    except (KeyError, TypeError, ValueError) as exc:
        print(f"Invalid two-turn request: {exc}", file=sys.stderr)
        return 2

    command = [
        "claude", "-p", "--input-format", "stream-json",
        "--output-format", "stream-json", "--verbose", "--replay-user-messages",
        "--session-id", args.session_id, "--model", args.model,
        "--dangerously-skip-permissions",
    ]
    if args.effort:
        command.extend(["--effort", args.effort])
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    assert process.stdin is not None and process.stdout is not None

    def send(text):
        process.stdin.write((json.dumps(user_message(text), separators=(",", ":")) + "\n").encode())
        process.stdin.flush()

    send(first)
    results = 0
    malformed = False
    for line in iter(process.stdout.readline, b""):
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()
        try:
            event = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            malformed = True
            continue
        if event.get("type") != "result":
            continue
        results += 1
        if results == 1:
            if event.get("is_error") or event.get("subtype") != "success":
                process.stdin.close()
                break
            send(followup)
        elif results == 2:
            process.stdin.close()
            break
    # Drain anything emitted during orderly shutdown.
    for line in process.stdout:
        sys.stdout.buffer.write(line)
    sys.stdout.buffer.flush()
    if not process.stdin.closed:
        process.stdin.close()
    returncode = process.wait()
    if returncode:
        return returncode
    if malformed or results != 2:
        print(f"Expected two valid result events, observed {results}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
