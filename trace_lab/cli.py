"""Host controller. All subprocesses use argument arrays, never a shell."""

import argparse
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

from .fixtures import PROMPTS
from .report import read_jsonl, write_report

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_IMAGE = "trace-lab:claude-2.1.269"
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


def native_command(args, session_id):
    return [
        "claude", "-p", "--output-format", "stream-json", "--verbose",
        "--session-id", session_id, "--model", args.model,
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
        self.metadata = {
            "run_id": self.run_id, "kind": "calibration" if args.command == "calibrate" else "model",
            "condition": args.condition, "status": "starting",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "image": args.image, "permission_mode": args.permission_mode,
            "cleanup_period_days": 365, "launch_mode": "native_cli_print",
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
            gateway = self.start_container(
                "gateway", "--network", "bridge", "--env", "ANTHROPIC_API_KEY",
                "--mount", mount(relay, "/relay"),
                command=("python3", "-m", "trace_lab.gateway", "--max-requests", str(self.args.max_requests)),
            )
            self.capture_logs(gateway, "gateway")
            self.wait_for(lambda: "gateway ready" in (self.directory / "gateway.log").read_text())
        self.agent = self.start_container(
            "agent", "--network", "none", "--mount", mount(home, "/home/agent"),
            "--mount", mount(workspace, "/workspace"), "--mount", mount(relay, "/relay", True),
            "--env", "ANTHROPIC_API_KEY=sk-ant-trace-lab-placeholder",
            "--env", "ANTHROPIC_BASE_URL=http://127.0.0.1:8080",
        )
        self.capture_logs(self.agent, "relay")
        self.wait_for(lambda: "relay ready" in (self.directory / "relay.log").read_text())
        self.metadata["claude_version"] = docker("exec", self.agent, "claude", "--version").stdout.strip()
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
        command = native_command(self.args, str(uuid.uuid4()))
        prompt = PROMPTS[self.args.condition]
        self.metadata.update(native_argv=command, prompt=prompt, requested_model=self.args.model,
                             max_requests=self.args.max_requests, timeout_seconds=self.args.timeout)
        self.save()
        output = self.stack.enter_context((self.directory / "claude.jsonl").open("wb"))
        error = self.stack.enter_context((self.directory / "claude.stderr").open("wb"))
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
    if args.command == "run" and not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("Set ANTHROPIC_API_KEY locally for the gateway. Host Claude login files are never imported.")
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
    if experiment.metadata["cleanup_errors"]:
        print("Some Docker resources remain; see cleanup_errors and exact resource names in run.json.", file=sys.stderr)
    if failure:
        raise RuntimeError(failure)
    return 0 if report["observation_status"] == "complete" else 1


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
    root = argparse.ArgumentParser(description="Observe native Claude Code in a disposable environment.")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Check Docker and whether a gateway credential is available")
    build = commands.add_parser("build", help="Build the pinned native Claude Code image")
    build.add_argument("--claude-version", default="2.1.269")
    build.add_argument("--image", default=DEFAULT_IMAGE)
    for name in ("calibrate", "run"):
        command = commands.add_parser(name)
        command.add_argument("--image", default=DEFAULT_IMAGE)
        command.add_argument("--output", type=Path, default=ROOT / "runs")
        command.add_argument("--condition", choices=PROMPTS, default="baseline")
        command.add_argument("--permission-mode", choices=["default", "acceptEdits", "auto"], default="auto")
        if name == "run":
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
                              "default_image_available": docker("image", "inspect", DEFAULT_IMAGE, check=False).returncode == 0}, indent=2))
            return 0
        if args.command == "build":
            check_engine()
            if not re.fullmatch(r"\d+\.\d+\.\d+", args.claude_version):
                raise RuntimeError("Use an exact CLI version such as 2.1.269")
            return subprocess.call(["docker", "build", "--build-arg", f"CLAUDE_VERSION={args.claude_version}",
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
