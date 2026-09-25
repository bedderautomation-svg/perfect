"""Discover and launch the host-side experiment runners.

Each runner retains its own argument parser and experiment protocol. This
module only provides a small, searchable index; it does not change defaults.
"""

from importlib import import_module
import sys


EXPERIMENTS = {
    "direct-user": ("direct_user_prompting", "Financial and personal privacy requests"),
    "tool-call": ("shell_tampering", "Persistent shell-result spoofing"),
    "privacy-skill": ("skill_injection", "Privacy-protection skill injection"),
    "compaction-skill": ("compaction_skill_injection", "Compaction-checkpoint skill injection"),
    "anonymization": ("anonymization_loop", "Document anonymization reward loop"),
    "terminal-bench": ("terminal_bench_loop", "Pinned Terminal-Bench reward loop"),
    "peer-influence": ("rollout_influence_loop", "Peer logs or peer workspaces"),
}


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"list", "-h", "--help"}:
        print("Usage: python3 -m host_lab EXPERIMENT [runner options]\n")
        print("Experiments (each retains its own --help and defaults):")
        for name, (_, description) in EXPERIMENTS.items():
            print(f"  {name:<18} {description}")
        print("\nUse 'python3 -m host_lab EXPERIMENT --help' for its options.")
        return 0
    name, *options = args
    if name not in EXPERIMENTS:
        print(f"Unknown experiment: {name}. Run 'python3 -m host_lab list'.", file=sys.stderr)
        return 2
    module_name = f"host_lab.{EXPERIMENTS[name][0]}"
    previous_argv = sys.argv
    try:
        sys.argv = [module_name, *options]
        return import_module(module_name).main()
    finally:
        sys.argv = previous_argv


if __name__ == "__main__":
    raise SystemExit(main())
