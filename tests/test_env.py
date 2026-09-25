import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from trace_lab import cli
from trace_lab.env import load_env


class DotenvLoading(unittest.TestCase):
    def test_literals_and_shell_precedence(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"EXISTING": "shell", "EMPTY": ""}, clear=True
        ):
            path = Path(directory) / ".env"
            path.write_text(
                "# comment\nexport ANTHROPIC_API_KEY='dummy#key' # comment\n"
                'LITERAL="$(do-not-run) ${EXISTING}"\n'
                "EXISTING=file\nEMPTY=file\nVALUE=hello world # comment\n",
                encoding="utf-8",
            )
            load_env(path)
            self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "dummy#key")
            self.assertEqual(os.environ["LITERAL"], "$(do-not-run) ${EXISTING}")
            self.assertEqual(os.environ["EXISTING"], "shell")
            self.assertEqual(os.environ["EMPTY"], "")
            self.assertEqual(os.environ["VALUE"], "hello world")

    def test_missing_file_is_optional(self):
        with tempfile.TemporaryDirectory() as directory:
            load_env(Path(directory) / ".env")

    def test_invalid_file_does_not_expose_secrets_or_partially_load(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / ".env"
            for invalid in ("secret-without-assignment", 'KEY="secret', "KEY=secret\x00"):
                with self.subTest(invalid=invalid):
                    path.write_text("FIRST=value\n" + invalid, encoding="utf-8")
                    with self.assertRaises(RuntimeError) as error:
                        load_env(path)
                    self.assertNotIn("secret", str(error.exception))
                    self.assertIn("line 2", str(error.exception))
                    self.assertNotIn("FIRST", os.environ)

    def test_doctor_loads_repository_env_without_printing_key(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            (root / ".env").write_text("ANTHROPIC_API_KEY=dummy-secret\n", encoding="utf-8")
            output = io.StringIO()
            with patch.object(cli, "ROOT", root), patch("sys.argv", ["trace_lab", "doctor"]), \
                    patch.object(cli, "check_engine", return_value="test"), \
                    patch.object(cli, "docker") as docker, contextlib.redirect_stdout(output):
                docker.return_value.returncode = 0
                self.assertEqual(cli.main(), 0)
            self.assertIn('"api_key_available": true', output.getvalue())
            self.assertNotIn("dummy-secret", output.getvalue())
