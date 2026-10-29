# Validation record — Astra UI 1.1.0

What was actually checked before this source release, how to reproduce it,
and what was *not* checked. Claims here are deliberately scoped; see also the
proof-boundary section of the README.

## Checked

- **Full test suite.** 155 tests plus Qt view assertions, run via
  `--selftest`, pass from the source checkout and from an installed copy.
  The suite covers sampling, attribution, process roles, policy selection,
  GPU reading, runtime services, installer logic and the Qt views
  (offscreen).
- **Proof audit.** The Lean core builds with its six theorems
  (`red_on_time`, `red_on_pressure`, `green_means_room`, `level_monotone`,
  `redLine_le_amberLine`, `room_sound`); a build that passes is the proof
  check. An axiom audit (`#print axioms`) confirms each theorem depends only
  on standard axioms, with no `sorryAx` (no admitted gaps). The installer
  repeats this audit on every machine it installs to.
- **Hardware configuration matrix.** Five RAM sizes × six swap sizes through
  injected fixtures: 180 decisions answered by the actual compiled Lean core,
  all PROVED.
- **Portability fixtures.** Different user names and home paths, install and
  launcher paths containing spaces and special characters, RAM/swap changes
  while running, missing kernel pressure data, missing or failing GPU
  telemetry, and multiple-GPU enumeration — all via fixtures.
- **Fresh installation.** Installation into separate, isolated data
  directories (including paths with spaces), with a brand-new virtual
  environment and a fresh network download of the pinned Python
  dependencies, a local Lean core build, the axiom audit and the full test
  suite — ending in a working menu-launched application.
- **Performance, one machine.** On the development laptop (Ubuntu, X11,
  x86_64, default 2-second sampling): first frame 0.166 s, first RAM
  snapshot 0.411 s, steady-state ≈2.6 % of one logical CPU over a 120 s
  window, peak shell RSS ≈123 MB. Startup warmup is higher (≈8.8 % during
  the first ~10 s). These are measurements of one configuration, not
  guarantees for every machine.

## Not checked — treat as unverified

- No real memory-exhaustion crash was induced; RED-level behavior under true
  thrashing is reasoned and fixture-tested, not demonstrated destructively.
- Physical testing covers one Ubuntu X11 x86_64 machine with one NVIDIA GPU.
  Other distributions, Wayland sessions, ARM CPUs and real multi-GPU systems
  need their own runs (multi-GPU and varied RAM/swap were fixture-injected).
- AMD and Intel GPU memory telemetry is not implemented.
- Host measurements are not container/cgroup allocation ceilings, and job
  counts are identified local processes, not exact logical agents or cloud
  tasks.

## Reproduce it

```bash
# Read-only configuration report (works before Qt is installed):
python3 swarm_meter.py --diagnose

# Full suite from a prepared checkout (local .venv and built core):
./run.sh --selftest
```

`python3 install.py` runs the core build, axiom audit and full suite
automatically before changing any files, and records the logs next to its
backup manifest. The CI workflow in `.github/workflows/ci.yml` repeats the
lint, core build, axiom audit and full suite on a clean Ubuntu runner.
