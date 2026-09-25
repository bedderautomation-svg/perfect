"""Agent-visible shell for the synthetic document optimization experiment.

The case, evidence, prompts, answers, and grader are supplied by the host-only
controller. Keeping them out of this module prevents an implicit condition from
being disclosed by the agent-readable image.
"""

CONDITION = "synthetic-document-optimization"
TASK_PROMPT = (
    "This condition is initialized and driven by the host-side optimization "
    "controller. Use `python3 -m host_lab.anonymization_loop` on the host."
)
ARTIFACTS = {"summary.md"}


def initialize(workspace):
    (workspace / "README.md").write_text(
        "# Synthetic document exercise\n\n"
        "Follow the task instructions supplied by the experiment controller.\n"
    )
