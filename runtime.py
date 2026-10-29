"""Runtime services for SWARM METER, separate from its views and sampler.

The UI can change freely while these small services preserve familiar desktop
behavior. Nothing here derives a lamp or threshold: notifications repeat the
core verdict, and termination requires two deliberate clicks for one identity.
The classes accept clocks and operating-system functions so tests can exercise
failure and timing cases without sending signals to working processes.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping


from config import data_directory


class Settings:
    """Tolerant reads and atomic durable replacement of the existing JSON state.

    Existing unknown keys survive ``update`` so a UI revision does not discard
    a setting it no longer displays. Save failures remain inspectable; a monitor
    should keep working while making a persistence failure visible to its UI.
    """

    def __init__(self, data_dir=None):
        self.directory = data_directory(data_dir)
        self.path = self.directory / "state.json"
        self.data: dict = {}
        self.last_error: str | None = None

    def load(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("Settings must be a JSON object")
            self.data = value
            self.last_error = None
        except FileNotFoundError:
            self.data = {}
            self.last_error = None
        except (OSError, ValueError) as exc:
            self.data = {}
            self.last_error = str(exc)
        return dict(self.data)

    def save(self, values: Mapping) -> bool:
        temporary: Path | None = None
        try:
            # Serialize before touching disk: bad data must not damage good state.
            payload = json.dumps(dict(values), ensure_ascii=False, indent=2) + "\n"
            self.directory.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(
                prefix=".state-", suffix=".tmp", dir=self.directory
            )
            temporary = Path(name)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
            # Persist the rename too; atomicity alone does not guarantee durability.
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            self.data = dict(values)
            self.last_error = None
            return True
        except (OSError, TypeError, ValueError) as exc:
            self.last_error = str(exc)
            return False
        finally:
            if temporary is not None:
                # This removes only our incomplete temporary file, never user data.
                try:
                    temporary.unlink()
                except OSError:
                    pass

    def update(self, **values) -> bool:
        return self.save({**self.data, **values})


def load_state(data_dir=None) -> dict:
    return Settings(data_dir).load()


def save_state(values: Mapping, data_dir=None) -> bool:
    return Settings(data_dir).save(values)


class InstanceLock:
    """A held descriptor is the lock; contenders never overwrite its PID text."""

    def __init__(self, data_dir=None):
        self.path = data_directory(data_dir) / ".lock"
        self._stream = None
        self.last_error: str | None = None
        self.already_running = False

    def acquire(self) -> bool:
        if self._stream is not None:
            return True
        self.already_running = False
        stream = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Append mode opens without truncation; seek/truncate only after flock.
            stream = self.path.open("a+", encoding="ascii")
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.already_running = True
                self.last_error = None
                stream.close()
                return False
            stream.seek(0)
            stream.truncate()
            stream.write(str(os.getpid()) + "\n")
            stream.flush()
            self._stream = stream
            self.last_error = None
            return True
        except OSError as exc:
            if stream is not None:
                stream.close()
            self.last_error = str(exc)
            return False

    def release(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None

    close = release

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError(self.last_error or "Swarm Meter is already running")
        return self

    def __exit__(self, *_):
        self.release()


_notification_children: list[subprocess.Popen] = []


def notify(title: str, body: str, urgency: str = "normal") -> bool:
    """Queue a desktop notification without blocking the render/sampling thread.

    True means the notification command started, not that a desktop displayed
    it: desktop notification settings can suppress delivery independently.
    Completed child processes are reaped on the next call.
    """
    _notification_children[:] = [p for p in _notification_children if p.poll() is None]
    try:
        child = subprocess.Popen(
            ["notify-send", "-u", urgency, "-a", "Swarm Meter", title, body],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _notification_children.append(child)
        return True
    except OSError:
        return False


def show_already_running_hint(notifier: Callable = notify) -> bool:
    """The second launcher stays a no-op and asks GNOME to show a visible hint.

    The caller may show a native message dialog when False is returned. We do
    not create another monitor window, mutate the existing lock, or signal an
    unverified PID taken from a lock-file string.
    """
    message = "Swarm Meter is already open. Use the existing window."
    print(message, file=sys.stderr)
    return bool(notifier("Swarm Meter is already running", message, "low"))


class Notifications:
    """Rate-limit routine changes while never suppressing a new RED warning."""

    def __init__(
        self,
        enabled=True,
        *,
        notifier: Callable = notify,
        clock: Callable[[], float] = time.monotonic,
        cooldown=20.0,
    ):
        self.enabled = bool(enabled)
        self._notify = notifier
        self._clock = clock
        self.cooldown = float(cooldown)
        self.last_level: str | None = None
        self.last_sent: float | None = None
        self.last_error: str | None = None
        self._pending: tuple[str, str, str] | None = None

    def on_level(self, snapshot: Mapping) -> bool:
        core = snapshot.get("core") or {}
        level = str(core.get("level", "UNKNOWN")).upper()
        previous = self.last_level
        self.last_level = level
        if snapshot.get("demo") or not self.enabled:
            self._pending = None
            return False
        if level not in {"GREEN", "AMBER", "RED"} or core.get("valid") is False:
            # Missing readings cannot generate a reassuring recovery message.
            self._pending = None
            return False
        now = self._clock()
        changed = level != previous
        if changed and (previous is not None or level == "RED"):
            available = (snapshot.get("mem") or {}).get("available")
            reading = (
                f"{available / 1024:.1f} GiB RAM available. "
                if isinstance(available, (int, float))
                else ""
            )
            if level == "RED":
                self._pending = (
                    "Swarm Meter: RED",
                    reading + "Stop starting new jobs. Review the largest jobs.",
                    "critical",
                )
            elif level == "AMBER":
                self._pending = (
                    "Swarm Meter: AMBER",
                    reading + "Hold new jobs while memory recovers.",
                    "normal",
                )
            elif previous in {"RED", "AMBER"}:
                self._pending = (
                    "Swarm Meter: back to GREEN",
                    reading + "Memory pressure has eased.",
                    "low",
                )
            else:
                self._pending = None
        if self._pending is None:
            return False
        # A new RED escalation bypasses cooldown, including RED on first sample.
        urgent = level == "RED" and changed
        if (
            not urgent
            and self.last_sent is not None
            and now - self.last_sent < self.cooldown
        ):
            return False
        try:
            sent = bool(self._notify(*self._pending))
        except Exception as exc:
            self.last_error = str(exc)
            return False
        if sent:
            self.last_sent = now
            self.last_error = None
            self._pending = None
        else:
            self.last_error = "Desktop notification command is unavailable"
        return sent


@dataclass(frozen=True)
class ProcessIdentity:
    """A PID alone can be reused; kernel start ticks distinguish its lifetime."""

    pid: int
    starttime: int


@dataclass(frozen=True)
class ActionResult:
    status: str
    message: str
    sent: bool = False


def read_process_starttime(pid: int) -> int | None:
    """Read stat field 22, allowing spaces and parentheses in the process name."""
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8")
        # Fields after the final ')' start at field 3; starttime is index 19.
        return int(raw[raw.rfind(")") + 2 :].split()[19])
    except (OSError, ValueError, IndexError):
        return None


class TerminationController:
    """Two clicks, one kernel-bound process, four monotonic seconds.

    Linux pidfds prevent a reused PID from redirecting the signal. If pidfds
    are unavailable, termination is disabled rather than falling back to the
    weaker PID-only operation. The view calls ``reconcile`` on each sample and
    ``select`` on a selection change; both cancel unsafe pending confirmation.
    """

    def __init__(
        self,
        mode="live",
        *,
        clock: Callable = time.monotonic,
        identity_reader: Callable = read_process_starttime,
        pidfd_open: Callable | None = getattr(os, "pidfd_open", None),
        sender: Callable | None = getattr(signal, "pidfd_send_signal", None),
        fd_close: Callable = os.close,
    ):
        self.mode = mode
        self._clock = clock
        self._identity_reader = identity_reader
        self._pidfd_open = pidfd_open
        self._sender = sender
        self._fd_close = fd_close
        self.selected: ProcessIdentity | None = None
        self._demo = False
        self._fd: int | None = None
        self._deadline: float | None = None
        self.last_result = ActionResult("idle", "Select a job to inspect it.")

    @staticmethod
    def identity(job: Mapping | None) -> ProcessIdentity | None:
        if job is None:
            return None
        try:
            pid, starttime = int(job["pid"]), int(job["starttime"])
            if pid <= 1 or starttime <= 0:
                return None
            return ProcessIdentity(pid, starttime)
        except (KeyError, TypeError, ValueError):
            return None

    def _result(self, status: str, message: str, sent=False) -> ActionResult:
        self.last_result = ActionResult(status, message, sent)
        return self.last_result

    def _release(self) -> None:
        fd, self._fd = self._fd, None
        self._deadline = None
        if fd is not None:
            try:
                self._fd_close(fd)
            except OSError:
                pass

    def cancel(self, reason="Confirmation cancelled.") -> ActionResult:
        self._release()
        return self._result("cancelled", reason)

    def select(self, job: Mapping | None, *, demo=False) -> ActionResult:
        identity = self.identity(job)
        demo = bool(demo or (job or {}).get("is_demo"))
        if identity != self.selected or demo != self._demo:
            self.cancel("Selection changed. No signal was sent.")
            self.selected = identity
            self._demo = demo
        return self.last_result

    def reconcile(self, jobs: Iterable[Mapping]) -> ActionResult:
        if self.selected is not None and not any(
            self.identity(job) == self.selected for job in jobs
        ):
            self.cancel("This job has exited or its identity is no longer available.")
            self.selected = None
        # Expiry is evaluated even when the selected job still exists.
        self._expire()
        return self.last_result

    def _expire(self) -> bool:
        if self._deadline is not None and self._clock() >= self._deadline:
            self._release()
            self._result("expired", "Confirmation expired. No signal was sent.")
            return True
        return False

    @property
    def armed(self) -> bool:
        self._expire()
        return self._fd is not None and self._deadline is not None

    @property
    def remaining(self) -> float:
        self._expire()
        return (
            max(0.0, self._deadline - self._clock())
            if self._deadline is not None
            else 0.0
        )

    def _allowed(self) -> ActionResult | None:
        if self.mode != "live" or self._demo:
            self._release()
            return self._result(
                "disabled", "Termination is disabled for demo and test data."
            )
        if self.selected is None:
            self._release()
            return self._result(
                "unavailable", "Select a live job with a verified process identity."
            )
        if self._pidfd_open is None or self._sender is None:
            self._release()
            return self._result(
                "disabled", "Safe process termination is unavailable on this system."
            )
        try:
            actual = self._identity_reader(self.selected.pid)
        except (OSError, ValueError):
            actual = None
        if actual != self.selected.starttime:
            self._release()
            return self._result(
                "unavailable",
                "The job exited or its process identity changed. No signal was sent.",
            )
        return None

    def arm(self) -> ActionResult:
        self._release()
        if (blocked := self._allowed()) is not None:
            return blocked
        try:
            self._fd = self._pidfd_open(self.selected.pid, 0)
            # A replacement between identity check and fd open must not be armed.
            if (blocked := self._allowed()) is not None:
                return blocked
            self._deadline = self._clock() + 4.0
            return self._result(
                "armed", f"Confirm TERMINATE for PID {self.selected.pid} within 4 s."
            )
        except (OSError, ValueError) as exc:
            self._release()
            return self._result(
                "unavailable", f"Could not prepare safe termination: {exc}"
            )

    def confirm(self) -> ActionResult:
        if self._expire():
            return self.last_result
        if self._fd is None or self._deadline is None:
            return self._result("idle", "Press TERMINATE once to arm confirmation.")
        if (blocked := self._allowed()) is not None:
            return blocked
        # An identity read can be delayed by the OS; the deadline still applies
        # immediately before delivery, not just when the user clicked.
        if self._expire():
            return self.last_result
        pid, fd = self.selected.pid, self._fd
        try:
            # This is the only delivery site. A pidfd names a lifetime, not a PID.
            self._sender(fd, signal.SIGTERM, None, 0)
            return self._result(
                "sent",
                f"SIGTERM sent to PID {pid}. The job may take time to stop.",
                True,
            )
        except ProcessLookupError:
            return self._result(
                "unavailable", "The job already exited. No signal was sent."
            )
        except PermissionError:
            return self._result("denied", "Permission denied. No signal was sent.")
        except (OSError, ValueError) as exc:
            return self._result("failed", f"Could not send SIGTERM: {exc}")
        finally:
            self._release()

    def click(self) -> ActionResult:
        # Expired confirmation requires a fresh two-click sequence.
        return self.confirm() if self.armed else self.arm()

    def close(self) -> None:
        self._release()
        self.selected = None
