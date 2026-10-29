"""Measured runtime and layout verification for the candidate Qt window.

This module is opt-in: normal launches do not import or run a verifier. The CLI
owns startup timing and can supply its monotonic launch timestamp. CPU is read
from /proc, where 100% means one fully occupied logical CPU. RSS is read from
/proc too; inherited ru_maxrss values are misleading in the execution harness.

Usage after constructing/showing a ui.SwarmWindow:
    verifier = Verifier(window, 60, mode='benchmark', output='benchmark.json')
    verifier.start()

For layout stress, construct a separate preview=True, start_worker=False window
and pass stress=True. It never replaces a live window's readings, writes live
history, enables notifications, or permits real process actions. A 600-second
live flicker run and a shorter synthetic stress run are complementary evidence.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import time

from PySide6.QtCore import QObject, QEvent, QTimer, Signal, Qt
from PySide6.QtWidgets import QApplication


CLOCK_TICKS = os.sysconf("SC_CLK_TCK")


def process_stat(pid: int) -> dict | None:
    """A process lifetime and CPU counters; names may contain spaces or ')'."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        fields = raw[raw.rfind(")") + 2 :].split()
        return {
            "pid": pid,
            "name": raw[raw.find("(") + 1 : raw.rfind(")")],
            "starttime": int(fields[19]),
            "cpu_ticks": int(fields[11]) + int(fields[12]),
            "waited_child_ticks": int(fields[13]) + int(fields[14]),
        }
    except (OSError, ValueError, IndexError):
        return None


def resident_bytes(pid: int) -> int | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def descendant_stats(pid: int) -> dict[tuple[int, int], dict]:
    """Read only this application's descendants, including worker-thread children.

    Scanning /proc/<pid>/task/*/children avoids re-reading every process on the
    machine. A very short-lived child can escape a snapshot; reaped-child CPU
    accounting in the parent's stat still captures its consumed CPU.
    """
    pending, seen, result = [pid], {pid}, {}
    while pending:
        parent = pending.pop()
        try:
            tasks = list(Path(f"/proc/{parent}/task").iterdir())
        except OSError:
            continue
        for task in tasks:
            try:
                children = [
                    int(item) for item in (task / "children").read_text().split()
                ]
            except (OSError, ValueError):
                continue
            for child in children:
                if child in seen:
                    continue
                seen.add(child)
                if (stats := process_stat(child)) is not None:
                    stats["rss_bytes"] = resident_bytes(child)
                    result[(child, stats["starttime"])] = stats
                    pending.append(child)
    return result


def cpu_interval(start_stat, start_children, final_stat, final_children, duration):
    """Account once for each process lifetime, including reaped descendants."""
    shell = (
        final_stat["cpu_ticks"] - start_stat["cpu_ticks"]
        if final_stat and start_stat
        else None
    )
    child_total = lambda data: data.get("cpu_ticks", 0) + data.get(
        "waited_child_ticks", 0
    )
    alive = sum(
        max(0, child_total(data) - child_total(start_children.get(key, {})))
        for key, data in final_children.items()
    )
    waited = None
    if final_stat and start_stat:
        # A helper reaped during this interval transfers its lifetime CPU into
        # the parent's waited counter; remove its pre-observation contribution.
        preexisting_gone = sum(
            child_total(data)
            for key, data in start_children.items()
            if key not in final_children
        )
        waited = max(
            0,
            final_stat["waited_child_ticks"]
            - start_stat["waited_child_ticks"]
            - preexisting_gone,
        )
    family = (
        shell + waited + alive if shell is not None and waited is not None else None
    )
    seconds = lambda ticks: round(ticks / CLOCK_TICKS, 6) if ticks is not None else None
    percent = (
        lambda ticks: round(ticks / CLOCK_TICKS / duration * 100, 4)
        if ticks is not None and duration > 0
        else None
    )
    return {
        "shell_cpu_s": seconds(shell),
        "waited_children_cpu_s": seconds(waited),
        "currently_alive_children_cpu_s": seconds(alive),
        "family_cpu_s": seconds(family),
        "shell_cpu_percent": percent(shell),
        "waited_children_cpu_percent": percent(waited),
        "currently_alive_children_cpu_percent": percent(alive),
        "family_cpu_percent": percent(family),
    }


class StressSource:
    """Synthetic process trees use the real aggregation seam and proved binary."""

    def __init__(self):
        import backend

        self.motor = backend
        self.sampler = backend.Sampler()
        self.index = 0

    def snapshot(self) -> dict:
        phase = self.index % 8
        counts = (0, 1, 9, 15, 64, 130, 4, 25)
        available = (48000, 40000, 22000, 10000, 2200, 500, 25000, 46000)[phase]
        pressure = (0, 0, 2, 10, 240, 300, 0, 0)[phase]
        self.index += 1
        processes = {}
        uid = os.getuid()
        kinds = [
            ("claude", "claude", ""),
            ("codex", "codex", ""),
            ("gemini", "gemini", ""),
            ("node", "node node_modules/openclaw", ""),
            ("ollama", "ollama serve", ""),
            ("python3", "uvicorn fixture", "0::/docker-012345abcdef.scope"),
        ]

        def process(
            pid,
            name,
            parent,
            memory,
            command,
            group="",
            folder="/example/swarm-fixture",
        ):
            processes[pid] = {
                "pid": pid,
                "name": name,
                "ppid": parent,
                "uid": uid,
                "mem": memory,
                "cmd": command,
                "cg": group,
                "cwd": folder,
                "starttime": 100000 + pid,
                "identity": (pid, 100000 + pid),
                "age_s": float(self.index),
                "memory_kind": "synthetic fixture",
            }

        # Empty state is genuinely empty. Other phases vary count, identity labels,
        # and number width while stable jobs keep the same process identity.
        if counts[phase]:
            for number, (name, command, group) in enumerate(kinds):
                folder = "/example/" + (
                    "long-project-label-" * 9 if phase % 2 else "swarm-fixture"
                )
                process(2000000 + number, name, 1, 128, command, group, folder)
            # Support processes intentionally outnumber work in the quiet
            # phase. They stay visible without inflating workload count/mean.
            for number in range(15):
                process(
                    2050000 + number,
                    "decomp",
                    2000001,
                    192,
                    "python /opt/tools/decomp mcp",
                )
            process(2060000, "node", 2000001, 96, "node")
            for number in range(counts[phase]):
                parent = 2000000 + number % len(kinds)
                mem = (16, 999, 1024, 10239, 10240, 24576)[(number + phase) % 6]
                # Large illustrative numbers deliberately stress widths; fixture
                # totals are not claimed to be a physically possible snapshot.
                name = (
                    "python-validation-worker-with-an-intentionally-long-name"
                    if number % 7 == 0
                    else "python"
                )
                process(
                    2100000 + number,
                    name,
                    parent,
                    mem,
                    "python fixture.py --display-only --label " + "long-argument-" * 20,
                    "0::/sg0-decode.slice/fixture.scope" if number % 4 == 0 else "",
                )
        mem = {
            "total": 63727,
            "available": available,
            "swap_total": 2047,
            "swap_free": 2047 if phase < 4 else 0,
        }
        gpu = {
            "name": "Synthetic GPU — layout fixture",
            "total": 24463,
            "used": (0, 999, 10240, 24462)[phase % 4],
            "util": 0,
            "per_pid": {},
            "process_data_valid": True,
        }
        snap = self.sampler.aggregate(processes, mem, pressure, gpu)
        snap.update(
            demo=True,
            demo_label="Synthetic layout stress — not live measurements",
            history_status="Synthetic preview — not recorded",
        )
        for owner in snap["owners"]:
            for process in owner["processes"]:
                process["is_demo"] = True
        return snap

    def close(self):
        self.sampler.close()


class Verifier(QObject):
    """Observe runtime without changing the live sampler or its displayed data.

    mode='benchmark' keeps the selected sample rate (normally 2 s).
    mode='flicker' temporarily requests 1 s, restoring it before window close.
    Set stress=True only for an isolated preview; it paints fixture samples at
    1 s and uses the same backend aggregation/core path as the live window.
    ``warmup_s`` delays the measurement baseline; startup CPU during that warmup
    is reported separately and first-frame/first-sample timings stay unchanged.
    ``finished`` emits the complete JSON-compatible report, also saved to output.
    """

    finished = Signal(object)

    def __init__(
        self,
        window,
        seconds,
        *,
        mode="benchmark",
        output=None,
        stress=False,
        launch_started=None,
        quit_on_finish=True,
        warmup_s=0,
    ):
        super().__init__(window)
        if mode not in {"benchmark", "flicker"}:
            raise ValueError("mode must be benchmark or flicker")
        if not math.isfinite(float(seconds)) or float(seconds) <= 0:
            raise ValueError("verification duration must be finite and positive")
        if not math.isfinite(float(warmup_s)) or float(warmup_s) < 0:
            raise ValueError("warmup duration must be finite and nonnegative")
        if stress and (
            not window.preview or (window.worker and window.worker.is_alive())
        ):
            raise ValueError(
                "Synthetic stress requires an isolated preview without a live sampler"
            )
        self.window = window
        self.seconds = float(seconds)
        self.mode = mode
        self.output = Path(output) if output else None
        self.stress = bool(stress)
        self.launch_started = launch_started
        self.quit_on_finish = quit_on_finish
        self.warmup_s = float(warmup_s)
        self._warmup_start = None
        self._warmup_report = None
        self.report = None
        self.done = False
        self._begun = False
        self._original_apply = window.apply_snapshot
        self._original_interval = window.interval
        self._source = StressSource() if stress else None
        self._sampler_timer = QTimer(self)
        self._sampler_timer.setInterval(1000)
        self._sampler_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._sampler_timer.timeout.connect(self._stress_tick)
        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._observe)
        self._finish_timer = QTimer(self)
        self._finish_timer.setSingleShot(True)
        self._finish_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._finish_timer.timeout.connect(self._finish_when_due)
        self._snapshots = 0
        self._layouts = {}
        self._violations = []
        self._violation_keys = {}
        self._paint_count = 0
        self._resize_events = 0
        self._rss = []
        self._child_peaks = {}
        self._max_child_rss = 0
        self._max_family_rss = 0
        self._seen_levels = set()
        self._min_jobs = None
        self._max_jobs = 0
        self._job_counts = set()
        self._min_processes = None
        self._max_processes = 0
        self._process_roles = set()
        self._max_tick_gap = 0.0
        self._first_frame_observed = None
        self._all_samples_proved = True
        self._last_process_observation = 0.0

    def start(self):
        """Defer baseline until Qt has performed the initial layout/first paint."""
        if self._begun or self.done or self._warmup_start is not None:
            return self
        self._warmup_start = time.monotonic()
        self._warmup_stat = process_stat(os.getpid())
        self._warmup_children = descendant_stats(os.getpid())
        self._baseline_widget_ids = self._widget_ids()
        self._snapshot_count_at_start = self._snapshots
        self.window.installEventFilter(self)
        QApplication.instance().aboutToQuit.connect(self._application_quitting)
        self.window.apply_snapshot = self._counted_apply
        if self.mode == "flicker":
            self.window.interval = 1
        # Warmup and the 250 ms initial layout settling interval are reported
        # separately. Neither shortens the requested steady measurement duration.
        QTimer.singleShot(
            math.ceil((self.warmup_s + 0.25) * 1000), self._begin_when_due
        )
        return self

    def _begin_when_due(self):
        # Enforce the requested warmup even if a default coarse timer fires early.
        if self.done or self._begun:
            return
        remaining = self.warmup_s + 0.25 - (time.monotonic() - self._warmup_start)
        if remaining > 0:
            QTimer.singleShot(max(1, math.ceil(remaining * 1000)), self._begin_when_due)
        else:
            self._begin()

    def eventFilter(self, watched, event):
        if watched is self.window:
            if event.type() == QEvent.Type.Paint:
                self._paint_count += 1
                if self._first_frame_observed is None:
                    self._first_frame_observed = time.monotonic()
            elif event.type() == QEvent.Type.Resize and self._begun:
                self._resize_events += 1
            elif event.type() == QEvent.Type.Close and not self.done:
                # Preserve the observed portion before the window and its child
                # widgets disappear. This does not intercept or prevent closing.
                self.finish(stop_reason="window_closed", close_window=False)
        return False

    def _application_quitting(self):
        # Application.quit() does not necessarily issue a Close event first.
        self.finish(stop_reason="application_quit", close_window=False)

    def _counted_apply(self, snap):
        self._original_apply(snap)
        self._snapshots += 1
        count = len(snap.get("agent_jobs", []))
        self._min_jobs = count if self._min_jobs is None else min(self._min_jobs, count)
        self._max_jobs = max(self._max_jobs, count)
        self._job_counts.add(count)
        process_count = int(snap.get("nproc", 0))
        self._min_processes = (
            process_count
            if self._min_processes is None
            else min(self._min_processes, process_count)
        )
        self._max_processes = max(self._max_processes, process_count)
        self._process_roles.update(
            process["role"]
            for owner in snap.get("owners", [])
            for process in owner.get("processes", [])
        )
        self._seen_levels.add(snap.get("core", {}).get("level", "UNKNOWN"))
        self._all_samples_proved &= bool(snap.get("proved"))

    def _begin(self):
        if self.done or self._begun:
            return
        self._begun = True
        self._start = time.monotonic()
        self._last_tick = self._start
        self._start_stat = process_stat(os.getpid())
        self._start_children = descendant_stats(os.getpid())
        self._warmup_report = self._warmup_metrics(
            self._start, self._start_stat, self._start_children
        )
        self._baseline_widget_ids = self._widget_ids()
        self._snapshot_count_at_start = self._snapshots
        self._observe()
        self._timer.start()
        if self._source:
            self._stress_tick()
            self._sampler_timer.start()
        self._finish_timer.start(math.ceil(self.seconds * 1000))

    def _warmup_metrics(self, end, final, children):
        warmup_duration = end - self._warmup_start
        return {
            "completed": warmup_duration >= self.warmup_s + 0.25,
            "requested_warmup_s": self.warmup_s,
            "additional_layout_settle_s": 0.25,
            "observed_duration_s": round(warmup_duration, 4),
            "began_s_after_launch": (
                round(self._warmup_start - self.launch_started, 4)
                if self.launch_started is not None
                else None
            ),
            "scope": "From verifier start through steady baseline; CPU before verifier start is outside this interval.",
            **cpu_interval(
                self._warmup_stat,
                self._warmup_children,
                final,
                children,
                warmup_duration,
            ),
        }

    def _finish_when_due(self):
        # Qt's default coarse timers may fire early. Acceptance is ten actual
        # minutes, so verify the monotonic deadline even with a precise timer.
        remaining = self.seconds - (time.monotonic() - self._start)
        if remaining > 0:
            self._finish_timer.start(max(1, math.ceil(remaining * 1000)))
        else:
            self.finish(stop_reason="duration_elapsed")

    def _widget_ids(self):
        # These are persistent Python references owned by SwarmWindow. Dynamic
        # row data need not create a different table/model/widget object.
        return {
            name: id(getattr(self.window, name))
            for name in (
                "glance",
                "overview",
                "resources",
                "hive",
                "scope",
                "table",
                "model",
                "footer",
            )
            if hasattr(self.window, name)
        }

    def _layout(self):
        w = self.window
        return {
            "window": [w.width(), w.height()],
            "table": [w.table.width(), w.table.height()],
            "columns": [w.table.columnWidth(i) for i in range(w.model.columnCount())],
            "header_height": w.table.horizontalHeader().height(),
            "overview_height": w.overview.height(),
            "hive_height": w.hive.height(),
        }

    def _record_violation(self, kind, expected, observed):
        # Bound diagnostics even if a layout oscillates for the entire ten minutes.
        key = json.dumps([kind, expected, observed], sort_keys=True)
        if key in self._violation_keys:
            item = self._violation_keys[key]
            item["observations"] += 1
            item["last_at_s"] = round(time.monotonic() - self._start, 3)
            return
        if len(self._violations) < 50:
            item = {
                "at_s": round(time.monotonic() - self._start, 3),
                "observations": 1,
                "kind": kind,
                "expected": expected,
                "observed": observed,
            }
            self._violations.append(item)
            self._violation_keys[key] = item

    def _observe(self, force_process=False):
        if self.done or not self._begun:
            return
        now = time.monotonic()
        self._max_tick_gap = max(self._max_tick_gap, now - self._last_tick)
        self._last_tick = now
        # Process-tree accounting is heavier than checking widget coordinates.
        # Sample RSS/children once per second; keep layout observations at 4 Hz.
        if force_process or now - self._last_process_observation >= 1.0:
            self._last_process_observation = now
            rss = resident_bytes(os.getpid())
            if rss is not None:
                self._rss.append(rss)
            children = descendant_stats(os.getpid())
            child_sum = sum(v["rss_bytes"] or 0 for v in children.values())
            self._max_child_rss = max(self._max_child_rss, child_sum)
            # Use the same observation for app + helpers; summing unrelated
            # individual peaks would exaggerate simultaneous resident memory.
            self._max_family_rss = max(self._max_family_rss, (rss or 0) + child_sum)
            for identity, data in children.items():
                old = self._child_peaks.get(
                    identity,
                    {
                        "pid": identity[0],
                        "name": data["name"],
                        "starttime": identity[1],
                        "max_rss_bytes": 0,
                        "last_cpu_ticks": 0,
                    },
                )
                old["max_rss_bytes"] = max(old["max_rss_bytes"], data["rss_bytes"] or 0)
                old["last_cpu_ticks"] = max(old["last_cpu_ticks"], data["cpu_ticks"])
                self._child_peaks[identity] = old
        ids = self._widget_ids()
        if ids != self._baseline_widget_ids:
            self._record_violation("widget identities", self._baseline_widget_ids, ids)
        layout = self._layout()
        # View changes are deliberate. Compare updates within each view separately.
        view = self.window.view
        if view not in self._layouts:
            self._layouts[view] = layout
        elif layout != self._layouts[view]:
            self._record_violation(
                "layout changed within " + view, self._layouts[view], layout
            )

    def _stress_tick(self):
        if self._source and not self.done:
            self.window.apply_snapshot(self._source.snapshot())

    def finish(self, *, stop_reason="explicit_finish", close_window=True):
        """Save complete or partial evidence; never imply an early run passed."""
        if self.done or self._warmup_start is None:
            return self.report
        if self._begun:
            self._observe(force_process=True)
        end = time.monotonic()
        final = process_stat(os.getpid())
        children = descendant_stats(os.getpid())
        duration = end - self._start if self._begun else 0.0
        if self._begun:
            cpu = cpu_interval(
                self._start_stat, self._start_children, final, children, duration
            )
        else:
            # Closing during warmup has no steady interval. Preserve warmup
            # measurements and leave steady CPU/RSS unavailable, not zero.
            self._warmup_report = self._warmup_metrics(end, final, children)
            cpu = {key: None for key in cpu_interval(None, {}, None, {}, 0)}
        completed = self._begun and duration >= self.seconds
        peak_rss = max(self._rss, default=None)
        family_rss = self._max_family_rss if self._begun else None
        first_sample = self.window.first_sample_s
        launch_sample = (
            self.window.started + first_sample - self.launch_started
            if first_sample is not None and self.launch_started is not None
            else None
        )
        startup = getattr(self.window, "first_frame_s", None)
        self.report = {
            "completed": completed,
            "stop_reason": stop_reason,
            "mode": self.mode,
            "source": "synthetic layout fixture" if self.stress else "live sampler",
            "requested_duration_s": self.seconds,
            "measured_duration_s": round(duration, 4),
            "elapsed_since_verifier_start_s": round(end - self._warmup_start, 4),
            "warmup": self._warmup_report,
            "sample_interval_s": self.window.interval,
            "pid": os.getpid(),
            "cpu_denominator": "100% = one fully occupied logical CPU",
            **cpu,
            "cpu_accounting_note": "Shell includes all threads and verifier overhead. Reaped-child counters and current descendants are included separately; extremely short un-reaped child races can escape the final snapshot.",
            "rss_source": "/proc/<pid>/status VmRSS; not resource.ru_maxrss",
            "rss_observation_interval_s": 1.0,
            "max_shell_rss_bytes": peak_rss,
            "max_shell_rss_mb": round(peak_rss / 1_000_000, 3)
            if peak_rss is not None
            else None,
            "max_shell_rss_mib": round(peak_rss / 1048576, 3)
            if peak_rss is not None
            else None,
            "last_shell_rss_bytes": self._rss[-1] if self._rss else None,
            "max_simultaneous_children_rss_bytes": self._max_child_rss
            if self._begun
            else None,
            "max_family_rss_bytes": family_rss,
            "max_family_rss_mb": round(family_rss / 1_000_000, 3)
            if family_rss is not None
            else None,
            "max_family_rss_mib": round(family_rss / 1048576, 3)
            if family_rss is not None
            else None,
            "observed_children": list(self._child_peaks.values()),
            "first_sample_from_window_creation_s": first_sample,
            "launch_to_first_sample_s": launch_sample,
            "first_frame_s_from_cli": startup,
            "snapshots_during_measurement": self._snapshots
            - self._snapshot_count_at_start,
            # Retain the old JSON key for evidence consumers, while spelling
            # out the new workload-only meaning and separate process inventory.
            "observed_job_count_range": [self._min_jobs, self._max_jobs],
            "observed_workload_count_range": [self._min_jobs, self._max_jobs],
            "job_count_definition": "Recognized workload processes; excludes hosts, helpers and unclassified processes",
            "observed_process_count_range": [self._min_processes, self._max_processes],
            "observed_process_roles": sorted(self._process_roles),
            "distinct_job_counts": sorted(self._job_counts),
            "levels_seen": sorted(self._seen_levels),
            "all_observed_samples_proved": self._all_samples_proved
            and self._snapshots > self._snapshot_count_at_start,
            "layout_baselines": self._layouts,
            "layout_stable": not self._violations if self._layouts else None,
            "layout_violations": self._violations,
            "widget_ids_initial": self._baseline_widget_ids,
            "paint_events_observed": self._paint_count,
            "window_resize_events": self._resize_events,
            "max_observer_tick_gap_s": round(self._max_tick_gap, 4),
            "visual_no_blink_result": "UNVERIFIED by counters; inspect the accompanying screen recording",
            "first_time_viewer_and_three_metre_tests": "Owner confirmation required",
        }
        self.done = True
        self._timer.stop()
        self._sampler_timer.stop()
        self._finish_timer.stop()
        self.window.apply_snapshot = self._original_apply
        self.window.interval = self._original_interval
        self.window.removeEventFilter(self)
        QApplication.instance().aboutToQuit.disconnect(self._application_quitting)
        if self._source:
            self._source.close()
        if self.output:
            self.output.parent.mkdir(parents=True, exist_ok=True)
            self.output.write_text(
                json.dumps(self.report, indent=2) + "\n", encoding="utf-8"
            )
        self.finished.emit(self.report)
        if self.quit_on_finish and close_window:
            self.window.close()
            QApplication.instance().quit()
        return self.report
