"""Machine-independent paths and startup discovery (stdlib only).

A checkout and a per-user installation use the same relative layout. Mutable
history/settings live outside a fresh checkout; existing installations retain
those files in place. Nothing here installs packages or changes the desktop.
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import platform
import shutil
import sys
import sysconfig

VERSION = "Astra UI 1.1.0"
APP_DIR = Path(__file__).resolve().parent


def xdg_directory(key, default, *, env=None, home=None):
    """Ignore relative XDG settings as required by the freedesktop specification."""
    env = os.environ if env is None else env
    home = Path.home() if home is None else Path(home)
    value = env.get(key, "")
    return Path(value) if value and Path(value).is_absolute() else home / default


def data_directory(directory=None, *, app_dir=None, env=None, home=None):
    env = os.environ if env is None else env
    app_dir = APP_DIR if app_dir is None else Path(app_dir)
    if directory is not None or env.get("SWARM_METER_DATA_DIR"):
        return Path(
            directory if directory is not None else env["SWARM_METER_DATA_DIR"]
        ).expanduser()
    legacy = (
        xdg_directory("XDG_DATA_HOME", ".local/share", env=env, home=home)
        / "swarm-meter"
    )
    for location in (app_dir, legacy):
        # Do not migrate a live database or create a second singleton during an
        # upgrade. New users have neither marker and get the standard state path.
        if any((location / name).is_file() for name in ("state.json", "history.db")):
            return location
    return (
        xdg_directory("XDG_STATE_HOME", ".local/state", env=env, home=home)
        / "swarm-meter"
    )


def core_paths(core_bin=None, core_dir=None, *, app_dir=None, env=None):
    """Explicit overrides win; normal binaries/toolchains stay beside the app."""
    env = os.environ if env is None else env
    app_dir = APP_DIR if app_dir is None else Path(app_dir)
    root = Path(
        core_dir or env.get("SWARM_METER_CORE_DIR") or app_dir / "core"
    ).expanduser()
    binary = core_bin or env.get("SWARM_METER_CORE")
    return root, Path(binary).expanduser() if binary else root / ".lake/build/bin/core"


def python_runtime(*, app_dir=None, env=None):
    env = os.environ if env is None else env
    app_dir = APP_DIR if app_dir is None else Path(app_dir)
    override = env.get("SWARM_METER_PYTHON")
    if override:
        candidate = Path(override).expanduser()
        selected = (
            str(candidate)
            if candidate.is_file()
            else shutil.which(override, path=env.get("PATH"))
        )
        if not selected or not os.access(selected, os.X_OK):
            raise RuntimeError(
                "SWARM_METER_PYTHON does not name an executable Python interpreter"
            )
        return Path(selected).absolute()
    local = app_dir / ".venv/bin/python"
    if local.exists() and not os.access(local, os.X_OK):
        raise RuntimeError(
            "The sibling .venv/bin/python is not executable; repair the runtime or set SWARM_METER_PYTHON"
        )
    return local if local.is_file() else Path(sys.executable)


def runtime_environment(python, *, env=None):
    """Support optional private Qt native libraries without pinning CPU ABI/path.

    Follow the venv directory, not its python symlink (which often targets the
    system executable). Distribution-installed Qt dependencies need no override.
    """
    result = dict(os.environ if env is None else env)
    prefix = Path(python).absolute().parent.parent
    abi = sysconfig.get_config_var("MULTIARCH")
    locations = [prefix / "native/usr/lib"]
    if abi:
        locations.insert(0, prefix / "native/usr/lib" / abi)
    private = [str(p) for p in locations if p.is_dir()]
    if private:
        previous = result.get("LD_LIBRARY_PATH", "").split(os.pathsep)
        result["LD_LIBRARY_PATH"] = os.pathsep.join(
            dict.fromkeys(private + [p for p in previous if p])
        )
    return result


def diagnostics():
    """Read-only support report: works without Qt or a graphical session.

    Driver-library availability is only a capability hint; the live sampler
    determines actual GPU telemetry and never turns missing readings into zero.
    """
    from backend import (
        CoreClient,
        cpu_threads,
        derive_policy,
        read_meminfo,
        read_pressure,
    )

    mem = read_meminfo()
    pressure = read_pressure()
    core = CoreClient()
    try:
        verdict = core.ask(
            mem["total"],
            mem["available"],
            pressure,
            1024,
            derive_policy(mem["total"], mem["swap_total"]),
        )
    finally:
        core.close()
    try:
        ctypes.CDLL("libnvidia-ml.so.1")
        nvml = True
    except OSError:
        nvml = False
    return {
        "version": VERSION,
        "platform": platform.system(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "cpu_threads": cpu_threads(),
        "ram_mib": mem["total"],
        "available_ram_mib": mem["available"],
        "swap_mib": mem["swap_total"],
        "memory_pressure_available": pressure is not None,
        "data_directory": str(data_directory()),
        "core_binary": str(core.core_bin),
        "core_proved": verdict["proved"],
        "core_status": verdict.get("reason"),
        "nvidia_driver_library_available": nvml,
        "gpu_scope": "All physical NVIDIA devices visible to NVML; AMD/Intel telemetry unavailable. GPU memory is not a pooled allocation limit.",
        "notify_send_available": shutil.which("notify-send") is not None,
        "docker_cli_available": shutil.which("docker") is not None,
        "display_session": os.environ.get("XDG_SESSION_TYPE", "unknown"),
        "limits": "Host Linux /proc resources; not cgroup/container allocation limits or exact cloud-agent counts.",
    }
