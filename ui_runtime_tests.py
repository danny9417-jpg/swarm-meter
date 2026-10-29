"""Inspector lifecycle regressions using injected clocks and fake pidfds.

No test opens a real process descriptor or delivers a signal. The UI is created
with no sampler, separate temporary settings, and muted notifications. A fake
controller permits checking the same arm/confirm UI path used by live jobs.
"""

from __future__ import annotations

import copy
import tempfile
import unittest

from PySide6.QtWidgets import QApplication, QDialog, QPushButton
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest

import backend
from runtime import TerminationController
from ui import OwnerInspector, SwarmWindow, style_application


class InspectorRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        style_application(cls.app)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="swarm-inspector-test-")
        self.window = SwarmWindow(
            preview=False,
            start_worker=False,
            data_dir=self.directory.name,
            view="expanded",
        )
        self.window.notifications.enabled = False
        self.snap = backend.make_demo_snapshot()
        self.window.apply_snapshot(self.snap)
        self.job = dict(self.snap["agent_jobs"][0], is_demo=False)
        self.now = 100.0
        self.signals, self.closed = [], []
        # Identity and signal functions are all fake. The large demo PID is
        # never passed to any OS process operation, even if it happens to exist.
        self.window.termination = TerminationController(
            clock=lambda: self.now,
            identity_reader=lambda pid: self.job["starttime"],
            pidfd_open=lambda pid, flags: 4567,
            sender=lambda *args: self.signals.append(args),
            fd_close=self.closed.append,
        )
        self.window.show()
        self.window.inspect_job(self.job)
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.app.processEvents()
        self.directory.cleanup()

    def test_hive_shrink_keeps_survivors_visible(self):
        hive = self.window.hive
        hive.resize(408, 160)
        jobs = [
            dict(self.job, identity=(90000 + i, i), pid=90000 + i) for i in range(40)
        ]
        hive.set_jobs(jobs, None)
        early = dict(hive.slots)
        hive.set_jobs(jobs[:2] + jobs[-10:], None)
        self.app.processEvents()
        limit = max(1, hive.width() // 51) * 3
        self.assertTrue(all(slot < limit for slot in hive.slots.values()))
        self.assertEqual(
            hive.slots[tuple(jobs[0]["identity"])], early[tuple(jobs[0]["identity"])]
        )
        self.assertEqual(len(set(hive.slots.values())), 12)

    def test_selected_helper_only_swarm_does_not_borrow_another_mean(self):
        snap = copy.deepcopy(self.snap)
        host = copy.deepcopy(snap["owners"][0])
        host.update(
            key="helper-only",
            label="Codex helper-only",
            count=0,
            jobs=[],
            mean_mb=None,
            capacity=None,
            planning_capacity=None,
            helper_count=15,
            unclassified_count=1,
        )
        snap["owners"].insert(0, host)
        self.window.apply_snapshot(snap)
        self.window.owner_key = "helper-only"
        self.assertEqual(self.window.capacity_owner()["key"], "helper-only")
        self.assertIsNone(self.window.capacity_size())
        self.assertEqual(
            self.window.capacity_title(self.window.capacity()),
            "No observed job size yet",
        )
        self.assertIn("15 helpers", self.window.no_workload_summary(host))

    def test_owner_details_refresh_helpers_without_replacing_unchanged_buttons(self):
        snap = copy.deepcopy(self.snap)
        o = next(o for o in snap["owners"] if o["jobs"])
        helper = dict(
            o["jobs"][0],
            pid=9876543,
            identity=(9876543, 9876543),
            starttime=9876543,
            name="decomp",
            role="helper",
            mem=59,
            role_reason="Known MCP connector",
        )
        o["processes"] = list(o.get("processes", o["jobs"])) + [helper]
        o["helper_count"] = 1
        o["helper_mb"] = 59
        self.window.apply_snapshot(snap)
        dialog = OwnerInspector(self.window, o["key"])
        try:
            button = dialog.rows[helper["identity"]]
            self.assertIn("Helpers", button.text())
            self.assertIn("59 MiB", dialog.breakdown.text())
            helper["mem"] = 80
            o["helper_mb"] = 80
            dialog.refresh()
            self.assertIs(dialog.rows[helper["identity"]], button)
            self.assertIn("80 MiB", button.text())
            self.assertIn("80 MiB", dialog.breakdown.text())
        finally:
            dialog.close()

    def test_helper_inspector_stays_live_outside_workload_hive(self):
        snap = copy.deepcopy(self.snap)
        o = next(o for o in snap["owners"] if o["jobs"])
        helper = dict(
            self.job,
            pid=9876543,
            identity=(9876543, 9876543),
            starttime=9876543,
            owner=o["key"],
            name="decomp",
            role="helper",
            role_reason="Known MCP connector",
            mem=59,
        )
        o["processes"] = list(o.get("processes", o["jobs"])) + [helper]
        self.window.apply_snapshot(snap)
        self.window.inspect_job(helper)
        self.window.apply_snapshot(snap)
        self.assertIn("Helpers", self.window.inspector.status.text())
        self.assertNotIn("exited", self.window.inspector.action_status.text())
        self.assertNotIn(helper["identity"], self.window.hive.slots)

    def test_unreadable_workload_memory_renders_without_fabricated_capacity(self):
        snap = copy.deepcopy(self.snap)
        o = next(o for o in snap["owners"] if o["jobs"])
        missing = o["jobs"][0]
        missing["mem"] = None
        # Job and process lists share objects in real snapshots. Recount using
        # the same adapter as sampling so unknown memory suspends this mean.
        core = backend.CoreClient()
        try:
            backend.recompute_workload_metrics(snap, core)
        finally:
            core.close()
        self.window.owner_key = o["key"]
        self.window.apply_snapshot(snap)
        self.window.inspect_job(missing)
        self.app.processEvents()
        self.assertIsNone(self.window.capacity())
        self.assertIn("Unavailable", self.window.inspector.memory.text())
        # Grabbing repaints the hive; None must not reach arithmetic/comparisons.
        self.assertFalse(self.window.grab().isNull())
        dialog = OwnerInspector(self.window, o["key"])
        dialog.close()

    def test_inspector_refreshes_same_identity_after_exec(self):
        inspector = self.window.inspector
        changed = dict(self.job, name="new-worker", cmd="new-worker --different")
        inspector.refresh_job(changed)
        self.assertEqual(inspector.title.text(), "new-worker")
        self.assertEqual(inspector.command.toPlainText(), "new-worker --different")
        inspector.command.selectAll()
        selected = inspector.command.textCursor().selectedText()
        inspector.refresh_job(changed)
        self.assertEqual(inspector.command.textCursor().selectedText(), selected)
        inspector.refresh_job(dict(changed, kind="Desktop", orphan_evidence=None))
        self.assertIn("No agent host attributed", inspector.status.text())

    def test_keyboard_owner_activation_opens_job_inspector(self):
        self.window.inspector.close()
        index = next(i for i, row in enumerate(self.window.model.rows) if row["jobs"])
        self.window.table.setCurrentIndex(self.window.model.index(index, 0))
        self.window.table.setFocus()
        opened = []

        def choose_job():
            dialog = QApplication.activeModalWidget()
            opened.append(isinstance(dialog, QDialog))
            if dialog:
                buttons = [
                    b for b in dialog.findChildren(QPushButton) if "PID " in b.text()
                ]
                if buttons:
                    buttons[0].click()
                else:
                    dialog.reject()

        QTimer.singleShot(30, choose_job)
        QTest.keyClick(self.window.table, Qt.Key.Key_Return)
        self.app.processEvents()
        self.assertEqual(opened, [True])
        self.assertTrue(self.window.inspector.isVisible())

    def test_expanding_at_screen_edge_stays_on_screen(self):
        self.window.inspector.close()
        self.window.set_view("glance", save=False)
        bounds = self.window.screen().availableGeometry()
        self.window.move(bounds.right() - 424 + 1, bounds.bottom() - 408 + 1)
        self.app.processEvents()
        self.window.set_view("expanded", save=False)
        self.app.processEvents()
        self.assertTrue(bounds.contains(self.window.frameGeometry()))

    def test_escape_reject_disarms_and_reopening_requires_fresh_first_click(self):
        inspector = self.window.inspector
        inspector.action()
        self.assertTrue(self.window.termination.armed)
        inspector.reject()  # This is the QDialog Escape path, not closeEvent.
        self.app.processEvents()
        self.assertFalse(self.window.termination.armed)
        self.window.inspect_job(self.job)
        inspector.action()
        self.assertTrue(self.window.termination.armed)
        self.assertEqual(self.signals, [])

    def test_close_disarms(self):
        self.window.inspector.action()
        self.window.inspector.close()
        self.app.processEvents()
        self.assertFalse(self.window.termination.armed)
        self.assertEqual(self.signals, [])

    def test_confirmation_expiry_updates_visible_status(self):
        inspector = self.window.inspector
        inspector.action()
        self.now += 4.0
        inspector.refresh_action()
        self.assertFalse(self.window.termination.armed)
        self.assertIn("expired", inspector.action_status.text().lower())
        self.assertNotIn("CONFIRM", inspector.stop.text())
        self.assertEqual(self.signals, [])

    def test_cancel_control_keeps_layout_space_while_disarmed(self):
        inspector = self.window.inspector
        before = (
            inspector.size(),
            inspector.cancel.geometry(),
            inspector.stop.geometry(),
        )
        self.assertTrue(inspector.cancel.isVisible())
        self.assertFalse(inspector.cancel.isEnabled())
        inspector.action()
        self.app.processEvents()
        self.assertTrue(inspector.cancel.isEnabled())
        self.assertEqual(
            before,
            (inspector.size(), inspector.cancel.geometry(), inspector.stop.geometry()),
        )

    def test_disappearing_job_cancels_and_disables_action(self):
        inspector = self.window.inspector
        inspector.action()
        empty = copy.deepcopy(self.snap)
        empty["agent_jobs"] = []
        self.window.apply_snapshot(empty)
        inspector.refresh_action()
        self.assertFalse(self.window.termination.armed)
        self.assertFalse(inspector.stop.isEnabled())
        self.assertEqual(self.signals, [])


def run_tests(verbosity=2) -> bool:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(InspectorRuntimeTests)
    return unittest.TextTestRunner(verbosity=verbosity).run(suite).wasSuccessful()


if __name__ == "__main__":
    unittest.main(verbosity=2)
