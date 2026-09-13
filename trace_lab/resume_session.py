"""Rehydrate a recorded native Claude session into a fresh observed sandbox."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
import time

from .cli import (CODEX_AUTH_MODES, DEFAULT_IMAGE, Experiment, docker, native_command,
                  parser as main_parser)
from .codex_auth import validate_auth_cache
from .native import trace_path_matches
from .report import read_jsonl, write_report


def verified_content(event):
    try:
        content = base64.b64decode(event["content_b64"], validate=True)
        return content if hashlib.sha256(content).hexdigest() == event["sha256"] else None
    except (KeyError, TypeError, ValueError):
        return None


def _decode_jsonl(transcript):
    records = []
    for number, raw in enumerate(transcript.splitlines(keepends=True), 1):
        if raw.endswith(b"\r\n"):
            payload, ending = raw[:-2], b"\r\n"
        elif raw.endswith(b"\n"):
            payload, ending = raw[:-1], b"\n"
        else:
            payload, ending = raw, b""
        try:
            records.append([json.loads(payload), ending, payload])
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Resume input has invalid JSON on line {number}: {exc}") from exc
    if not records:
        raise RuntimeError("Resume input is empty")
    return records


def _rewrite_claude_initial_task_prompt(transcript, replacement):
    """Rewrite the initial SDK prompt and its stale native metadata records."""
    if not isinstance(replacement, str) or not replacement:
        raise RuntimeError("Replacement task prompt must be a non-empty string")
    records = _decode_jsonl(transcript)
    roots = [index for index, (event, _, _) in enumerate(records)
             if event.get("type") == "user" and event.get("parentUuid") is None
             and event.get("message", {}).get("role") == "user"
             and isinstance(event.get("message", {}).get("content"), str)]
    if len(roots) != 1:
        raise RuntimeError(
            f"Expected exactly one root user prompt in resume input; found {len(roots)}"
        )
    root_index = roots[0]
    root = records[root_index][0]
    session_id = root.get("sessionId")
    original = root["message"]["content"]
    boundary = len(records)
    for index in range(root_index + 1, len(records)):
        event = records[index][0]
        same_session = not session_id or event.get("sessionId") == session_id
        queued_prompt = (event.get("type") == "queue-operation"
                         and event.get("operation") == "enqueue"
                         and isinstance(event.get("content"), str))
        sdk_prompt = (event.get("type") == "user"
                      and event.get("promptSource") == "sdk"
                      and isinstance(event.get("message", {}).get("content"), str))
        if same_session and (queued_prompt or sdk_prompt):
            boundary = index
            break

    changed = {"queue_records": 0, "root_user_records": 0, "last_prompt_records": 0}
    changed_indices = set()
    for index in range(root_index):
        event = records[index][0]
        if (event.get("type") == "queue-operation" and event.get("operation") == "enqueue"
                and (not session_id or event.get("sessionId") == session_id)
                and isinstance(event.get("content"), str)):
            if event["content"] != replacement:
                event["content"] = replacement
                changed["queue_records"] += 1
                changed_indices.add(index)
    if original != replacement:
        root["message"]["content"] = replacement
        changed["root_user_records"] = 1
        changed_indices.add(root_index)
    last_prompt_records_seen = 0
    for index in range(root_index + 1, boundary):
        event = records[index][0]
        if (event.get("type") == "last-prompt"
                and (not session_id or event.get("sessionId") == session_id)
                and isinstance(event.get("lastPrompt"), str)):
            last_prompt_records_seen += 1
            if event["lastPrompt"] != replacement:
                event["lastPrompt"] = replacement
                changed["last_prompt_records"] += 1
                changed_indices.add(index)

    rendered = b"".join(
        (json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode()
         if index in changed_indices else payload) + ending
        for index, (event, ending, payload) in enumerate(records)
    )
    audit = {
        "original_prompt": original,
        "replacement_prompt": replacement,
        "records_changed": sum(changed.values()),
        "changed_by_type": changed,
        "initial_last_prompt_records_seen": last_prompt_records_seen,
        "sha256_before": hashlib.sha256(transcript).hexdigest(),
        "sha256_after": hashlib.sha256(rendered).hexdigest(),
        "size_before": len(transcript),
        "size_after": len(rendered),
    }
    return rendered, audit


def _codex_message_texts(event):
    payload = event.get("payload", {})
    if (event.get("type") == "response_item" and payload.get("type") == "message"
            and payload.get("role") == "user"):
        return [block.get("text") for block in payload.get("content", [])
                if isinstance(block, dict) and block.get("type") == "input_text"
                and isinstance(block.get("text"), str)]
    if event.get("type") == "event_msg" and payload.get("type") == "item_completed":
        item = payload.get("item", {})
        if item.get("type") == "UserMessage":
            return [block.get("text") for block in item.get("content", [])
                    if isinstance(block, dict) and block.get("type") == "text"
                    and isinstance(block.get("text"), str)]
    return []


def _replace_codex_text(event, original, replacement):
    changed = False
    payload = event.get("payload", {})
    containers = []
    if (event.get("type") == "response_item" and payload.get("type") == "message"
            and payload.get("role") == "user"):
        containers.append(payload.get("content", []))
    if event.get("type") == "event_msg" and payload.get("type") == "item_completed":
        item = payload.get("item", {})
        if item.get("type") == "UserMessage":
            containers.append(item.get("content", []))
    for content in containers:
        for block in content:
            if not isinstance(block, dict):
                continue
            for key in ("text",):
                if block.get(key) == original:
                    block[key] = replacement
                    changed = True
    return changed


def _rewrite_codex_initial_task_prompt(transcript, replacement):
    records = _decode_jsonl(transcript)
    candidates = []
    for index, (event, _, _) in enumerate(records):
        texts = [text for text in _codex_message_texts(event)
                 if not text.lstrip().startswith("<environment_context>")]
        if event.get("type") == "response_item" and texts:
            candidates.append((index, texts[0]))
    if not candidates:
        raise RuntimeError("Expected an initial Codex user task prompt in resume input")
    root_index, original = candidates[0]
    boundary = next((index for index, _ in candidates[1:]), len(records))
    changed_indices = set()
    changed = {"response_item_records": 0, "event_msg_records": 0}
    for index in range(root_index, boundary):
        event = records[index][0]
        if _replace_codex_text(event, original, replacement):
            changed_indices.add(index)
            changed[("response_item_records" if event.get("type") == "response_item"
                     else "event_msg_records")] += 1
    rendered = b"".join(
        (json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode()
         if index in changed_indices else payload) + ending
        for index, (event, ending, payload) in enumerate(records)
    )
    audit = {
        "client": "codex",
        "original_prompt": original,
        "replacement_prompt": replacement,
        "records_changed": sum(changed.values()),
        "changed_by_type": changed,
        "sha256_before": hashlib.sha256(transcript).hexdigest(),
        "sha256_after": hashlib.sha256(rendered).hexdigest(),
        "size_before": len(transcript),
        "size_after": len(rendered),
    }
    return rendered, audit


def rewrite_initial_task_prompt(transcript, replacement, client="claude"):
    """Rewrite the first user task and redundant native metadata representations."""
    if not isinstance(replacement, str) or not replacement:
        raise RuntimeError("Replacement task prompt must be a non-empty string")
    if client == "claude":
        return _rewrite_claude_initial_task_prompt(transcript, replacement)
    if client == "codex":
        return _rewrite_codex_initial_task_prompt(transcript, replacement)
    raise RuntimeError(f"Unsupported native client: {client}")


def source_details(source_run, source_trace=None, allow_modified=False, replacement_prompt=None):
    source_run = Path(source_run).resolve()
    metadata = json.loads((source_run / "run.json").read_text())
    report = json.loads((source_run / "report.json").read_text())
    client = metadata.get("client", "claude")
    trace = report.get("native_trace", {})
    session_id = metadata.get("session_id") or report.get("session_id")
    source_path = PurePosixPath(trace.get("source_path", ""))
    if (not trace.get("exported") or
            not trace_path_matches(source_path.as_posix(), session_id, client)):
        raise RuntimeError("Source run does not describe a safe exported native session")
    input_path = Path(source_trace).resolve() if source_trace else source_run / trace["path"]
    if not input_path.is_file() or input_path.is_symlink():
        raise RuntimeError("Resume input must be a regular native-session JSONL file")
    source_transcript = input_path.read_bytes()
    source_digest = hashlib.sha256(source_transcript).hexdigest()
    input_modified = (source_digest != trace.get("sha256")
                      or len(source_transcript) != trace.get("size_bytes"))
    if input_modified and not allow_modified:
        raise RuntimeError("Exported native session does not match its recorded digest and size")
    transcript, rewrite = ((source_transcript, None) if replacement_prompt is None else
                           rewrite_initial_task_prompt(source_transcript, replacement_prompt, client))
    digest = hashlib.sha256(transcript).hexdigest()
    modified = digest != trace.get("sha256") or len(transcript) != trace.get("size_bytes")
    if not isinstance(metadata.get("condition"), str) or not metadata["condition"]:
        raise RuntimeError("Source run condition is unavailable")
    return {
        "directory": source_run,
        "metadata": metadata,
        "report": report,
        "client": client,
        "session_id": session_id,
        "source_path": source_path,
        "input_path": input_path,
        "source_transcript": source_transcript,
        "source_sha256": source_digest,
        "transcript": transcript,
        "sha256": digest,
        "recorded_sha256": trace.get("sha256"),
        "recorded_size": trace.get("size_bytes"),
        "input_modified_from_recorded": input_modified,
        "modified_from_recorded": modified,
        "prompt_rewrite": rewrite,
    }


def final_workspace_artifacts(source_run):
    events, errors = read_jsonl(Path(source_run) / "observer.jsonl")
    if errors:
        raise RuntimeError("Source observer log is incomplete: " + ", ".join(errors))
    artifacts = {}
    for event in events:
        if event.get("kind") != "final_artifact" or not event.get("readable"):
            continue
        relative = PurePosixPath(event.get("path", ""))
        if (relative.is_absolute() or not relative.parts or
                any(part in {"", ".", ".."} for part in relative.parts)):
            raise RuntimeError("Source run contains an unsafe workspace artifact path")
        content = verified_content(event)
        if content is None:
            raise RuntimeError(f"Workspace artifact failed digest verification: {relative}")
        artifacts[relative.as_posix()] = content
    return artifacts


def copy_into_container(container, source, destination):
    docker("exec", container, "mkdir", "-p", str(PurePosixPath(destination).parent))
    source.chmod(0o666)
    docker("cp", str(source), f"{container}:{destination}")


def seed(experiment, details):
    artifacts = final_workspace_artifacts(details["directory"])
    with tempfile.TemporaryDirectory(prefix="trace-resume-", dir="/tmp") as temporary:
        temporary = Path(temporary)
        transcript = temporary / "native-session.jsonl"
        transcript.write_bytes(details["transcript"])
        transcript_target = "/home/agent/" + details["source_path"].as_posix()
        copy_into_container(experiment.agent, transcript, transcript_target)
        for index, (relative, content) in enumerate(sorted(artifacts.items())):
            staged = temporary / f"artifact-{index}"
            staged.write_bytes(content)
            copy_into_container(experiment.agent, staged, "/workspace/" + relative)
    actual = docker("exec", experiment.agent, "sha256sum", transcript_target).stdout.split()[0]
    if actual != details["sha256"]:
        raise RuntimeError("Sandbox transcript digest differs from the exported source")
    return transcript_target, sorted(artifacts)


def run(args):
    details = source_details(
        args.source_run, args.source_trace, args.allow_modified_source, args.replace_task_prompt
    )
    condition = details["metadata"].get("condition")
    codex_auth = "api-key"
    if details["client"] == "codex":
        codex_auth = args.codex_auth or details["metadata"].get("codex_auth", "api-key")
    if details["client"] == "codex" and codex_auth == "subscription":
        validate_auth_cache(args.codex_auth_file)
    elif details["client"] == "codex" and not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("Set OPENAI_API_KEY locally for the gateway or use --codex-auth subscription.")
    elif details["client"] != "codex" and not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "Set ANTHROPIC_API_KEY locally for the gateway. Host Claude login files are never imported."
        )
    base = [
        "run", "--client", details["client"], "--model", args.model, "--condition", condition,
        "--image", args.image, "--output", str(args.output),
        "--max-turns", str(args.max_turns), "--max-budget-usd", str(args.max_budget_usd),
        "--max-requests", str(args.max_requests), "--timeout", str(args.timeout),
    ]
    if details["client"] == "codex":
        base.extend(["--codex-auth", codex_auth,
                     "--codex-auth-file", str(args.codex_auth_file)])
    experiment_args = main_parser().parse_args(base)
    experiment = Experiment(experiment_args)
    preserved_input = experiment.directory / "resume-input-native-session.jsonl"
    preserved_input.write_bytes(details["transcript"])
    preserved_input.chmod(0o444)
    preserved_source = None
    if details["prompt_rewrite"] is not None:
        preserved_source = experiment.directory / "resume-source-native-session.jsonl"
        preserved_source.write_bytes(details["source_transcript"])
        preserved_source.chmod(0o444)
    failure = None
    print(f"Artifacts: {experiment.directory}", flush=True)
    try:
        experiment.prepare()
        transcript_target, restored = seed(experiment, details)
        expected_relative = details["source_path"].as_posix()
        experiment.wait_for(lambda: experiment.observed(
            lambda event: event.get("kind") == "snapshot" and event.get("root") == "home"
            and event.get("path") == expected_relative and event.get("sha256") == details["sha256"]
        ))
        experiment.metadata.update(
            condition="session-resume", scenario_type="native_session_resume",
            source_condition=condition, session_id=details["session_id"],
            resumed_from_run_id=details["metadata"].get("run_id"),
            source_native_trace_path=str(details["input_path"]),
            source_file_native_trace_sha256=details["source_sha256"],
            source_native_trace_sha256=details["sha256"],
            source_native_trace_size=len(details["transcript"]),
            source_file_modified_from_recorded=details["input_modified_from_recorded"],
            source_modified_from_recorded=details["modified_from_recorded"],
            recorded_native_trace_sha256=details["recorded_sha256"],
            recorded_native_trace_size=details["recorded_size"],
            preserved_resume_input=str(preserved_input),
            preserved_resume_source=str(preserved_source) if preserved_source else None,
            task_prompt_rewrite=details["prompt_rewrite"],
            sandbox_native_trace_path=transcript_target,
            restored_workspace_artifacts=restored,
            requested_model=args.model, stages=[], controller_intervened=False,
            max_requests=args.max_requests, timeout_seconds=args.timeout,
            max_turns=args.max_turns, max_budget_usd=args.max_budget_usd,
            native_budget_limits_enforced=details["client"] == "claude",
            prompt=args.prompt,
        )
        experiment.save()
        stage = experiment.supervised_stage(
            "resume", native_command(experiment_args, details["session_id"], resume=True),
            args.prompt, time.monotonic() + args.timeout,
        )
        experiment.metadata.update(exit_code=stage["exit_code"], status="finished")
    except (Exception, KeyboardInterrupt) as exc:
        failure = str(exc) or "Interrupted"
        experiment.metadata.update(status="failed", error=failure)
    finally:
        experiment.close()
    report = write_report(experiment.directory)
    exported = report.get("native_trace", {})
    final_trace = ((experiment.directory / exported["path"]).read_bytes()
                   if exported.get("exported") else b"")
    resume_stage = next((stage for stage in experiment.metadata.get("stages", [])
                         if stage.get("name") == "resume"), {})
    verification = {
        "source_digest_verified_before_launch": experiment.observed(
            lambda event: event.get("kind") == "snapshot" and event.get("root") == "home"
            and event.get("path") == details["source_path"].as_posix()
            and event.get("sha256") == details["sha256"]
            and event.get("observed_ns", 0) < resume_stage.get("started_ns", 0)
        ),
        "source_bytes_preserved_as_prefix": final_trace.startswith(details["transcript"]),
        "followup_prompt_present_in_final_trace": args.prompt.encode() in final_trace,
        "final_trace_grew": len(final_trace) > len(details["transcript"]),
    }
    verification["passed"] = all(verification.values())
    experiment.metadata["resume_verification"] = verification
    experiment.save()
    report = write_report(experiment.directory)
    print(json.dumps({
        "observation_status": report["observation_status"],
        "resume_verification": verification,
        "native_trace": report.get("native_trace"),
    }, indent=2))
    if failure:
        raise RuntimeError(failure)
    return 0 if report["observation_status"] == "complete" and verification["passed"] else 1


def parser(default_prompt=None, description=__doc__):
    command = argparse.ArgumentParser(description=description)
    command.add_argument("--source-run", type=Path, required=True)
    command.add_argument("--source-trace", type=Path)
    command.add_argument("--allow-modified-source", action="store_true")
    command.add_argument(
        "--replace-task-prompt",
        help=("Rewrite every native representation of the initial task prompt in the preserved "
              "resume input; the source file is not changed"),
    )
    if default_prompt is None:
        command.add_argument("--prompt", required=True)
    else:
        command.set_defaults(prompt=default_prompt)
    command.add_argument("--model", required=True)
    command.add_argument("--codex-auth", choices=CODEX_AUTH_MODES,
                         help="Codex authentication mode; defaults to the source run's mode")
    command.add_argument("--codex-auth-file", type=Path,
                         default=Path.home() / ".codex" / "auth.json")
    command.add_argument("--image", default=DEFAULT_IMAGE)
    command.add_argument("--output", type=Path, default=Path(__file__).resolve().parent.parent / "runs")
    command.add_argument("--max-turns", type=int, default=20)
    command.add_argument("--max-budget-usd", type=float, default=2.0)
    command.add_argument("--max-requests", type=int, default=60)
    command.add_argument("--timeout", type=int, default=600)
    return command


def main():
    return run(parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
