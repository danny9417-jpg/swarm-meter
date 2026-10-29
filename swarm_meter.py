#!/usr/bin/env python3
"""Portable Linux entry point; no package installation occurs during startup."""

import os
from pathlib import Path
import sys

# Check the platform before importing Linux-only sampling/locking modules.
if sys.platform != "linux":
    raise SystemExit(
        "Swarm Meter currently supports Linux desktops with /proc; Windows/macOS are not supported."
    )

import config


def main():
    if sys.argv[1:] == ["--version"]:
        print("Swarm Meter — " + config.VERSION)
        return 0
    if sys.argv[1:] == ["--diagnose"]:
        import json

        print(json.dumps(config.diagnostics(), indent=2))
        return 0
    python = config.python_runtime()
    env = config.runtime_environment(python)
    # The dynamic loader reads LD_LIBRARY_PATH at interpreter startup. Re-exec
    # once when selecting a private venv or its optional native Qt dependencies.
    if os.environ.get("SWARM_METER_REEXEC") != "1" and (
        os.path.abspath(sys.executable) != os.path.abspath(python)
        or env.get("LD_LIBRARY_PATH") != os.environ.get("LD_LIBRARY_PATH")
    ):
        env["SWARM_METER_REEXEC"] = "1"
        os.execve(
            str(python),
            [str(python), str(Path(__file__).resolve()), *sys.argv[1:]],
            env,
        )
    try:
        import PySide6  # noqa: F401
    except ImportError:
        print(
            "Qt runtime unavailable. From the source folder run: python3 install.py --check\nThen: python3 install.py (creates a user venv; never uses sudo).",
            file=sys.stderr,
        )
        return 2
    # Desktop launches retain a local log; explicit CLI tools keep their output
    # in the terminal for scripts and diagnosis. Respect --data-dir for both.
    if not sys.argv[1:] or sys.argv[1:] == ["--view", "glance"]:
        directory = config.data_directory()
        directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            directory / "widget.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
        )
        os.dup2(fd, 1)
        os.dup2(fd, 2)
        os.close(fd)
    from main import main as run_app

    return run_app()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
