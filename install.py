#!/usr/bin/env python3
"""Install the Linux desktop application for the current user, without sudo.

Installation is explicit, never part of normal startup. Source and the Lean core
are validated in scratch before launchers or working modules are replaced. The
installer neither starts nor stops applications and never removes user data.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
import datetime
import hashlib
import json
import os
import platform
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import venv

SOURCE = Path(__file__).resolve().parent
THEOREMS = (
    "red_on_time", "red_on_pressure", "green_means_room", "level_monotone",
    "redLine_le_amberLine", "room_sound",
)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def desktop_string(value):
    """Encode one Desktop Entry string value, never interpret it as shell code."""
    value = str(value)
    if any(ord(char) < 32 for char in value):
        raise ValueError("Launcher paths cannot contain control characters")
    return value.replace("\\", "\\\\")


def desktop_argument(value):
    """Apply Exec quoting, then the Desktop Entry string escape layer.

    See the freedesktop Exec specification: backslashes need both layers;
    percent signs must be doubled so a path never introduces a field code.
    """
    value = str(value)
    escaped = "".join("\\" + c if c in '\\"`$' else c for c in value)
    return '"' + desktop_string(escaped).replace("%", "%%") + '"'


def desktop_entry(destination):
    destination = Path(destination)
    # Keep the executable field on the detected shell. GLib rejects a literal
    # percent in executable paths during lookup; the script argument is expanded
    # correctly, including spaces, percent and equals signs in the install path.
    shell = shutil.which("bash")
    require(shell is not None, "Bash is required by run.sh")
    return (
        "[Desktop Entry]\nVersion=1.0\nType=Application\nName=Swarm Meter\n"
        "GenericName=Agent Swarm Monitor\n"
        "Comment=Future Gadget No.21 — local workload memory and headroom\n"
        f"Exec={desktop_argument(shell)} {desktop_argument(destination / 'run.sh')}\n"
        f"Icon={desktop_string(destination / 'icon.png')}\n"
        "Terminal=false\nCategories=Utility;System;Monitor;\n"
        "Keywords=agents;swarm;RAM;VRAM;monitor;\nStartupNotify=false\n"
    )


def find_lake():
    found = shutil.which("lake")
    if found:
        return Path(found)
    candidate = Path.home() / ".elan/bin/lake"
    return candidate if candidate.is_file() and os.access(candidate, os.X_OK) else None


def desktop_directory():
    """Respect localized/redirected desktop folders; never guess ~/Desktop."""
    command = shutil.which("xdg-user-dir")
    require(command is not None, "--desktop requires xdg-user-dir")
    result = subprocess.run([command, "DESKTOP"], capture_output=True, text=True,
                            check=True, timeout=5)
    path = Path(result.stdout.strip()).expanduser()
    require(path.is_absolute() and path.is_dir() and path != Path.home(),
            "No enabled Desktop folder was reported; omit --desktop")
    return path


def source_files(source):
    """Allowlist distributable code/assets; never copy logs or local settings."""
    names = {"README.md", "requirements.txt", "requirements-dev.txt", "run.sh",
             "icon.svg", "icon.png", "icon-1024.png"}
    files = [p for p in source.iterdir()
             if p.is_file() and (p.name in names or p.suffix == ".py")]
    files += [p for p in (source / "core").rglob("*")
              if p.is_file() and ".lake" not in p.parts
              and (p.suffix == ".lean" or p.name in
                   {"lean-toolchain", "lakefile.toml", "lake-manifest.json"})]
    return sorted(files)


def plan(args):
    require(sys.platform.startswith("linux"), "Swarm Meter currently supports Linux only")
    require(sys.version_info >= (3, 10), "Python 3.10 or newer is required")
    require(os.geteuid() != 0, "Run the installer as your normal user, without sudo")
    from config import data_directory, xdg_directory
    data_home = xdg_directory("XDG_DATA_HOME", ".local/share")
    destination = Path(args.install_dir).expanduser().resolve() if args.install_dir else data_home / "swarm-meter"
    desktop_entry(destination)  # Fail on unsupported path characters before writes.
    lake = find_lake()
    require(lake is not None,
            "Lean/Lake was not found. Install Lean's elan toolchain manager first; this installer never installs system tools.")
    for name in ("run.sh", "swarm_meter.py", "config.py", "requirements.txt", "core/lean-toolchain"):
        require((SOURCE / name).is_file(), "Source checkout is missing " + name)
    reuse = Path(args.reuse_venv).expanduser().resolve() if args.reuse_venv else None
    if reuse is not None:
        require((reuse / "bin/python").is_file() and (reuse / "pyvenv.cfg").is_file(),
                "--reuse-venv must name a Python virtual environment")
    desktop = desktop_directory() / "Swarm Meter.desktop" if args.desktop else None
    return {
        "source": str(SOURCE), "install_dir": str(destination),
        "runtime_data": str(data_directory(app_dir=destination)),
        "menu_entry": str(data_home / "applications/swarm-meter.desktop"),
        "desktop_entry": str(desktop) if desktop else None,
        "lake": str(lake), "venv": str(destination / ".venv"),
        "reuse_venv": str(reuse) if reuse else None,
        "toolchain": (SOURCE / "core/lean-toolchain").read_text().strip(),
        "network": "A new venv downloads pinned Python dependencies; Lake may fetch the pinned Lean toolchain through an existing elan installation.",
    }


@contextmanager
def held_lock(path):
    """Use the app's own advisory lock; never trust PID text or kill a process."""
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Swarm Meter is open. Close its window before installing.") from exc
        yield


def validate_python(python, requirements):
    """Check exact pinned packages without changing an existing environment."""
    pins = dict(line.strip().split("==", 1) for line in requirements.read_text().splitlines()
                if line.strip() and not line.lstrip().startswith("#"))
    code = (
        "import importlib.metadata,json,sys; from PySide6 import QtCore,QtWidgets; "
        "print(json.dumps({'python':list(sys.version_info[:2]),'packages':"
        "{n:importlib.metadata.version(n) for n in json.loads(sys.argv[1])}}))"
    )
    result = subprocess.run([str(python), "-c", code, json.dumps(list(pins))],
                            capture_output=True, text=True, check=True, timeout=30)
    observed = json.loads(result.stdout)
    require(observed["python"] >= [3, 10], "The selected runtime needs Python 3.10 or newer")
    require(observed["packages"] == pins,
            "The selected venv does not match requirements.txt; use a fresh install directory or a compatible --reuse-venv")


def prepare_venv(record):
    target = Path(record["venv"])
    reuse = Path(record["reuse_venv"]) if record["reuse_venv"] else None
    if target.exists() or target.is_symlink():
        require(reuse is None or target.resolve() == reuse,
                "Existing .venv points elsewhere; it will not be overwritten")
        validate_python(target / "bin/python", SOURCE / "requirements.txt")
        return target / "bin/python"
    if reuse:
        validate_python(reuse / "bin/python", SOURCE / "requirements.txt")
        target.symlink_to(reuse, target_is_directory=True)
    else:
        venv.EnvBuilder(with_pip=True).create(target)
        subprocess.run([str(target / "bin/python"), "-m", "pip", "install", "-r",
                        str(SOURCE / "requirements.txt")], check=True)
        validate_python(target / "bin/python", SOURCE / "requirements.txt")
    return target / "bin/python"


def validate_stage(stage, python, lake):
    """Build the local architecture's core, audit theorems, then run app tests."""
    core = stage / "core"
    subprocess.run([lake, "build"], cwd=core, check=True)
    audit = core / "InstallationAxioms.lean"
    audit.write_text("import Core\n" + "".join(
        "#print axioms AgentMeter." + name + "\n" for name in THEOREMS))
    result = subprocess.run([lake, "env", "lean", str(audit)], cwd=core,
                            capture_output=True, text=True, check=True)
    require("sorryAx" not in result.stdout + result.stderr
            and all(name in result.stdout for name in THEOREMS),
            "Lean theorem audit was incomplete or contains sorryAx")
    from config import runtime_environment
    environment = dict(os.environ, SWARM_METER_DATA_DIR=str(stage / "test-data"),
                       SWARM_METER_PYTHON=str(python),
                       SWARM_METER_CORE=str(core / ".lake/build/bin/core"),
                       SWARM_METER_CORE_DIR=str(core), QT_QPA_PLATFORM="offscreen",
                       PYTHONDONTWRITEBYTECODE="1",
                       DBUS_SESSION_BUS_ADDRESS="unix:path=/nonexistent/swarm-meter-install-validation")
    environment.pop("SWARM_METER_REEXEC", None)
    environment = runtime_environment(python, env=environment)
    tests = subprocess.run([str(python), str(stage / "swarm_meter.py"), "--selftest"],
                           env=environment, cwd=stage, capture_output=True, text=True)
    (stage / "selftest.log").write_text(tests.stdout + tests.stderr)
    if tests.returncode:
        print(tests.stdout + tests.stderr, file=sys.stderr)
        raise RuntimeError("Staged application self-tests failed; installation was not changed")
    return result.stdout


def atomic_copy(source, destination, mode=None):
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".swarm-install-", dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as target, Path(source).open("rb") as original:
            shutil.copyfileobj(original, target)
            target.flush()
            os.fsync(target.fileno())
        shutil.copystat(source, temporary)
        if mode is not None:
            temporary.chmod(mode)
        os.replace(temporary, destination)
    finally:
        # Only our incomplete staging file is removed, never an installed file.
        if temporary.exists():
            temporary.unlink()


def install(record):
    destination = Path(record["install_dir"])
    data = Path(record["runtime_data"])
    # Keep both locks: existing revisions stored mutable data beside code.
    with ExitStack() as stack:
        for path in sorted({destination / ".lock", data / ".lock"}):
            stack.enter_context(held_lock(path))
        python = prepare_venv(record)
        with tempfile.TemporaryDirectory(prefix=".swarm-meter-stage-", dir=destination.parent) as name:
            stage = Path(name)
            source_paths = source_files(SOURCE)
            for source in source_paths:
                target = stage / source.relative_to(SOURCE)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            proof_log = validate_stage(stage, python, record["lake"])
            files = [p.relative_to(SOURCE) for p in source_paths]
            files.append(Path("core/.lake/build/bin/core"))
            launcher = stage / "swarm-meter.desktop"
            launcher.write_text(desktop_entry(destination))
            mappings = [(stage / relative, destination / relative) for relative in files]
            mappings.append((launcher, Path(record["menu_entry"])))
            if record["desktop_entry"]:
                mappings.append((launcher, Path(record["desktop_entry"])))
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = destination / "install-backups" / stamp
            backup.mkdir(parents=True, exist_ok=False)
            from config import VERSION
            manifest = {"status": "prepared", "version": VERSION,
                        "machine": {"system": platform.system(), "architecture": platform.machine(),
                                    "setup_python": platform.python_version()},
                        "configuration": record, "files": []}
            (backup / "lean-axioms.log").write_text(proof_log)
            if (stage / "selftest.log").exists():
                shutil.copy2(stage / "selftest.log", backup / "selftest.log")
            for source, target in mappings:
                # Runtime data, icon files and modules that did not change are untouched.
                if target.is_file() and digest(source) == digest(target):
                    continue
                before = None
                if target.exists() or target.is_symlink():
                    require(target.is_file() and not target.is_symlink(),
                            "Refusing to replace a non-regular file: " + str(target))
                    saved = backup / "files" / str(target).lstrip("/")
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, saved)
                    before = {"path": str(saved), "sha256": digest(saved)}
                manifest["files"].append({"source": str(source), "target": str(target),
                                           "before": before, "sha256": digest(source)})
            manifest_path = backup / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
            changed = []
            try:
                for item in manifest["files"]:
                    target = Path(item["target"])
                    executable = target.name in ("run.sh", "core") or target.suffix == ".desktop"
                    atomic_copy(Path(item["source"]), target, 0o755 if executable else None)
                    changed.append(item)
                    require(digest(target) == item["sha256"], "Installed checksum differs: " + str(target))
            except Exception:
                # Reverse only this transaction, preserving newly created files in backup.
                for item in reversed(changed):
                    target = Path(item["target"])
                    if item["before"]:
                        atomic_copy(Path(item["before"]["path"]), target)
                    else:
                        retained = backup / "failed-new" / str(target).lstrip("/")
                        retained.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(target), retained)
                manifest["status"] = "rolled_back_after_error"
                manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
                raise
            manifest["status"] = "installed"
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    # A desktop may not provide GNOME's trust metadata; this is not an install failure.
    if record["desktop_entry"] and shutil.which("gio"):
        trust = subprocess.run(["gio", "set", record["desktop_entry"], "metadata::trusted", "true"],
                               capture_output=True, text=True)
        if trust.returncode:
            print("Desktop entry created. Your file manager may ask you to allow launching it.", file=sys.stderr)
    return {"installed": str(destination), "manifest": str(manifest_path),
            "launch": str(destination / "run.sh"), "automatic_start": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="print the setup plan without writes or downloads")
    parser.add_argument("--install-dir", help="override the current user's application directory")
    parser.add_argument("--reuse-venv", help="use an already compatible venv without modifying its packages")
    parser.add_argument("--desktop", action="store_true", help="also create an icon in the configured Desktop folder")
    args = parser.parse_args(argv)
    try:
        record = plan(args)
        if args.check:
            if record["reuse_venv"]:
                validate_python(Path(record["reuse_venv"]) / "bin/python", SOURCE / "requirements.txt")
            print(json.dumps(record, indent=2))
        else:
            print(json.dumps(install(record), indent=2))
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print("Installation stopped: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
