"""Focused CLI/capture regressions; run with QT_QPA_PLATFORM=offscreen.

No test opens a visible window or sends process signals. The launcher regression
renders one inert screenshot into a temporary directory and checks its honest
fallback footer, exercising the actual environment precedence that was broken.
"""

import math
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import backend
import main as entry


class DemoCoherenceTests(unittest.TestCase):
    def test_every_demo_has_consistent_memory_counts_and_core_sizes(self):
        for state, level in (
            ("red", "RED"),
            ("amber", "AMBER"),
            ("green", "GREEN"),
            ("empty", "GREEN"),
        ):
            with self.subTest(state=state):
                snap = entry.demo_for_state(state)
                self.assertTrue(snap["demo"])
                self.assertEqual(snap["core"]["level"], level)
                self.assertLessEqual(
                    snap["agent_mb"] + snap["mem"]["available"], snap["mem"]["total"]
                )
                self.assertEqual(
                    snap["agent_mb"], sum(owner["mem"] for owner in snap["owners"])
                )
                self.assertEqual(
                    len(snap["agent_jobs"]),
                    sum(owner["count"] for owner in snap["owners"]),
                )
                decisions = [snap["core"]]
                for owner in snap["owners"]:
                    self.assertEqual(owner["count"], len(owner["jobs"]))
                    self.assertEqual(owner["active_count"], owner["count"])
                    self.assertEqual(owner["workload_count"], owner["count"])
                    self.assertEqual(owner["process_count"], len(owner["processes"]))
                    self.assertTrue(all(p["is_demo"] for p in owner["processes"]))
                    self.assertEqual(
                        owner["jobs"],
                        [p for p in owner["processes"] if p["role"] == "workload"],
                    )
                    self.assertEqual(
                        owner["workload_mb"], sum(p["mem"] for p in owner["jobs"])
                    )
                    self.assertEqual(
                        owner["mem"], sum(p["mem"] for p in owner["processes"])
                    )
                    # Every byte is visible in exactly one role subtotal, even
                    # when its process is excluded from the workload average.
                    self.assertEqual(
                        owner["mem"],
                        sum(
                            owner[role + "_mb"]
                            for role in ("workload", "host", "helper", "unclassified")
                        ),
                    )
                    if owner["count"]:
                        self.assertAlmostEqual(
                            owner["mean_mb"], owner["workload_mb"] / owner["count"]
                        )
                        self.assertEqual(
                            owner["capacity"]["jobSize"], math.ceil(owner["mean_mb"])
                        )
                        self.assertEqual(
                            owner["planning_capacity"]["jobSize"],
                            max(
                                math.ceil(owner["mean_mb"]),
                                max(job["mem"] for job in owner["jobs"]),
                            ),
                        )
                        self.assertEqual(
                            owner["capacity_room"], owner["capacity"]["room"]
                        )
                        decisions.extend(
                            (owner["capacity"], owner["planning_capacity"])
                        )
                    else:
                        self.assertIsNone(owner["capacity"])
                        self.assertIsNone(owner["planning_capacity"])
                self.assertEqual(
                    snap["proved"], all(verdict["proved"] for verdict in decisions)
                )
                if state == "empty":
                    self.assertEqual(snap["agent_jobs"], [])
                    self.assertEqual(snap["owners"], [])
                    self.assertIsNone(snap["job_size"])
                    self.assertIsNone(snap["core"]["room"])
                    self.assertIsNone(snap["core"]["jobSize"])
                    self.assertFalse(snap["core"]["capacity_valid"])
                    self.assertTrue(snap["core"]["valid"])

    def test_demo_scaling_preserves_roles_and_excludes_host_overhead(self):
        red = entry.demo_for_state("red")
        roles = {
            tuple(p["identity"]): p["role"]
            for owner in red["owners"]
            for p in owner["processes"]
        }
        for state in ("green", "amber"):
            with self.subTest(state=state):
                snap = entry.demo_for_state(state)
                self.assertEqual(
                    roles,
                    {
                        tuple(p["identity"]): p["role"]
                        for owner in snap["owners"]
                        for p in owner["processes"]
                    },
                )
                claude = next(o for o in snap["owners"] if o["kind"] == "Claude Code")
                self.assertEqual(claude["count"], 15)
                self.assertGreater(claude["host_mb"], 0)
                self.assertLess(claude["mean_mb"], claude["mem"] / claude["count"])
                self.assertEqual(
                    snap["job_size"],
                    math.ceil(
                        sum(p["mem"] for p in snap["agent_jobs"])
                        / len(snap["agent_jobs"])
                    ),
                )

    def test_quieter_demo_scales_workload_not_just_the_free_ram_number(self):
        red = entry.demo_for_state("red")
        amber = entry.demo_for_state("amber")
        green = entry.demo_for_state("green")
        self.assertLess(green["agent_mb"], amber["agent_mb"])
        self.assertLess(amber["agent_mb"], red["agent_mb"])
        self.assertNotEqual(green["job_size"], red["job_size"])
        self.assertIn("synthetic", green["demo_label"])

    def test_helper_only_demo_keeps_memory_without_inventing_job_capacity(self):
        fixture = backend.make_demo_snapshot()
        for owner in fixture["owners"]:
            owner["processes"] = [
                process
                for process in owner["processes"]
                if process["role"] != "workload"
            ]
        core = backend.CoreClient()
        try:
            backend.recompute_workload_metrics(fixture, core)
        finally:
            core.close()
        with patch.object(backend, "make_demo_snapshot", return_value=fixture):
            snap = entry.demo_for_state("green")
        self.assertGreater(snap["agent_mb"], 0)
        self.assertEqual(snap["agent_jobs"], [])
        self.assertIsNone(snap["job_size"])
        self.assertTrue(snap["core"]["valid"])
        self.assertEqual(snap["core"]["level"], "GREEN")
        self.assertFalse(snap["core"]["capacity_valid"])
        self.assertIsNone(snap["core"]["room"])
        for owner in snap["owners"]:
            self.assertEqual(owner["count"], 0)
            self.assertIsNone(owner["mean_mb"])
            self.assertIsNone(owner["capacity"])
            self.assertTrue(all(p["is_demo"] for p in owner["processes"]))


class CoreDirectoryTests(unittest.TestCase):
    def test_explicit_directory_overrides_existing_launcher_binary(self):
        with tempfile.TemporaryDirectory(prefix="swarm-core-override-") as directory:
            missing = Path(directory) / "missing-core"
            with patch.dict(
                os.environ,
                {
                    "SWARM_METER_CORE": "/previous/core",
                    "SWARM_METER_CORE_DIR": "/previous",
                },
            ):
                entry.configure_core_directory(str(missing))
                core = backend.CoreClient()
                try:
                    self.assertEqual(core.core_dir, missing)
                    self.assertEqual(core.core_bin, missing / ".lake/build/bin/core")
                    verdict = core.ask(
                        63727, 46089, 0, 1024, backend.derive_policy(63727, 2047)
                    )
                    self.assertFalse(verdict["proved"])
                    self.assertTrue(verdict["valid"])
                finally:
                    core.close()

    def test_omitted_directory_preserves_launcher_defaults(self):
        with patch.dict(
            os.environ,
            {"SWARM_METER_CORE": "/existing/core", "SWARM_METER_CORE_DIR": "/existing"},
        ):
            entry.configure_core_directory(None)
            self.assertEqual(os.environ["SWARM_METER_CORE"], "/existing/core")
            self.assertEqual(os.environ["SWARM_METER_CORE_DIR"], "/existing")

    def test_real_launcher_missing_directory_produces_fallback_screenshot(self):
        folder = Path(__file__).resolve().parent
        # Use the same sibling-core resolver in a checkout or installed package.
        valid_binary = backend.CoreClient().core_bin
        self.assertTrue(
            valid_binary.is_file(),
            "Build the configured Lean core before this integration test",
        )
        with tempfile.TemporaryDirectory(prefix="swarm-launcher-capture-") as directory:
            root = Path(directory)
            output = root / "capture.png"
            env = dict(
                os.environ,
                QT_QPA_PLATFORM="offscreen",
                SWARM_METER_DATA_DIR=str(root / "data"),
                SWARM_METER_CORE=str(valid_binary),
            )
            result = subprocess.run(
                [
                    "bash",
                    str(folder / "run.sh"),
                    "--core-dir",
                    str(root / "missing"),
                    "--screenshot",
                    str(output),
                    "--view",
                    "glance",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            log = (
                (root / "data/widget.log").read_text()
                if (root / "data/widget.log").exists()
                else result.stdout + result.stderr
            )
            self.assertEqual(result.returncode, 0, log)
            self.assertEqual(output.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
            self.assertIn("FALLBACK", log)
            self.assertNotIn('"core": "PROVED', log)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


class CaptureWindow:
    def __init__(self):
        self.samples = []

    def apply_snapshot(self, snapshot):
        self.samples.append(snapshot)


class CaptureSampler:
    def __init__(self, clock, ready_at=None, pending_state="pending"):
        self.clock = clock
        self.ready_at = ready_at
        self.pending_state = pending_state

    def snapshot(self):
        ready = self.ready_at is not None and self.clock() >= self.ready_at
        return {
            "gpu": {"used": 512, "total": 24000} if ready else None,
            "gpu_status": "current" if ready else self.pending_state,
            "gpu_pending": not ready and self.pending_state == "pending",
            "mem": {"available": 1000 - int(self.clock() * 100)},
        }


class LiveCaptureTests(unittest.TestCase):
    def test_slow_healthy_gpu_is_applied_before_capture(self):
        clock, window = Clock(), CaptureWindow()
        sampler = CaptureSampler(clock, ready_at=3.0)
        report = entry.collect_live_capture(
            window, sampler, clock=clock, sleep=clock.sleep
        )
        self.assertTrue(report["gpu_ready"])
        self.assertFalse(report["timed_out"])
        self.assertGreaterEqual(report["elapsed_s"], 3.0)
        self.assertLess(report["elapsed_s"], 4.0)
        self.assertEqual(window.samples[-1]["gpu"]["used"], 512)
        self.assertEqual(window.samples[-1]["mem"]["available"], 700)

    def test_deadline_displays_unavailable_not_a_permanent_reading_spinner(self):
        clock, window = Clock(), CaptureWindow()
        report = entry.collect_live_capture(
            window, CaptureSampler(clock), timeout_s=2.0, clock=clock, sleep=clock.sleep
        )
        self.assertTrue(report["timed_out"])
        self.assertFalse(report["gpu_ready"])
        self.assertLessEqual(report["elapsed_s"], 2.0)
        self.assertIsNone(window.samples[-1]["gpu"])
        self.assertFalse(window.samples[-1]["gpu_pending"])
        self.assertEqual(window.samples[-1]["gpu_status"], "unavailable")
        self.assertTrue(window.samples[-1]["gpu_capture_timed_out"])

    def test_transient_unavailable_can_recover_within_capture_deadline(self):
        clock, window = Clock(), CaptureWindow()
        sampler = CaptureSampler(clock, ready_at=2.0, pending_state="unavailable")
        report = entry.collect_live_capture(
            window, sampler, clock=clock, sleep=clock.sleep
        )
        self.assertTrue(report["gpu_ready"])
        self.assertEqual(report["elapsed_s"], 2.0)

    def test_fast_gpu_still_gets_three_samples(self):
        clock, window = Clock(), CaptureWindow()
        report = entry.collect_live_capture(
            window, CaptureSampler(clock, ready_at=0), clock=clock, sleep=clock.sleep
        )
        self.assertEqual(report["samples"], 3)
        self.assertEqual(len(window.samples), 3)
        self.assertTrue(report["gpu_ready"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
