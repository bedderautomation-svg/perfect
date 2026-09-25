"""Agent-visible shell for the synthetic completed-peer influence setting.

The reference environments, prompts, ground truth, and grader are supplied by the
host-only controller.  This module only registers the fixture condition inside
the disposable agent image.
"""


CONDITION = "synthetic-rollout-influence-optimization"
TASK_PROMPT = (
    "This condition is initialized and driven by the host-side rollout influence "
    "controller. Use `python3 -m host_lab.rollout_influence_loop` on the host."
)
REFERENCE_ROOT = "completed-agents"
ARTIFACTS = {"summary.md", REFERENCE_ROOT + "/scores.md", "prior-agent-rollouts.jsonl"}


def initialize(workspace):
    (workspace / "README.md").write_text(
        "# Completed-run reference exercise\n\n"
        "Follow the task instructions supplied by the experiment controller.\n"
    )
