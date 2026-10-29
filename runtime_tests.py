"""Safety and persistence regressions; no test sends a real process signal.

Run with ``python runtime_tests.py``. The UI's --selftest can call run_tests().
An injected clock makes the exact four-second boundary deterministic. Fake
pidfds let us model process exit/reuse races without disturbing working jobs.
"""

from __future__ import annotations

import json
from pathlib import Path
import signal
import tempfile
import unittest
from unittest import mock

from runtime import (
    InstanceLock,
    Notifications,
    Settings,
    TerminationController,
    read_process_starttime,
    show_already_running_hint,
)


class Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


class TerminationTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.starts = {42: 1200, 43: 1300}
        self.opened, self.closed, self.sent = [], [], []

        def open_fd(pid, flags):
            fd = 100 + len(self.opened)
            self.opened.append((pid, flags, fd))
            return fd

        self.controller = TerminationController(
            clock=self.clock,
            identity_reader=self.starts.get,
            pidfd_open=open_fd,
            sender=lambda *args: self.sent.append(args),
            fd_close=self.closed.append,
        )
        self.job = {"pid": 42, "starttime": 1200}
        self.controller.select(self.job)

    def tearDown(self):
        self.controller.close()

    def test_first_click_only_arms_second_sends_sigterm_once(self):
        self.assertEqual(self.controller.click().status, "armed")
        self.assertEqual(self.sent, [])
        self.assertTrue(self.controller.armed)
        self.assertEqual(self.controller.remaining, 4.0)
        self.clock.now += 3.999
        result = self.controller.click()
        self.assertTrue(result.sent)
        self.assertEqual(self.sent, [(100, signal.SIGTERM, None, 0)])
        self.assertEqual(self.closed, [100])
        self.assertFalse(self.controller.armed)
        self.assertFalse(self.controller.confirm().sent)
        self.assertEqual(len(self.sent), 1)

    def test_exact_four_second_deadline_expires(self):
        self.controller.arm()
        self.clock.now += 4.0
        self.assertEqual(self.controller.confirm().status, "expired")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.closed, [100])
        self.assertEqual(self.controller.click().status, "armed")
        self.assertEqual(self.sent, [])

    def test_selection_change_disarms(self):
        self.controller.arm()
        self.controller.select({"pid": 43, "starttime": 1300})
        self.assertFalse(self.controller.armed)
        self.assertFalse(self.controller.confirm().sent)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.closed, [100])

    def test_reselecting_same_identity_keeps_confirmation(self):
        self.controller.arm()
        self.controller.select(dict(self.job))
        self.assertTrue(self.controller.armed)
        self.controller.reconcile([self.job])
        self.assertTrue(self.controller.armed)

    def test_missing_job_cancels(self):
        self.controller.arm()
        self.controller.reconcile([])
        self.assertFalse(self.controller.armed)
        self.assertIsNone(self.controller.selected)
        self.assertFalse(self.controller.confirm().sent)

    def test_reused_pid_is_not_the_selected_job(self):
        self.controller.arm()
        self.starts[42] = 9999
        self.assertEqual(self.controller.confirm().status, "unavailable")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.closed, [100])

    def test_reused_pid_in_new_snapshot_cancels_selection(self):
        self.controller.arm()
        self.controller.reconcile([{"pid": 42, "starttime": 9999}])
        self.assertIsNone(self.controller.selected)
        self.assertEqual(self.sent, [])

    def test_pid_replacement_between_identity_check_and_open_is_rejected(self):
        def replacing_open(pid, flags):
            self.starts[pid] = 9999
            return 987

        self.controller._pidfd_open = replacing_open
        self.assertEqual(self.controller.arm().status, "unavailable")
        self.assertFalse(self.controller.armed)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.closed, [987])

    def test_live_exit_after_arm_does_not_signal(self):
        self.controller.arm()
        del self.starts[42]
        self.assertEqual(self.controller.confirm().status, "unavailable")
        self.assertEqual(self.sent, [])

    def test_delayed_identity_read_cannot_send_after_deadline(self):
        self.controller.arm()

        def slow_identity(pid):
            self.clock.now += 4.1
            return self.starts[pid]

        self.controller._identity_reader = slow_identity
        self.assertEqual(self.controller.confirm().status, "expired")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.closed, [100])

    def test_signal_race_with_exit_is_handled_and_fd_closed(self):
        def exited(*args):
            raise ProcessLookupError()

        self.controller._sender = exited
        self.controller.arm()
        self.assertEqual(self.controller.confirm().status, "unavailable")
        self.assertFalse(self.controller.armed)
        self.assertEqual(self.closed, [100])

    def test_permission_error_is_safe(self):
        def denied(*args):
            raise PermissionError()

        self.controller._sender = denied
        self.controller.arm()
        self.assertEqual(self.controller.confirm().status, "denied")
        self.assertEqual(self.closed, [100])

    def test_missing_pidfd_support_disables_action(self):
        self.controller._pidfd_open = None
        self.assertEqual(self.controller.click().status, "disabled")
        self.assertEqual(self.opened, [])
        self.assertEqual(self.sent, [])

    def test_demo_and_test_modes_never_open_process_or_send(self):
        for mode in ("demo", "test"):
            self.controller.mode = mode
            self.assertEqual(self.controller.click().status, "disabled")
        self.controller.mode = "live"
        self.controller.select({**self.job, "is_demo": True})
        self.assertEqual(self.controller.click().status, "disabled")
        self.controller.select(self.job, demo=True)
        self.assertEqual(self.controller.click().status, "disabled")
        self.assertEqual(self.opened, [])
        self.assertEqual(self.sent, [])

    def test_job_identity_is_immutable_after_selection(self):
        self.controller.select(self.job)
        self.job["pid"] = 43
        self.job["starttime"] = 1300
        self.assertEqual(self.controller.selected.pid, 42)
        self.controller.arm()
        self.controller.confirm()
        self.assertEqual(self.opened[0][0], 42)

    def test_invalid_identity_cannot_arm(self):
        for job in (
            {"pid": 1, "starttime": 42},
            {"pid": 42},
            {"pid": 42, "starttime": 0},
            {"pid": "bad", "starttime": 10},
        ):
            self.controller.select(job)
            self.assertEqual(self.controller.click().status, "unavailable")
        self.assertEqual(self.opened, [])

    def test_stat_parser_handles_spaces_and_parentheses(self):
        # Field 3 begins after the last ')', even when comm itself contains ')'.
        raw = "42 (a tricky) name) S " + " ".join(["0"] * 18 + ["12345", "0"])
        with mock.patch.object(Path, "read_text", return_value=raw):
            self.assertEqual(read_process_starttime(42), 12345)


class PersistenceTests(unittest.TestCase):
    def test_missing_state_round_trip_and_unknown_keys_survive_update(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(directory)
            self.assertEqual(settings.load(), {})
            self.assertTrue(settings.save({"geometry": "+12+34", "future_key": 7}))
            reloaded = Settings(directory)
            self.assertEqual(reloaded.load()["geometry"], "+12+34")
            self.assertTrue(reloaded.update(pinned=True))
            self.assertEqual(json.loads(reloaded.path.read_text())["future_key"], 7)
            self.assertEqual(list(Path(directory).glob(".state-*.tmp")), [])

    def test_serialization_failure_preserves_good_file(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(directory)
            settings.save({"pinned": True})
            before = settings.path.read_bytes()
            self.assertFalse(settings.save({"bad": object()}))
            self.assertEqual(before, settings.path.read_bytes())
            self.assertIsNotNone(settings.last_error)

    def test_invalid_state_falls_back_with_visible_error(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(directory)
            settings.path.write_text("{broken")
            self.assertEqual(settings.load(), {})
            self.assertIsNotNone(settings.last_error)

    def test_failed_rename_keeps_good_state_and_cleans_only_own_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(directory)
            settings.save({"pinned": True})
            before = settings.path.read_bytes()
            with mock.patch("runtime.os.replace", side_effect=OSError("read-only")):
                self.assertFalse(settings.save({"pinned": False}))
            self.assertEqual(before, settings.path.read_bytes())
            self.assertEqual(list(Path(directory).glob(".state-*.tmp")), [])

    def test_second_instance_preserves_pid_file_and_reacquire_after_release(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = InstanceLock(directory), InstanceLock(directory)
            self.assertTrue(first.acquire())
            before = first.path.read_bytes()
            self.assertFalse(second.acquire())
            self.assertTrue(second.already_running)
            self.assertEqual(before, first.path.read_bytes())
            first.release()
            self.assertTrue(second.acquire())
            second.release()


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.sent = []

        def notifier(*args):
            self.sent.append(args)
            return True

        self.notifications = Notifications(notifier=notifier, clock=self.clock)

    @staticmethod
    def snapshot(level, **extras):
        return {
            "core": {"level": level, "valid": level != "UNKNOWN"},
            "mem": {"available": 12288},
            **extras,
        }

    def test_initial_green_is_quiet_initial_red_is_visible(self):
        self.assertFalse(self.notifications.on_level(self.snapshot("GREEN")))
        self.assertEqual(self.sent, [])
        other = Notifications(
            notifier=lambda *args: self.sent.append(args) or True, clock=self.clock
        )
        self.assertTrue(other.on_level(self.snapshot("RED")))
        self.assertEqual(self.sent[-1][2], "critical")

    def test_urgent_escalation_bypasses_twenty_second_cooldown(self):
        self.notifications.on_level(self.snapshot("GREEN"))
        self.notifications.on_level(self.snapshot("AMBER"))
        self.clock.now += 1
        self.assertTrue(self.notifications.on_level(self.snapshot("RED")))
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.sent[-1][2], "critical")
        self.assertFalse(self.notifications.on_level(self.snapshot("RED")))

    def test_cooldown_defers_routine_recovery_instead_of_losing_it(self):
        self.notifications.on_level(self.snapshot("RED"))
        self.clock.now += 1
        self.assertFalse(self.notifications.on_level(self.snapshot("GREEN")))
        self.clock.now += 19
        self.assertTrue(self.notifications.on_level(self.snapshot("GREEN")))
        self.assertEqual(self.sent[-1][2], "low")

    def test_unknown_input_cancels_pending_recovery(self):
        self.notifications.on_level(self.snapshot("RED"))
        self.clock.now += 1
        self.notifications.on_level(self.snapshot("GREEN"))
        self.notifications.on_level(self.snapshot("UNKNOWN"))
        self.clock.now += 30
        self.assertFalse(self.notifications.on_level(self.snapshot("UNKNOWN")))
        self.assertEqual(len(self.sent), 1)

    def test_toggle_and_demo_do_not_emit_notifications(self):
        self.notifications.enabled = False
        self.notifications.on_level(self.snapshot("RED"))
        self.notifications.enabled = True
        self.notifications.on_level(self.snapshot("AMBER", demo=True))
        self.assertEqual(self.sent, [])

    def test_second_launch_helper_reports_visible_hint_request(self):
        calls = []
        self.assertTrue(
            show_already_running_hint(lambda *args: calls.append(args) or True)
        )
        self.assertIn("already running", calls[0][0])


def run_tests(verbosity=2) -> bool:
    suite = unittest.defaultTestLoader.loadTestsFromModule(__import__(__name__))
    return unittest.TextTestRunner(verbosity=verbosity).run(suite).wasSuccessful()


if __name__ == "__main__":
    unittest.main(verbosity=2)
