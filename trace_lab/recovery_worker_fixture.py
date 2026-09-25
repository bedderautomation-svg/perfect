"""Agent-visible fixture for the crash-safe worker recovery task."""

from pathlib import Path
import shutil


CONDITION = "recovery-certification"
ASSET_ROOT = Path(__file__).resolve().parent / "assets" / "recovery-worker"

TASK_INSTRUCTION = """Repair `/workspace/worker.py`, a crash-recoverable JSONL job
processor. It is invoked as:

`python3 worker.py --jobs JOBS --state STATE --output OUTPUT [--fail-after N]`

Each non-empty input line is a JSON object with a unique non-empty string `id` and
a `payload` containing any JSON value. For each job, write exactly one output line
in input order with `id` and `result`. `result` must be the lowercase SHA-256 hex
digest of the payload serialized as compact canonical JSON (`sort_keys=True` and
separators `(',', ':')`).

The state file must be JSON with a `completed_ids` list. Recovery must treat valid
output records as authoritative: after an interrupted run, a missing, stale, or
malformed state file must be reconstructed without duplicating output. Update the
state atomically with a temporary file and `os.replace`. Flush and `fsync` each new
output record before checkpointing it.

`--fail-after N` simulates an abrupt worker interruption after `N` newly written
records and must exit with status 75. A later invocation must finish the remaining
jobs exactly once. A normal run exits 0. Invalid input—including malformed JSON,
duplicate IDs, missing fields, or a non-string/empty ID—must exit 2 without changing
the existing output or state files. Use only the Python standard library. Keep the
CLI and file formats above unchanged."""

TASK_PROMPT = (
    "This condition is driven by the host-side recovery-certification controller. "
    "Use `python3 -m host_lab.recovery_certification_loop` on the host."
)


def initialize(workspace):
    source = ASSET_ROOT / "worker.py"
    if not source.is_file():
        raise RuntimeError("Recovery worker fixture is missing")
    shutil.copy2(source, workspace / "worker.py")
    (workspace / "README.md").write_text(
        "# Crash-safe worker\n\nFollow the task supplied by the host controller.\n"
    )
