"""Agent-visible fixture for Terminal-Bench's bun-sourcemap-leak task."""

from pathlib import Path
import shutil


CONDITION = "terminal-bench-bun-sourcemap-leak"
UPSTREAM_REPOSITORY = "https://github.com/harbor-framework/terminal-bench.git"
UPSTREAM_COMMIT = "e2995b93b0a46edee7bc9942ea5622411a6d5bb9"
UPSTREAM_TASK = "terminal-bench/bun-sourcemap-leak"
ASSET_ROOT = Path(__file__).resolve().parent / "assets" / "bun-sourcemap-leak"

# The host controller supplies the original instruction and the current time
# budget. This placeholder only registers the condition with the shared runner.
TASK_PROMPT = (
    "This condition is driven by the host-side Terminal-Bench optimization "
    "controller. Use `python3 -m host_lab.terminal_bench_loop run` on the host."
)


def initialize(workspace):
    source = ASSET_ROOT / "environment"
    if not source.is_dir():
        raise RuntimeError("Pinned bun-sourcemap-leak environment is missing")
    for item in source.iterdir():
        target = workspace / item.name
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)
    (workspace / ".terminal-bench-source").write_text(
        f"repository={UPSTREAM_REPOSITORY}\n"
        f"commit={UPSTREAM_COMMIT}\n"
        f"task={UPSTREAM_TASK}\n"
    )
