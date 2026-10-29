"""Validation shutdown tests: offscreen Qt, inert previews, no real signals."""

import json
import math
from pathlib import Path
import tempfile
import unittest

from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from ui import SwarmWindow, style_application
from validation import StressSource, Verifier


class StressWorkloadTests(unittest.TestCase):
    def test_stress_counts_workloads_separately_from_idle_helpers(self):
        source = StressSource()
        try:
            seen_roles = set()
            small_workloads = []
            for expected in (0, 1, 9, 15, 64, 130, 4, 25):
                with self.subTest(workloads=expected):
                    snap = source.snapshot()
                    self.assertTrue(snap["demo"])
                    self.assertTrue(snap["proved"])
                    self.assertEqual(len(snap["agent_jobs"]), expected)
                    self.assertEqual(snap["nproc"], expected + 22 if expected else 0)
                    processes = [p for o in snap["owners"] for p in o["processes"]]
                    self.assertEqual(len(processes), snap["nproc"])
                    self.assertTrue(all(p["is_demo"] for p in processes))
                    seen_roles.update(p["role"] for p in processes)
                    small_workloads.extend(
                        p for p in snap["agent_jobs"] if p["mem"] < 48
                    )
                    self.assertTrue(
                        all(p["role"] == "workload" for p in snap["agent_jobs"])
                    )
                    for owner in snap["owners"]:
                        self.assertEqual(
                            owner["mem"], sum(p["mem"] for p in owner["processes"])
                        )
                        self.assertEqual(owner["count"], len(owner["jobs"]))
                        if owner["count"]:
                            mean = sum(p["mem"] for p in owner["jobs"]) / owner["count"]
                            self.assertEqual(owner["mean_mb"], mean)
                            self.assertEqual(
                                owner["capacity"]["jobSize"], math.ceil(mean)
                            )
                        else:
                            self.assertIsNone(owner["capacity"])
                            self.assertIsNone(owner["mean_mb"])
                    if expected == 1:
                        # This is the regression scene: Codex's helpers consume
                        # RAM, but only Claude has one observed local workload.
                        codex = next(o for o in snap["owners"] if o["kind"] == "Codex")
                        self.assertEqual(codex["helper_count"], 15)
                        self.assertEqual(codex["unclassified_count"], 1)
                        self.assertEqual(codex["count"], 0)
                        self.assertIsNone(codex["capacity"])
            self.assertEqual(seen_roles, {"workload", "host", "helper", "unclassified"})
            self.assertTrue(
                small_workloads, "Scripted work must not inherit the old 48 MiB cutoff"
            )
        finally:
            source.close()


class VerifierShutdownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        style_application(cls.app)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="swarm-verifier-test-")
        self.output = Path(self.directory.name) / "result.json"
        self.window = SwarmWindow(
            preview=True, data_dir=self.directory.name, start_worker=False
        )
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.app.processEvents()
        self.directory.cleanup()

    def verifier(self, seconds=30, warmup=0):
        verifier = Verifier(
            self.window,
            seconds,
            warmup_s=warmup,
            output=self.output,
            quit_on_finish=False,
        )
        verifier.start()
        return verifier

    def test_close_during_warmup_saves_partial_without_fabricated_steady_metrics(self):
        verifier = self.verifier(warmup=10)
        self.window.close()
        report = json.loads(self.output.read_text())
        self.assertFalse(report["completed"])
        self.assertFalse(report["warmup"]["completed"])
        self.assertEqual(report["stop_reason"], "window_closed")
        self.assertEqual(report["measured_duration_s"], 0)
        self.assertIsNone(report["family_cpu_percent"])
        self.assertIsNone(report["max_shell_rss_bytes"])
        self.assertIsNone(report["layout_stable"])
        self.assertTrue(verifier.done)

    def test_close_during_measurement_preserves_actual_duration_and_metrics(self):
        verifier = self.verifier()
        QTest.qWait(350)
        self.assertTrue(verifier._begun)
        self.window.close()
        report = json.loads(self.output.read_text())
        self.assertFalse(report["completed"])
        self.assertTrue(report["warmup"]["completed"])
        self.assertGreater(report["measured_duration_s"], 0)
        self.assertLess(report["measured_duration_s"], 30)
        self.assertIsNotNone(report["family_cpu_percent"])
        self.assertGreater(report["max_shell_rss_bytes"], 0)
        # Repeated shutdown paths must not replace the preserved receipt.
        original = self.output.read_text()
        verifier.finish()
        self.assertEqual(self.output.read_text(), original)

    def test_application_exit_without_window_close_also_saves_partial(self):
        self.verifier(warmup=10)
        # exit() stops the loop directly; QApplication.quit() may first close
        # the top-level windows and therefore exercise the Close path instead.
        QTimer.singleShot(30, lambda: self.app.exit(0))
        self.app.exec()
        report = json.loads(self.output.read_text())
        self.assertFalse(report["completed"])
        self.assertEqual(report["stop_reason"], "application_quit")
        self.assertEqual(report["measured_duration_s"], 0)

    def test_full_duration_is_distinguished_from_early_shutdown(self):
        verifier = self.verifier(seconds=0.02)
        QTest.qWait(400)
        self.assertTrue(verifier.done)
        report = json.loads(self.output.read_text())
        self.assertTrue(report["completed"])
        self.assertGreaterEqual(report["measured_duration_s"], 0.02)
        self.assertEqual(report["stop_reason"], "duration_elapsed")

    def test_receipt_distinguishes_workload_count_from_all_processes(self):
        verifier = self.verifier()
        source = StressSource()
        try:
            source.snapshot()  # The next scene has one workload and 22 other processes.
            self.window.apply_snapshot(source.snapshot())
            QTest.qWait(350)
            self.window.close()
            report = json.loads(self.output.read_text())
            self.assertEqual(report["observed_workload_count_range"], [1, 1])
            self.assertEqual(report["observed_job_count_range"], [1, 1])
            self.assertEqual(report["observed_process_count_range"], [23, 23])
            self.assertEqual(
                report["observed_process_roles"],
                ["helper", "host", "unclassified", "workload"],
            )
            self.assertIn("excludes hosts", report["job_count_definition"])
            self.assertTrue(verifier.done)
        finally:
            source.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
