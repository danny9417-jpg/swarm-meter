"""CLI orchestration: the same views serve live use and inert screenshot tests."""

import argparse
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox
import backend
from config import VERSION
import runtime
from ui import SwarmWindow, style_application


def configure_core_directory(directory):
    """An explicit CLI directory overrides the launcher's default binary too."""
    if directory is None:
        return
    root = Path(directory).expanduser().resolve()
    os.environ["SWARM_METER_CORE_DIR"] = str(root)
    os.environ["SWARM_METER_CORE"] = str(root / ".lake/build/bin/core")


def demo_for_state(state="red"):
    """Scale synthetic memory, then reuse the backend's workload accounting.

    Host and helper processes remain part of an owner's total RAM, but do not
    inflate the count or the workload-only average. All scenes pass through the
    same backend accounting and Lean request path as a measured snapshot.
    """
    s = backend.make_demo_snapshot()
    if state == "red":
        return s
    s = copy.deepcopy(s)
    s["mem"]["available"] = {"green": 46089, "amber": 14336, "empty": 46089}[state]
    s["pressure"] = 0
    if state == "empty":
        s["owners"] = []
    else:
        # Leave 4 GiB for other software. Scale every process, including host
        # and helper overhead, without changing evidence or process roles.
        budget = max(0, s["mem"]["total"] - s["mem"]["available"] - 4096)
        scale = min(1.0, budget / max(1, s["agent_mb"]))
        for owner in s["owners"]:
            for process in owner["processes"]:
                process["mem"] = int(process["mem"] * scale)
    if state in ("green", "empty"):
        s["mem"]["swap_free"] = s["mem"]["swap_total"]
    s["demo_label"] = f"Illustrative {state} workload · synthetic values"
    core = backend.CoreClient()
    try:
        # There is deliberately no second role/mean/capacity policy in the CLI.
        # This also clears counts and capacities when the scene becomes empty.
        backend.recompute_workload_metrics(s, core)
    finally:
        core.close()
    return s


def collect_live_capture(
    window,
    sampler,
    *,
    timeout_s=8.0,
    poll_s=0.5,
    clock=time.monotonic,
    sleep=time.sleep,
):
    """Wait for asynchronous GPU readiness without turning it into fake zero.

    Cold NVML initialization can take several seconds. A live screenshot gets
    fresh RAM samples while it waits, then either captures a healthy GPU reading
    or explicitly records an unavailable/timed-out reading at a bounded deadline.
    Only this capture workflow waits; ordinary application launch stays async.
    """
    began = clock()
    deadline = began + max(0.0, timeout_s)
    samples = 0
    while True:
        snapshot = sampler.snapshot()
        samples += 1
        gpu_ready = (
            snapshot.get("gpu_status") == "current" and snapshot.get("gpu") is not None
        )
        remaining = deadline - clock()
        timed_out = remaining <= 0 and not gpu_ready
        if timed_out:
            snapshot = dict(snapshot)
            snapshot.update(
                gpu=None,
                gpu_pending=False,
                gpu_status="unavailable",
                gpu_capture_timed_out=True,
            )
        window.apply_snapshot(snapshot)
        if (gpu_ready and samples >= 3) or remaining <= 0:
            return {
                "elapsed_s": clock() - began,
                "samples": samples,
                "gpu_ready": gpu_ready,
                "timed_out": timed_out,
                "gpu_status": snapshot.get("gpu_status", "unavailable"),
            }
        sleep(min(max(0.01, poll_s), remaining))


def seed_history(window, snap):
    # The synthetic trace is never inserted into live SQLite history.
    for i in range(150):
        f = i / 150
        s = dict(snap)
        s["ts"] = snap["ts"] - 300 + i * 2
        s["mem"] = dict(
            snap["mem"], available=int(46089 * (1 - f) + snap["mem"]["available"] * f)
        )
        s["agent_mb"] = int(3000 * (1 - f) + snap["agent_mb"] * f)
        count = (
            6
            if i < 120
            else 6 + int((i - 120) / 30 * max(0, len(snap["agent_jobs"]) - 6))
        )
        s["agent_jobs"] = snap["agent_jobs"][:count]
        window.trends.add(s)


def selftest(app):
    import backend_tests
    import runtime_tests
    import ui_runtime_tests
    import gpu_reader_tests
    import main_tests
    import validation_tests
    import config_tests
    import portability_tests
    import install_tests

    suite = unittest.TestSuite(
        [
            unittest.defaultTestLoader.loadTestsFromModule(backend_tests),
            unittest.defaultTestLoader.loadTestsFromModule(runtime_tests),
            unittest.defaultTestLoader.loadTestsFromModule(ui_runtime_tests),
            unittest.defaultTestLoader.loadTestsFromModule(gpu_reader_tests),
            unittest.defaultTestLoader.loadTestsFromModule(main_tests),
            unittest.defaultTestLoader.loadTestsFromModule(validation_tests),
            unittest.defaultTestLoader.loadTestsFromModule(config_tests),
            unittest.defaultTestLoader.loadTestsFromModule(portability_tests),
            unittest.defaultTestLoader.loadTestsFromModule(install_tests),
        ]
    )
    if not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful():
        return 1
    with tempfile.TemporaryDirectory(prefix="swarm-ui-test-") as directory:
        w = SwarmWindow(
            preview=True, view="glance", data_dir=directory, start_worker=False
        )
        s = demo_for_state("red")
        seed_history(w, s)
        w.apply_snapshot(s)
        w.show()
        app.processEvents()
        assert (w.width(), w.height()) == (424, 408)
        assert w.snap["core"]["level"] == "RED" and w.capacity()["room"] == 0
        assert w.model.rowCount() == len(s["owners"])
        assert ("PROVED" in w.core_line()) == s["proved"]
        identity = id(w.model)
        size = w.size()
        for state in ["empty", "green", "amber", "red"]:
            w.apply_snapshot(demo_for_state(state))
            app.processEvents()
            assert id(w.model) == identity and w.size() == size
        w.set_view("expanded", save=False)
        app.processEvents()
        w.inspect_largest()
        app.processEvents()
        assert w.inspector and not w.inspector.stop.isEnabled()
        w.termination.click()
        assert not w.termination.armed
        w.set_view("glance", save=False)
        assert (w.width(), w.height()) == (424, 408)
        w.close()
        app.processEvents()
    print("selftest OK — backend, runtime and Qt views; no real signals sent")
    return 0


def main(argv=None):
    launch_started = float(
        os.environ.get("SWARM_METER_LAUNCH_STARTED", time.monotonic())
    )
    parser = argparse.ArgumentParser(description="Future Gadget No.21 — Swarm Meter")
    parser.add_argument(
        "--version", action="version", version="Swarm Meter — " + VERSION
    )
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--screenshot", metavar="PNG")
    parser.add_argument("--screenshot-live", metavar="PNG")
    parser.add_argument("--view", choices=["glance", "expanded", "inspector"])
    parser.add_argument("--demo", choices=["green", "amber", "red", "empty"])
    parser.add_argument("--data-dir")
    parser.add_argument("--core-dir")
    parser.add_argument("--quit-after", type=float)
    parser.add_argument("--benchmark", type=float)
    parser.add_argument("--warmup", type=float, default=10.0)
    parser.add_argument("--flicker-test", type=float)
    parser.add_argument("--live-flicker", type=float)
    parser.add_argument("--metrics-output")
    args = parser.parse_args(argv)
    if args.data_dir:
        os.environ["SWARM_METER_DATA_DIR"] = args.data_dir
    configure_core_directory(args.core_dir)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Swarm Meter")
    app.setDesktopFileName("swarm-meter")
    style_application(app)
    if args.selftest:
        return selftest(app)
    capture = args.screenshot or args.screenshot_live
    preview = bool(capture or args.demo or args.flicker_test)
    lock = None
    if not preview:
        lock = runtime.InstanceLock(args.data_dir)
        if not lock.acquire():
            if lock.already_running:
                runtime.show_already_running_hint()
                # A native hint remains visible even if desktop notifications are muted.
                hint = QMessageBox(
                    QMessageBox.Icon.Information,
                    "Swarm Meter",
                    "Swarm Meter is already running.",
                )
                hint.show()
                QTimer.singleShot(1800, app.quit)
                app.exec()
                return 0
            print(
                "Cannot acquire instance lock: " + str(lock.last_error), file=sys.stderr
            )
            return 1
    w = SwarmWindow(
        preview=preview,
        view="expanded" if args.view == "inspector" else args.view,
        data_dir=args.data_dir,
        start_worker=not preview,
    )
    if args.demo or args.screenshot or args.flicker_test:
        snap = demo_for_state(args.demo or "red")
        seed_history(w, snap)
        w.apply_snapshot(snap)
    capture_status = None
    if args.screenshot_live:
        sampler = backend.Sampler()
        try:
            capture_status = collect_live_capture(w, sampler)
        finally:
            sampler.close()
        w.source_label = "LIVE CAPTURE" + (
            " · GPU wait timed out" if capture_status["timed_out"] else ""
        )
    w.show()
    app.processEvents()
    w.first_frame_s = time.monotonic() - launch_started
    if not preview:
        # This receipt verifies what a Desktop launch actually executed. It is
        # local runtime metadata, not a second lock or a background service.
        receipt = {
            "pid": os.getpid(),
            "version": VERSION,
            "entrypoint": str(Path(__file__).resolve().with_name("swarm_meter.py")),
            "python": sys.executable,
            "window_id": int(w.winId()),
        }
        try:
            (runtime.data_directory(args.data_dir) / "running.json").write_text(
                json.dumps(receipt, indent=2) + "\n"
            )
        except OSError:
            pass
    if args.view == "inspector":
        w.inspect_largest()
    if capture:

        def save_capture():
            target = w.inspector if args.view == "inspector" and w.inspector else w
            path = Path(capture)
            path.parent.mkdir(parents=True, exist_ok=True)
            ok = target.grab().save(str(path), "PNG")
            print(
                json.dumps(
                    {
                        "screenshot": str(path),
                        "saved": ok,
                        "source": "live" if args.screenshot_live else "demo",
                        "core": w.core_line(),
                        "capture_wait": capture_status,
                    }
                )
            )
            w.close()
            app.exit(0 if ok else 1)

        QTimer.singleShot(100, save_capture)
    verifier = None
    if args.benchmark or args.flicker_test or args.live_flicker:
        from validation import Verifier

        result_path = Path(args.metrics_output or "validation.json")
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.with_suffix(".started.json").write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "window_id": int(w.winId()),
                    "width": w.width(),
                    "height": w.height(),
                    "first_frame_s": w.first_frame_s,
                    "source": "synthetic" if args.flicker_test else "live",
                },
                indent=2,
            )
        )
        verifier = Verifier(
            w,
            args.benchmark or args.flicker_test or args.live_flicker,
            mode="flicker" if args.flicker_test or args.live_flicker else "benchmark",
            output=str(result_path),
            stress=bool(args.flicker_test),
            launch_started=launch_started,
            warmup_s=args.warmup if args.benchmark else 0.0,
        )
        verifier.start()
    if args.quit_after:
        QTimer.singleShot(int(args.quit_after * 1000), w.close)
    status = app.exec()
    w.stop_event.set()
    if w.worker:
        w.worker.join(timeout=8)
    if lock:
        lock.release()
    return status
