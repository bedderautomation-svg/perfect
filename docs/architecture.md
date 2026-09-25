# Code architecture

The code has two main layers. **`host_lab/`** launches and controls an
experiment. **`trace_lab/`** provides native-client adapters, task fixtures,
observation, and reports. New run artifacts belong in the ignored `runs/`
directory; selected paper evidence and figures are managed separately.

```text
host_lab runner ──> disposable agent container ──> native session record
       │                    │
       │                    └── trace_lab fixture and client adapter
       ├── independent observer / verifier
       └── run report and raw artifacts in runs/
```

## Which component to edit

| Change | Location |
|---|---|
| A task prompt or synthetic data | The setting's `trace_lab/*fixture.py` or versioned `assets/` file |
| A native client's invocation, authentication, or session location | `trace_lab/cli.py`, `native.py`, `permissions.py`, or its client adapter |
| Time budget, feedback loop, retry rule, or external verifier | The corresponding `host_lab/*loop.py` runner |
| Independent filesystem/process evidence | `trace_lab/observer.py` and the setting's report code |

Use `python3 -m host_lab list` to find the runner for each of the paper's ten
settings. The common `trace_lab` CLI (`python3 -m trace_lab --help`) handles
image setup, calibration, and older single-run controls. Each setting runner
keeps its own `--help` so new options cannot silently change another protocol.

## Reproduction boundaries

- The agent sees only its container, staged task files, and native session
  records. The independent observer and verifier run outside that container.
- A task score, a trace-mutation score, a completed run, and a conclusive
  observation are separate fields. A verified edit can count even if a later
  native continuation fails. A valid refusal is a negative, not a retry target.
- Protocol defaults are not necessarily the settings used for a published
  batch. Preserve the exact runner arguments, selected trials, review decisions,
  and evidence hashes alongside any published dataset.

## Paths and privacy

Code should derive host paths from `Path(__file__)`, `Path.cwd()`, or a caller
supplied output directory, and shared metadata should store paths relative to
the repository or evidence package. Paths such as `/workspace` and
`/home/agent` denote locations **inside disposable containers**. They do not
identify a researcher. Historical native transcripts and host observer logs
are evidence, may contain collection-time paths, and should not be rewritten
silently: that would invalidate hashes and make it harder to audit the original
trial. Review and redact a publication copy, preserving an explicit source
hash and redaction record.

The baseline unit suite is `python3 -m unittest discover -s tests`. Some
tests bind a loopback server and therefore need a host environment that allows
local sockets. Use `python3 -m trace_lab doctor` and `calibrate` before live
experiments; neither makes a model request.
