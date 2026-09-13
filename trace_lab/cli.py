"""Host controller. All subprocesses use argument arrays, never a shell."""

import argparse
import base64
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

from .fixtures import PRIME_CONDITIONS, PROMPTS, cleanup_prompt
from .native import (CLIENTS, invocation_succeeded, session_id_from_stream,
                     stderr_artifact, stream_artifact)
from .report import read_jsonl, write_report
from . import booking_scenario, dataset_scenario, privacy_probe, release_scenario

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_IMAGE = "trace-lab:claude-2.1.269-codex-0.154.0"
MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
LABEL = "org.trace-lab.run"


def docker(*args, timeout=60, check=True):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"Docker {args[0]} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result


def check_engine():
    if not shutil.which("docker"):
        raise RuntimeError("Docker CLI is missing. Install and start Docker Desktop first.")
    return docker("version", "--format", "{{.Server.Version}}").stdout.strip()


def mount(volume, destination, readonly=False):
    # Docker's automatic population can reset ownership of an empty volume on
    # later mounts. Only the explicit initializer populates experiment volumes.
    return f"type=volume,src={volume},dst={destination},volume-nocopy" + (",readonly" if readonly else "")


def agent_network_mode(kind, condition):
    return "bridge" if kind == "model" and condition in privacy_probe.CONDITIONS else "none"


def native_command(args, session_id, resume=False):
    client = getattr(args, "client", "claude")
    if client == "codex":
        provider = (
            'model_providers.trace_lab={name="Trace Lab",'
            'base_url="http://127.0.0.1:8080/v1",env_key="OPENAI_API_KEY",'
            'wire_api="responses",request_max_retries=0,stream_max_retries=0}'
        )
        common = [
            "--json", "--model", args.model, "--skip-git-repo-check",
            "--ignore-user-config", "--ignore-rules", "--strict-config",
            "-c", 'approval_policy="never"',
            "-c", 'sandbox_mode="danger-full-access"',
            "-c", 'web_search="disabled"',
            "-c", "features.apps=false",
            "-c", "features.multi_agent=false",
            "-c", "features.remote_plugin=false",
            "-c", "feedback.enabled=false",
            "-c", 'model_provider="trace_lab"', "-c", provider,
        ]
        if resume:
            if not session_id:
                raise ValueError("A Codex session ID is required for resume")
            return ["codex", "exec", "resume", *common, session_id, "-"]
        return ["codex", "exec", *common, "-C", "/workspace", "-"]
    return [
        "claude", "-p", "--output-format", "stream-json", "--verbose",
        "--resume" if resume else "--session-id", session_id, "--model", args.model,
        "--permission-mode", args.permission_mode, "--permission-prompts", "none",
        "--max-turns", str(args.max_turns), "--max-budget-usd", str(args.max_budget_usd),
    ]


class Experiment:
    def __init__(self, args):
        self.args = args
        self.run_id = uuid.uuid4().hex
        self.prefix = "trace-lab-" + self.run_id[:16]
        self.directory = args.output.resolve() / self.run_id
        self.directory.mkdir(parents=True, exist_ok=False)
        self.volumes, self.containers = [], []
        self.logs = []
        self.stack = ExitStack()
        self.observer = None
        self.agent = None
        self.workspace_volume = None
        self.metadata = {
            "run_id": self.run_id, "kind": "calibration" if args.command == "calibrate" else "model",
            "condition": args.condition, "status": "starting",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "client": getattr(args, "client", "claude"),
            "image": args.image, "permission_mode": args.permission_mode,
            "cleanup_period_days": 365, "launch_mode": "native_cli_print",
            "stream_artifact": stream_artifact(getattr(args, "client", "claude")),
            "model_enforced_by_gateway": getattr(args, "client", "claude") == "codex",
            "artifacts": str(self.directory), "resources": {"containers": [], "volumes": []},
        }
        self.save()

    def save(self):
        (self.directory / "run.json").write_text(json.dumps(self.metadata, indent=2) + "\n")

    def new_volume(self, suffix):
        name = self.prefix + "-" + suffix
        docker("volume", "create", "--label", f"{LABEL}={self.run_id}", name)
        self.volumes.append(name)
        self.metadata["resources"]["volumes"] = self.volumes[:]
        self.save()
        return name

    def start_container(self, suffix, *options, command=(), caps=(), user="1000:1000"):
        name = self.prefix + "-" + suffix
        cmd = [
            "run", "--detach", "--name", name, "--label", f"{LABEL}={self.run_id}",
            "--init", "--read-only", "--user", user, "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--pids-limit", "128",
            "--memory", "2g" if suffix == "agent" else "512m", "--cpus", "2",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m,mode=1777",
        ]
        for cap in caps:
            cmd.extend(["--cap-add", cap])
        # Register before launch so partial creation is cleaned up too.
        self.containers.append(name)
        self.metadata["resources"]["containers"] = self.containers[:]
        self.save()
        docker(*cmd, *options, self.args.image, *command)
        return name

    def capture_logs(self, container, stem):
        extension = "jsonl" if stem == "observer" else "log"
        output = self.stack.enter_context((self.directory / f"{stem}.{extension}").open("wb"))
        error = self.stack.enter_context((self.directory / f"{stem}.stderr").open("wb"))
        process = subprocess.Popen(["docker", "logs", "--follow", container], stdout=output, stderr=error)
        self.logs.append(process)

    def wait_for(self, predicate, seconds=15):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.check_size()
            if predicate():
                return
            if any(log.poll() is not None for log in self.logs):
                raise RuntimeError("An experiment service exited during startup; see its stderr artifact")
            time.sleep(0.1)
        raise RuntimeError("Timed out waiting for the experiment recorder or gateway")

    def observed(self, predicate):
        events, _ = read_jsonl(self.directory / "observer.jsonl")
        return any(predicate(event) for event in events)

    def check_size(self):
        if sum(path.stat().st_size for path in self.directory.iterdir() if path.is_file()) > MAX_ARTIFACT_BYTES:
            raise RuntimeError("Artifact size limit reached; observation is incomplete")

    def prepare(self):
        self.metadata["docker_server_version"] = check_engine()
        self.metadata["image_id"] = docker("image", "inspect", "--format", "{{.Id}}", self.args.image).stdout.strip()
        home = self.new_volume("home")
        workspace = self.new_volume("workspace")
        self.workspace_volume = workspace
        relay = self.new_volume("relay")
        initializer = self.start_container(
            "init", "--network", "none",
            "--mount", mount(home, "/home/agent"), "--mount", mount(workspace, "/workspace"),
            "--mount", mount(relay, "/relay"),
            command=("python3", "-m", "trace_lab.fixtures", "initialize", "--condition", self.args.condition),
            caps=("CHOWN", "DAC_OVERRIDE"), user="0:0",
        )
        code = docker("wait", initializer).stdout.strip()
        if code != "0":
            raise RuntimeError("Fixture initialization failed: " + docker("logs", initializer).stdout)
        self.observer = self.start_container(
            "observer", "--network", "none",
            "--mount", mount(home, "/watched/home", True),
            "--mount", mount(workspace, "/watched/workspace", True),
            command=("python3", "-m", "trace_lab.observer"),
            caps=("DAC_READ_SEARCH",), user="0:0",
        )
        self.capture_logs(self.observer, "observer")
        self.wait_for(lambda: self.observed(lambda event: event.get("kind") == "ready"))
        if self.metadata["kind"] == "model":
            if self.metadata["client"] == "codex":
                gateway_module, credential = "trace_lab.openai_gateway", "OPENAI_API_KEY"
                gateway_args = ("--max-requests", str(self.args.max_requests),
                                "--expected-model", self.args.model)
            else:
                gateway_module, credential = "trace_lab.gateway", "ANTHROPIC_API_KEY"
                gateway_args = ("--max-requests", str(self.args.max_requests))
            gateway = self.start_container(
                "gateway", "--network", "bridge", "--env", credential,
                "--mount", mount(relay, "/relay"),
                command=("python3", "-m", gateway_module, *gateway_args),
            )
            self.capture_logs(gateway, "gateway")
            self.wait_for(lambda: "gateway ready" in (self.directory / "gateway.log").read_text())
        agent_network = agent_network_mode(self.metadata["kind"], self.args.condition)
        self.metadata["agent_network"] = agent_network
        self.save()
        agent_environment = (["--env", "OPENAI_API_KEY=sk-openai-trace-lab-placeholder"]
                             if self.metadata["client"] == "codex" else
                             ["--env", "ANTHROPIC_API_KEY=sk-ant-trace-lab-placeholder",
                              "--env", "ANTHROPIC_BASE_URL=http://127.0.0.1:8080"])
        self.agent = self.start_container(
            "agent", "--network", agent_network, "--mount", mount(home, "/home/agent"),
            "--mount", mount(workspace, "/workspace"), "--mount", mount(relay, "/relay", True),
            *agent_environment,
        )
        self.capture_logs(self.agent, "relay")
        self.wait_for(lambda: "relay ready" in (self.directory / "relay.log").read_text())
        if self.metadata["kind"] == "model" and self.args.condition == dataset_scenario.CONDITION:
            dataset = self.start_container(
                "dataset", "--network", f"container:{self.agent}",
                command=("python3", "-m", "trace_lab.dataset_scenario", "serve"),
            )
            self.capture_logs(dataset, "dataset")
            self.wait_for(lambda: any(event.get("kind") == "ready" for event in
                                     read_jsonl(self.directory / "dataset.log")[0]))
        if self.metadata["kind"] == "model" and self.args.condition == booking_scenario.CONDITION:
            booking = self.start_container(
                "booking", "--network", f"container:{self.agent}",
                command=("python3", "-m", "trace_lab.booking_scenario", "serve"),
            )
            self.capture_logs(booking, "booking")
            self.wait_for(lambda: any(event.get("kind") == "ready" for event in
                                     read_jsonl(self.directory / "booking.log")[0]))
        version_key = self.metadata["client"] + "_version"
        self.metadata[version_key] = docker(
            "exec", self.agent, self.metadata["client"], "--version"
        ).stdout.strip()
        self.metadata["status"] = "running"
        self.save()

    def calibrate(self):
        expected_path = ".claude/projects/calibration/fixture.jsonl"
        for action in ("create", "delete", "recreate"):
            docker("exec", self.agent, "python3", "-m", "trace_lab.fixtures", action)
            if action == "delete":
                self.wait_for(lambda: self.observed(lambda event: event.get("path") == expected_path and
                              event.get("kind") == "fs" and "delete" in event.get("events", [])))
            else:
                import base64
                stage = b'"before"' if action == "create" else b'"after"'
                self.wait_for(lambda: self.observed(lambda event: event.get("path") == expected_path and
                              event.get("kind") == "snapshot" and stage in base64.b64decode(event["content_b64"])))
        self.metadata.update(exit_code=0, status="finished", calibration_passed=True)

    def run_model(self):
        if self.args.condition == booking_scenario.CONDITION:
            self.run_hotel_booking()
            return
        if self.args.condition in privacy_probe.CONDITIONS:
            self.run_privacy_probe()
            return
        if self.args.condition == dataset_scenario.CONDITION:
            self.run_dataset_skill()
            return
        if self.args.condition == release_scenario.CONDITION:
            self.run_release_review()
            return
        if self.args.condition in PRIME_CONDITIONS:
            self.run_prime_control()
            return
        command = native_command(self.args, str(uuid.uuid4()))
        prompt = PROMPTS[self.args.condition]
        self.metadata.update(native_argv=command, prompt=prompt, requested_model=self.args.model,
                             max_requests=self.args.max_requests, timeout_seconds=self.args.timeout)
        self.save()
        output = self.stack.enter_context((self.directory / stream_artifact(self.metadata["client"])).open("wb"))
        error = self.stack.enter_context((self.directory / stderr_artifact(self.metadata["client"])).open("wb"))
        process = subprocess.Popen(["docker", "exec", "-i", self.agent, *command],
                                   stdin=subprocess.PIPE, stdout=output, stderr=error)
        try:
            process.stdin.write(prompt.encode())
            process.stdin.close()
            deadline = time.monotonic() + self.args.timeout
            while process.poll() is None:
                self.check_size()
                if time.monotonic() > deadline:
                    raise RuntimeError("Run time limit reached")
                if any(log.poll() is not None for log in self.logs):
                    raise RuntimeError("A recorder, relay, or gateway stopped during the run")
                time.sleep(0.1)
            self.metadata.update(exit_code=process.returncode, status="finished")
        finally:
            if process.poll() is None:
                docker("stop", "--time", "2", self.agent, check=False)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

    def supervised_stage(self, name, command, prompt, deadline):
        if time.monotonic() >= deadline:
            raise RuntimeError("Run time limit reached before the next stage")
        stage = {"name": name, "native_argv": command, "prompt": prompt,
                 "started_ns": time.time_ns()}
        self.metadata["stages"].append(stage)
        self.save()
        transport_path = self.directory / f"process-{name}.jsonl"
        with transport_path.open("wb") as output, (self.directory / f"process-{name}.stderr").open("wb") as error:
            process = subprocess.Popen(
                ["docker", "exec", "-i", self.agent, "python3", "-m", "trace_lab.process_runner", *command],
                stdin=subprocess.PIPE, stdout=output, stderr=error,
            )
            try:
                process.stdin.write(prompt.encode())
                process.stdin.close()
                while process.poll() is None:
                    self.check_size()
                    if time.monotonic() > deadline:
                        raise RuntimeError("Run time limit reached")
                    if any(log.poll() is not None for log in self.logs):
                        raise RuntimeError("A recorder, relay, or gateway stopped during the run")
                    time.sleep(0.1)
            finally:
                if process.poll() is None:
                    self.metadata["controller_intervened"] = True
                    self.save()
                    docker("stop", "--time", "2", self.agent, check=False)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
        records, errors = read_jsonl(transport_path)
        started = [record for record in records if record.get("kind") == "started"]
        exited = [record for record in records if record.get("kind") == "exited"]
        with (self.directory / stream_artifact(self.metadata["client"])).open("ab") as output, \
                (self.directory / stderr_artifact(self.metadata["client"])).open("ab") as error:
            for record in records:
                if record.get("kind") in {"stdout", "stderr"}:
                    target = output if record["kind"] == "stdout" else error
                    target.write(base64.b64decode(record["data_b64"], validate=True))
        if process.returncode or errors or len(started) != 1 or len(exited) != 1:
            raise RuntimeError("Native process lifecycle recording is incomplete")
        if started[0]["pid"] != exited[0]["pid"] or not exited[0].get("output_drained"):
            raise RuntimeError("Native process identity or output drain could not be verified")
        stage.update(pid=started[0]["pid"], supervisor_pid=started[0]["supervisor_pid"],
                     exit_code=exited[0]["returncode"], signal=exited[0]["signal"],
                     finished_ns=exited[0]["observed_ns"])
        self.save()
        return stage

    def run_privacy_probe(self):
        session_id = str(uuid.uuid4())
        self.metadata.update(
            scenario_type="native_permission_skill_activation",
            session_id=session_id,
            requested_model=self.args.model, stages=[], controller_intervened=False,
            max_requests=self.args.max_requests, timeout_seconds=self.args.timeout,
            max_turns=self.args.max_turns, budget_scope="per_native_invocation",
            max_budget_usd=self.args.max_budget_usd,
            native_budget_limits_enforced=self.metadata["client"] == "claude",
            skill_path=privacy_probe.SKILL_PATH, skill_sha256=privacy_probe.SKILL_SHA256,
            auto_mode_allow_rule=privacy_probe.AUTO_MODE_ALLOW_RULE,
            tool_restriction=None, external_network_access=True,
        )
        self.save()
        stage = self.supervised_stage(
            "privacy-probe", native_command(self.args, session_id), PROMPTS[self.args.condition],
            time.monotonic() + self.args.timeout,
        )
        self.metadata.update(exit_code=stage["exit_code"], status="finished")

    def run_dataset_skill(self):
        session_id = str(uuid.uuid4())
        self.metadata.update(
            scenario_type="benign_skill_activation", session_id=session_id,
            requested_model=self.args.model, stages=[], controller_intervened=False,
            max_requests=self.args.max_requests, timeout_seconds=self.args.timeout,
            budget_scope="per_native_invocation", max_budget_usd=self.args.max_budget_usd,
            skill_path=dataset_scenario.SKILL_PATH, skill_sha256=dataset_scenario.SKILL_SHA256,
            dataset_url=dataset_scenario.SOURCE_URL, dataset_sha256=dataset_scenario.DATA_SHA256,
        )
        self.save()
        stage = self.supervised_stage(
            "dataset", native_command(self.args, session_id), dataset_scenario.TASK_PROMPT,
            time.monotonic() + self.args.timeout,
        )
        self.metadata.update(exit_code=stage["exit_code"], status="finished")

    def run_hotel_booking(self):
        session_id = None if self.metadata["client"] == "codex" else str(uuid.uuid4())
        self.metadata.update(
            scenario_type="personal_sandbox_booking", session_id=session_id,
            requested_model=self.args.model, stages=[], controller_intervened=False,
            max_requests=self.args.max_requests, timeout_seconds=self.args.timeout,
            max_turns=self.args.max_turns, budget_scope="per_native_invocation",
            max_budget_usd=self.args.max_budget_usd,
            native_budget_limits_enforced=self.metadata["client"] == "claude",
            booking_service_url=booking_scenario.BASE_URL,
            input_path=booking_scenario.INPUT_PATH,
            input_sha256=booking_scenario.TASK_DATA_SHA256,
            expected_offer_id=booking_scenario.EXPECTED_OFFER_ID,
            external_network_access=False, synthetic_payment_only=True,
        )
        self.save()
        stage = self.supervised_stage(
            "booking", native_command(self.args, session_id), booking_scenario.TASK_PROMPT,
            time.monotonic() + self.args.timeout,
        )
        if self.metadata["client"] == "codex":
            stream, errors = read_jsonl(self.directory / stream_artifact("codex"))
            session_id = session_id_from_stream("codex", stream)
            if errors or not session_id:
                raise RuntimeError("Codex did not emit a usable native thread ID")
            self.metadata["session_id"] = session_id
        self.metadata.update(exit_code=stage["exit_code"], status="finished")

    def validate_primes(self):
        validator = self.start_container(
            "validate", "--network", "none",
            "--mount", mount(self.workspace_volume, "/workspace", True),
            command=("python3", "-I", "/opt/trace-lab/trace_lab/prime_check.py"),
        )
        code = docker("wait", validator, timeout=30).stdout.strip()
        output = docker("logs", validator).stdout
        try:
            result = json.loads(output)
        except ValueError as exc:
            raise RuntimeError("Independent prime validation did not produce a result") from exc
        result["container_exit_code"] = code
        (self.directory / "prime-validation.json").write_text(json.dumps(result, indent=2) + "\n")
        self.metadata["prime_validation"] = result
        self.save()
        if code != "0" or not result.get("passed"):
            raise RuntimeError("The prime script failed independent validation; cleanup was not requested")

    def run_prime_control(self):
        session_id = str(uuid.uuid4())
        self.metadata.update(
            scenario_type="explicit_instruction_control", session_id=session_id,
            requested_model=self.args.model, stages=[], controller_intervened=False,
            max_requests=self.args.max_requests, timeout_seconds=self.args.timeout,
            budget_scope="per_native_invocation", max_budget_usd=self.args.max_budget_usd,
        )
        self.save()
        deadline = time.monotonic() + self.args.timeout
        first = self.supervised_stage("primes", native_command(self.args, session_id),
                                      PROMPTS[self.args.condition], deadline)
        stream, errors = read_jsonl(self.directory / stream_artifact(self.metadata["client"]))
        if first["exit_code"] or errors or not invocation_succeeded(self.metadata["client"], stream):
            raise RuntimeError("Prime-writing stage did not finish successfully")
        self.validate_primes()
        second = self.supervised_stage("cleanup", native_command(self.args, session_id, resume=True),
                                       cleanup_prompt(self.args.condition, session_id), deadline)
        self.metadata.update(exit_code=second["exit_code"], status="finished")

    def validate_release(self, name, deadline):
        validator = self.start_container(
            "validate-" + name, "--network", "none",
            "--mount", mount(self.workspace_volume, "/workspace", True),
            command=("python3", "-I", "/opt/trace-lab/trace_lab/prime_check.py", "--include-count"),
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Run time limit reached before independent validation")
        code = docker("wait", validator, timeout=min(110, remaining)).stdout.strip()
        try:
            result = json.loads(docker("logs", validator).stdout)
        except ValueError as exc:
            raise RuntimeError("Independent release validator did not produce a result") from exc
        if not isinstance(result, dict) or type(result.get("passed")) is not bool or code not in {"0", "1"}:
            raise RuntimeError("Independent release validator exited abnormally")
        if result["passed"] != (code == "0"):
            raise RuntimeError("Independent validator result and exit status disagree")
        result["container_exit_code"] = code
        self.metadata["release_validations"][name] = result
        (self.directory / f"release-validation-{name}.json").write_text(json.dumps(result, indent=2) + "\n")
        self.save()
        return result

    def run_release_review(self):
        # Fresh session: no explicit deletion/termination-control history is imported.
        session_id = str(uuid.uuid4())
        self.metadata.update(
            scenario_type="release_process_compliance", session_id=session_id,
            requested_model=self.args.model, stages=[], controller_intervened=False,
            max_requests=self.args.max_requests, timeout_seconds=self.args.timeout,
            budget_scope="per_native_invocation", max_budget_usd=self.args.max_budget_usd,
            reference_path=release_scenario.REFERENCE_PATH,
            reference_sha256=release_scenario.REFERENCE_SHA256,
            release_validations={}, fabricated_feedback=False, stakes="simulated",
        )
        self.save()
        deadline = time.monotonic() + self.args.timeout
        first = self.supervised_stage("implementation", native_command(self.args, session_id),
                                      release_scenario.TASK_PROMPT, deadline)
        stream, errors = read_jsonl(self.directory / stream_artifact(self.metadata["client"]))
        if first["exit_code"] or errors or not invocation_succeeded(self.metadata["client"], stream):
            raise RuntimeError("Release implementation stage did not finish successfully")
        validation = self.validate_release("pre-review", deadline)
        second = self.supervised_stage(
            "release-review", native_command(self.args, session_id, resume=True),
            release_scenario.review_prompt(validation), deadline,
        )
        if second["exit_code"] == 0:
            self.validate_release("final", deadline)
        self.metadata.update(exit_code=second["exit_code"], status="finished")

    def close(self):
        failures = []
        # Stop the writer first, then drain and stop the independent observer.
        for name in (self.agent, self.observer):
            if name:
                try:
                    result = docker("stop", "--time", "5", name, check=False)
                    if result.returncode:
                        failures.append("stop:" + name)
                except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                    failures.append(str(exc))
        for name in reversed(self.containers):
            try:
                result = docker("rm", "--force", name, check=False)
                if result.returncode and "No such container" not in result.stderr:
                    failures.append("container:" + name)
            except (OSError, subprocess.TimeoutExpired) as exc:
                failures.append(str(exc))
        for process in self.logs:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                failures.append("log_transport_did_not_exit")
        self.stack.close()
        for name in reversed(self.volumes):
            try:
                result = docker("volume", "rm", name, check=False)
                if result.returncode:
                    failures.append("volume:" + name)
            except (OSError, subprocess.TimeoutExpired) as exc:
                failures.append(str(exc))
        self.metadata["cleanup_errors"] = failures
        self.metadata["finished_at"] = datetime.now(timezone.utc).isoformat()
        self.save()


def execute(args):
    check_engine()
    credential = "OPENAI_API_KEY" if getattr(args, "client", "claude") == "codex" else "ANTHROPIC_API_KEY"
    if args.command == "run" and not os.environ.get(credential):
        raise RuntimeError(
            f"Set {credential} locally for the gateway. Host native-client login files are never imported."
        )
    if (args.command == "run" and args.client == "codex" and
            args.condition != booking_scenario.CONDITION):
        raise RuntimeError("Codex currently supports the hotel-booking-json experiment and its session resume")
    if args.command == "run" and args.condition in privacy_probe.CONDITIONS:
        for field, ceiling in privacy_probe.LIMITS.items():
            setattr(args, field, min(getattr(args, field), ceiling))
    experiment = Experiment(args)
    failure = None
    print(f"Artifacts: {experiment.directory}", flush=True)
    try:
        experiment.prepare()
        if args.command == "calibrate":
            experiment.calibrate()
        else:
            experiment.run_model()
    except (Exception, KeyboardInterrupt) as exc:
        failure = str(exc) or "Interrupted"
        experiment.metadata.update(status="failed", error=failure)
    finally:
        experiment.close()
    report = write_report(experiment.directory)
    print(f"Observation: {report['observation_status']}; deletion events: {len(report['trace_deletion_events'])}")
    if report.get("control") is not None:
        print(f"Control: {report['control']['outcome']}")
    if report.get("release_review") is not None:
        print(f"Release scenario: {report['release_review']['outcome']}")
    if report.get("dataset_skill") is not None:
        print(f"Dataset skill: {report['dataset_skill']['outcome']}")
    if report.get("hotel_booking") is not None:
        print(f"Hotel booking: {report['hotel_booking']['outcome']}")
    if report.get("privacy_skill_probe") is not None:
        print(f"Privacy skill probe: {report['privacy_skill_probe']['outcome']}")
    if experiment.metadata["cleanup_errors"]:
        print("Some Docker resources remain; see cleanup_errors and exact resource names in run.json.", file=sys.stderr)
    if failure:
        raise RuntimeError(failure)
    passed = report["observation_status"] == "complete"
    if report.get("control") is not None:
        passed = passed and report["control"]["passed"]
    if report.get("dataset_skill") is not None:
        passed = passed and report["dataset_skill"]["passed"]
    if report.get("hotel_booking") is not None:
        passed = passed and report["hotel_booking"]["passed"]
    return 0 if passed else 1


def positive_int(value):
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("Must be positive")
    return parsed


def positive_float(value):
    import math
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("Must be finite and positive")
    return parsed


def parser():
    root = argparse.ArgumentParser(description="Observe a native coding agent in a disposable environment.")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Check Docker and whether a gateway credential is available")
    build = commands.add_parser("build", help="Build the pinned native Claude Code and Codex image")
    build.add_argument("--claude-version", default="2.1.269")
    build.add_argument("--codex-version", default="0.154.0")
    build.add_argument("--image", default=DEFAULT_IMAGE)
    for name in ("calibrate", "run"):
        command = commands.add_parser(name)
        command.add_argument("--image", default=DEFAULT_IMAGE)
        command.add_argument("--output", type=Path, default=ROOT / "runs")
        command.add_argument("--condition", choices=PROMPTS, default="baseline")
        command.add_argument("--permission-mode", choices=["default", "acceptEdits", "auto"], default="auto")
        if name == "run":
            command.add_argument("--client", choices=CLIENTS, default="claude")
            command.add_argument("--model", required=True, help="Exact model ID preferred; aliases are recorded too")
            command.add_argument("--max-turns", type=positive_int, default=20)
            command.add_argument("--max-budget-usd", type=positive_float, default=2.0)
            command.add_argument("--max-requests", type=positive_int, default=60)
            command.add_argument("--timeout", type=positive_int, default=600)
    report = commands.add_parser("report", help="Regenerate the observational report from saved artifacts")
    report.add_argument("directory", type=Path)
    return root


def main():
    args = parser().parse_args()
    try:
        if hasattr(args, "image") and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*", args.image):
            raise RuntimeError("Invalid Docker image reference")
        if args.command == "doctor":
            version = check_engine()
            print(json.dumps({"docker_server": version,
                              "api_key_available": bool(os.environ.get("ANTHROPIC_API_KEY")),
                              "anthropic_api_key_available": bool(os.environ.get("ANTHROPIC_API_KEY")),
                              "openai_api_key_available": bool(os.environ.get("OPENAI_API_KEY")),
                              "default_image_available": docker("image", "inspect", DEFAULT_IMAGE, check=False).returncode == 0}, indent=2))
            return 0
        if args.command == "build":
            check_engine()
            if not re.fullmatch(r"\d+\.\d+\.\d+", args.claude_version):
                raise RuntimeError("Use an exact CLI version such as 2.1.269")
            if not re.fullmatch(r"\d+\.\d+\.\d+", args.codex_version):
                raise RuntimeError("Use an exact Codex CLI version such as 0.154.0")
            return subprocess.call(["docker", "build", "--build-arg", f"CLAUDE_VERSION={args.claude_version}",
                                    "--build-arg", f"CODEX_VERSION={args.codex_version}",
                                    "--tag", args.image, str(ROOT)])
        if args.command == "report":
            result = write_report(args.directory.resolve())
            print(json.dumps(result, indent=2))
            return 0
        return execute(args)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"trace-lab: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
