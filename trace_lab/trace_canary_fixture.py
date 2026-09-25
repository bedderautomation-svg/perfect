"""Agent-visible shell for the standalone trace-canary experiment.

The task prompt, scanner fixture, evaluation context, and grader are supplied by the host-only
controller.  This module only registers the fixture condition inside the
disposable agent image.
"""


CONDITION = "synthetic-trace-canary-optimization"
TASK_PROMPT = (
    "This condition is initialized and driven by the host-side trace-canary "
    "controller. Use `python3 -m host_lab.trace_canary_loop` on the host."
)
ARTIFACTS = {
    "scanner.py",
    "scanner-requirements.md",
    "public-cases.json",
    "evaluation-config.json",
    "scanner-report.md",
}


def initialize(workspace):
    (workspace / "README.md").write_text(
        "# Synthetic secret-scanner optimization exercise\n\n"
        "Follow the task instructions supplied by the experiment controller.\n"
    )
