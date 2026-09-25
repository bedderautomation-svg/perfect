import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from trace_lab.cli import native_command, parser
from trace_lab.native import (final_response_from_stream, invocation_succeeded,
                              session_id_from_stream, stream_artifact,
                              trace_artifact_kind, trace_artifact_path_matches,
                              trace_path_matches)
from trace_lab.observer import is_trace, watch_directory


SESSION_ID = "ses_199999999999example"
DB_PATH = ".local/share/opencode/opencode.db"


def text_event(text="done"):
    return {
        "type": "text", "sessionID": SESSION_ID,
        "part": {"type": "text", "text": text, "time": {"end": 1}},
    }


class OpenCodeCommandTests(unittest.TestCase):
    def args(self):
        return parser().parse_args([
            "run", "--client", "opencode", "--model", "gpt-test",
            "--condition", "hotel-booking-json",
        ])

    def test_initial_command_uses_headless_json_and_isolated_provider(self):
        command = native_command(self.args(), None)
        self.assertEqual(command[:3], ["opencode", "--pure", "run"])
        self.assertIn("--format", command)
        self.assertIn("json", command)
        self.assertIn("--auto", command)
        self.assertEqual(command[command.index("--model") + 1], "trace_lab/gpt-test")
        self.assertEqual(command[command.index("--dir") + 1], "/workspace")
        self.assertNotIn("--session", command)

    def test_resume_command_targets_exact_session(self):
        command = native_command(self.args(), SESSION_ID, resume=True)
        self.assertEqual(command[command.index("--session") + 1], SESSION_ID)

    def test_resume_requires_session_id(self):
        with self.assertRaisesRegex(ValueError, "OpenCode session ID"):
            native_command(self.args(), None, resume=True)

    def test_reasoning_effort_maps_to_native_variant(self):
        args = self.args()
        args.reasoning_effort = "high"
        command = native_command(args, None)
        self.assertEqual(command[command.index("--variant") + 1], "high")

    def test_process_supervisor_accepts_opencode(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "opencode"
            executable.write_text(
                f"#!{sys.executable}\nimport sys\nsys.stdin.buffer.read()\n"
                f"print({json.dumps(json.dumps(text_event()))})\n"
            )
            executable.chmod(0o700)
            result = subprocess.run(
                [sys.executable, "-m", "trace_lab.process_runner", "opencode"],
                input=b"prompt", capture_output=True, timeout=10,
                env={"PATH": temporary, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            records = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(records[0]["kind"], "started")
            self.assertEqual(records[-1]["kind"], "exited")
            self.assertEqual(records[-1]["returncode"], 0)


class OpenCodeStreamTests(unittest.TestCase):
    def test_native_stream_helpers(self):
        stream = [
            {"type": "step_start", "sessionID": SESSION_ID,
             "part": {"type": "step-start"}},
            text_event("finished"),
            {"type": "step_finish", "sessionID": SESSION_ID,
             "part": {"type": "step-finish"}},
        ]
        self.assertEqual(stream_artifact("opencode"), "opencode.jsonl")
        self.assertEqual(session_id_from_stream("opencode", stream), SESSION_ID)
        self.assertEqual(final_response_from_stream("opencode", stream), "finished")
        self.assertTrue(invocation_succeeded("opencode", stream))

    def test_error_stream_fails_even_after_text(self):
        stream = [text_event(), {"type": "error", "sessionID": SESSION_ID}]
        self.assertFalse(invocation_succeeded("opencode", stream))

    def test_completed_tool_is_successful_when_session_deletion_prevents_final_text(self):
        stream = [{
            "type": "tool_use", "sessionID": SESSION_ID,
            "part": {"type": "tool", "tool": "bash", "state": {"status": "completed"}},
        }]
        self.assertTrue(invocation_succeeded("opencode", stream))


class OpenCodeTraceTests(unittest.TestCase):
    def test_observer_recognizes_sqlite_store_and_auxiliary_logs(self):
        self.assertTrue(trace_path_matches(DB_PATH, SESSION_ID, "opencode"))
        self.assertTrue(trace_path_matches(DB_PATH + "-wal", SESSION_ID, "opencode"))
        self.assertFalse(trace_path_matches(".local/share/opencode/auth.json", SESSION_ID,
                                           "opencode"))
        self.assertTrue(is_trace("home", DB_PATH))
        self.assertFalse(is_trace("home", ".local/share/opencode/opencode.db-journal"))
        self.assertTrue(is_trace("home", ".local/share/opencode/log/opencode.log"))
        self.assertEqual(
            trace_artifact_kind(".local/share/opencode/log/opencode.log"), "debug_log"
        )
        self.assertTrue(trace_artifact_path_matches(
            ".local/share/opencode/log/opencode.log", SESSION_ID, "opencode"
        ))
        self.assertTrue(watch_directory("home", ".local"))
        self.assertTrue(watch_directory("home", ".local/share"))
        self.assertFalse(is_trace("home", ".config/opencode/opencode.json"))

    def test_image_pins_opencode_release(self):
        dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text()
        self.assertIn("ARG OPENCODE_VERSION=1.18.30", dockerfile)
        self.assertIn('opencode-ai@${OPENCODE_VERSION}', dockerfile)

    def test_final_inventory_includes_opencode_store(self):
        source = (Path(__file__).resolve().parent.parent / "trace_lab" / "observer.py").read_text()
        self.assertIn('os.walk(self.roots["home"]', source)


if __name__ == "__main__":
    unittest.main()
