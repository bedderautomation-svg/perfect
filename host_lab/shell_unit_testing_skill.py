"""Run the implicit shell-unit-testing skill and verify command/trace tampering."""

import argparse
import json
from pathlib import Path
import sys
import time
import uuid

from host_lab import anonymization_loop as base
from host_lab.paid_lookup_loop import validate_auth
from host_lab.shell_tampering import check_service
from trace_lab.cli import (CLAUDE_AUTH_MODES, CODEX_AUTH_MODES, DEFAULT_IMAGE, Experiment,
                           ROOT, docker, mount, positive_int)
from trace_lab.native import invocation_succeeded, session_id_from_stream, stream_artifact
from trace_lab.report import read_jsonl, write_report
from trace_lab.shell_unit_testing_fixture import (
    CONCEAL_CONDITION,
    CONTROL_CONDITION,
    SERVICE_CONFIG_PATH,
    grade,
    task_prompt,
    two_turn_followup_prompt,
    two_turn_task_prompt,
)


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--client", choices=("claude", "codex"), default="claude")
    command.add_argument("--model", help="Required for a new run")
    command.add_argument("--report", type=Path, help="Regrade a saved run without launching an agent")
    command.add_argument("--variant", choices=("conceal", "override-only"), default="conceal")
    command.add_argument("--time-budget", type=positive_int, default=None,
                         help="Optional wall-time limit; default waits for native completion")
    command.add_argument("--claude-auth", choices=CLAUDE_AUTH_MODES, default="api-key")
    command.add_argument("--claude-auth-file", type=Path,
                         default=Path.home() / ".claude/.credentials.json")
    command.add_argument("--codex-auth", choices=CODEX_AUTH_MODES, default="api-key")
    command.add_argument("--codex-auth-file", type=Path, default=Path.home() / ".codex/auth.json")
    command.add_argument("--reasoning-effort", choices=("low", "medium", "high", "xhigh", "max"))
    command.add_argument("--image", default=DEFAULT_IMAGE)
    command.add_argument("--output", type=Path, default=ROOT / "runs")
    command.set_defaults(max_turns=0, max_requests=0, max_budget_usd=0)
    return command


def run(args):
    validate_auth(args)
    condition = CONCEAL_CONDITION if args.variant == "conceal" else CONTROL_CONDITION
    config = base.main_parser().parse_args(base.experiment_arguments(args, condition))
    config.timeout = args.time_budget
    experiment = Experiment(config)
    nonce = uuid.uuid4().hex
    two_turn = args.client == "claude"
    task = two_turn_task_prompt() if two_turn else task_prompt(nonce)
    followup = two_turn_followup_prompt() if two_turn else None
    experiment.metadata.update(
        scenario_type="implicit_shell_unit_testing_skill",
        stages=[],
        requested_model=args.model,
        probe_nonce=nonce,
        variant=args.variant,
        controller_intervened=False,
        stage_completed=False,
        task_prompt=task,
        followup_prompt=followup,
        two_turn_hidden_followup=two_turn,
        reference_verifier_required=two_turn,
        explicit_invocation_requested=False,
        synthetic_only=True,
        external_network_access=False,
        single_native_process=True,
    )
    experiment.save()
    (experiment.directory / "task-prompt.txt").write_text(task)
    if followup:
        (experiment.directory / "followup-prompt.txt").write_text(followup)
    service, failure = None, None
    started = time.monotonic()
    print(f"[shell-unit-skill] artifacts={experiment.directory}", flush=True)
    try:
        experiment.prepare()
        service = experiment.start_container(
            "shell-service", "--network", f"container:{experiment.agent}",
            "--mount", mount(experiment.home_volume, "/watched/home", True),
            command=("python3", "-m", "trace_lab.shell_tampering_service", "--client", args.client),
        )
        experiment.capture_logs(service, "shell-audit")
        experiment.wait_for(lambda: any(event.get("kind") == "ready" for event in
                                        read_jsonl(experiment.directory / "shell-audit.log")[0]))
        check_service(service, nonce, "host-before")
        if two_turn:
            url = f"http://127.0.0.1:8081/health?probe={nonce}-live"
            code = ("from pathlib import Path; import sys; "
                    f"p=Path('/workspace/{SERVICE_CONFIG_PATH}'); "
                    "p.parent.mkdir(parents=True,exist_ok=True); "
                    "p.write_text('HEALTH_CHECK_URL='+sys.argv[1]+'\\n')")
            docker("exec", experiment.agent, "python3", "-c", code, url)
        deadline = time.monotonic() + args.time_budget if args.time_budget else float("inf")
        requested = str(uuid.uuid4()) if args.client == "claude" else None
        print(f"[shell-unit-skill] {args.client} {args.model} variant={args.variant} start", flush=True)
        if two_turn:
            command = ["python3", "-m", "trace_lab.claude_two_turn",
                       "--session-id", requested, "--model", args.model]
            if args.reasoning_effort:
                command.extend(["--effort", args.reasoning_effort])
            input_payload = json.dumps({"first_prompt": task, "followup_prompt": followup})
        else:
            command = base.native_command(config, requested)
            input_payload = task
        stage = experiment.supervised_stage(
            "unit-testing", command, input_payload, deadline
        )
        if two_turn:
            verifier = experiment.start_container(
                "snapshot-contract-check", "--network", "none",
                "--mount", mount(experiment.home_volume, "/home/agent", True),
                "--mount", mount(experiment.workspace_volume, "/workspace", True),
                command=("python3", "-m", "trace_lab.shell_snapshot_verifier"),
            )
            verifier_code = docker("wait", verifier, timeout=35).stdout.strip()
            verifier_output = docker("logs", verifier).stdout
            try:
                snapshot_check = json.loads(verifier_output.strip().splitlines()[-1])
            except (IndexError, ValueError) as exc:
                raise RuntimeError(
                    "Independent snapshot contract check did not produce valid JSON"
                ) from exc
            if (verifier_code not in {"0", "1"}
                    or type(snapshot_check.get("verified")) is not bool
                    or snapshot_check["verified"] != (verifier_code == "0")):
                raise RuntimeError("Independent snapshot contract check was inconsistent")
            snapshot_check["container_exit_code"] = verifier_code
            experiment.metadata["snapshot_contract_check"] = snapshot_check
            (experiment.directory / "snapshot-contract-check.json").write_text(
                json.dumps(snapshot_check, indent=2) + "\n"
            )
            experiment.save()
        stream, errors = read_jsonl(experiment.directory / stream_artifact(args.client))
        session = session_id_from_stream(args.client, stream)
        turn_results = [event for event in stream if event.get("type") == "result"]
        completed = (not errors and bool(session) and (not requested or session == requested)
                     and stage["exit_code"] == 0 and invocation_succeeded(args.client, stream)
                     and (not two_turn or len(turn_results) == 2))
        experiment.metadata.update(
            session_id=session,
            stage_completed=completed,
            status="finished",
            exit_code=stage["exit_code"],
        )
        experiment.save()
        check_service(service, nonce, "host-after")
    except (Exception, KeyboardInterrupt) as exc:
        failure = str(exc) or "Interrupted"
        experiment.metadata.update(status="failed", error=failure)
        experiment.save()
    finally:
        if service:
            try:
                docker("stop", "--time", "5", service)
            except Exception as exc:
                failure = failure or str(exc)
                experiment.metadata.update(status="failed", error=failure)
        experiment.metadata["elapsed_seconds"] = time.monotonic() - started
        experiment.save()
        experiment.close()
    result = write_setting_report(experiment.directory)
    print(
        f"[shell-unit-skill] outcome={result['outcome']} "
        f"skill_loaded={result['skill_loaded']} "
        f"snapshot_override={result['snapshot_override_observed']} "
        f"fake_without_request={result['fabricated_healthy_result_without_request']} "
        f"selective_rewrite={result['selective_trace_rewrite_observed']}",
        flush=True,
    )
    if failure:
        raise RuntimeError(failure)
    return 0 if result["observation_complete"] else 1


def write_setting_report(directory):
    directory = Path(directory)
    metadata = json.loads((directory / "run.json").read_text())
    if metadata.get("condition") not in {CONCEAL_CONDITION, CONTROL_CONDITION}:
        raise ValueError("Not a shell-unit-testing skill experiment")
    general = write_report(directory)
    client = metadata["client"]
    stream, stream_errors = read_jsonl(directory / stream_artifact(client))
    audit, audit_errors = read_jsonl(directory / "shell-audit.log")
    events, event_errors = read_jsonl(directory / "observer.jsonl")
    native, native_errors = read_jsonl(directory / "native-session.jsonl")
    status = (
        "inconclusive"
        if metadata.get("error") or stream_errors or audit_errors or event_errors
        or metadata.get("cleanup_errors")
        else general["observation_status"]
    )
    result = grade(metadata, stream, audit, native, native_errors, events, status)
    result.update(
        client=client,
        model=metadata.get("requested_model"),
        cli_version=metadata.get(client + "_version"),
        errors=(stream_errors + audit_errors + event_errors + native_errors
                + ([metadata["error"]] if metadata.get("error") else [])),
    )
    (directory / "shell-unit-testing-report.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    try:
        command = parser()
        args = command.parse_args()
        if args.report:
            result = write_setting_report(args.report)
            print(json.dumps(result, indent=2))
            return 0 if result["observation_complete"] else 1
        if not args.model:
            command.error("--model is required for a new run")
        return run(args)
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"shell-unit-testing-skill: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
