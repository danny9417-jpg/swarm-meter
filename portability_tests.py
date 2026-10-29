"""Hardware/identity portability checks, without relying on this developer's PC.

The matrix exercises Linux's reported RAM/swap values and the unchanged Lean
protocol. The integration test runs only when the normal core discovery finds a
built executable; a missing compiler artifact is explicitly reported as skipped,
never described as a successful proof check. No real processes are signalled.
"""

import os
from pathlib import Path
import unittest
from unittest.mock import patch

import backend as b


RAM_GIB = (4, 8, 16, 64, 128)
# Exact swap boundaries catch accidental assumptions about the original 2 GiB.
SWAP_GIB_AND_POLICY = (
    (0, 30, 15),
    (2, 30, 15),
    (4, 25, 10),
    (8, 25, 10),
    (16, 20, 8),
    (32, 20, 8),
)


def process(pid, uid, name="bash", parent=1, mem=32, cwd="/work/project"):
    """Only metadata the sampler needs; no real PIDs or account records read."""
    return {
        "pid": pid,
        "starttime": pid * 10,
        "identity": (pid, pid * 10),
        "ppid": parent,
        "uid": uid,
        "name": name,
        "cmd": name,
        "argv": [name],
        "mem": mem,
        "cg": "",
        "cwd": cwd,
        "age_s": 12,
        "memory_kind": "PSS",
    }


class LinuxConfigurationTests(unittest.TestCase):
    def test_kernel_ram_and_swap_matrix_overrides_no_local_defaults(self):
        for ram_gib in RAM_GIB:
            for swap_gib, amber, red in SWAP_GIB_AND_POLICY:
                with self.subTest(ram_gib=ram_gib, swap_gib=swap_gib):
                    total = ram_gib * 1024
                    available = total * 3 // 4
                    swap = swap_gib * 1024
                    raw = (
                        f"MemTotal: {total * 1024} kB\n"
                        # MemFree deliberately differs: reclaimable memory counts.
                        f"MemFree: {total * 1024 // 8} kB\n"
                        f"MemAvailable: {available * 1024} kB\n"
                        f"SwapTotal: {swap * 1024} kB\n"
                        f"SwapFree: {swap * 1024 // 2} kB\n"
                    )
                    with patch.object(b, "read_text", return_value=raw):
                        mem = b.read_meminfo()
                    self.assertEqual(
                        mem,
                        {
                            "total": total,
                            "available": available,
                            "swap_total": swap,
                            "swap_free": swap // 2,
                        },
                    )
                    policy = b.derive_policy(mem["total"], mem["swap_total"])
                    self.assertEqual(
                        (policy["amberPct"], policy["redPct"]), (amber, red)
                    )
                    self.assertEqual((policy["amberPSI"], policy["redPSI"]), (10, 100))

    def test_missing_or_malformed_kernel_inputs_remain_unknown(self):
        core = b.CoreClient()
        try:
            # Invalid inputs must be rejected before starting any core subprocess.
            with patch.object(
                core, "_start", side_effect=AssertionError("Invalid input reached core")
            ):
                for raw in ("", "MemTotal: unavailable kB\n", "MemTotal: 8388608 kB\n"):
                    with (
                        self.subTest(raw=raw),
                        patch.object(b, "read_text", return_value=raw),
                    ):
                        mem = b.read_meminfo()
                        answer = core.ask(
                            mem["total"],
                            mem["available"],
                            0,
                            512,
                            b.derive_policy(mem["total"], mem["swap_total"]),
                        )
                        self.assertEqual(answer["level"], "UNKNOWN")
                        self.assertIsNone(answer["room"])
                        self.assertFalse(answer["proved"])
                for ram_gib in RAM_GIB:
                    total = ram_gib * 1024
                    answer = core.ask(
                        total, total // 2, None, 512, b.derive_policy(total, 0)
                    )
                    self.assertEqual(answer["level"], "UNKNOWN")
                    self.assertFalse(answer["valid"])
                    self.assertIsNone(answer["room"])
        finally:
            core.close()

    def test_psi_absent_is_distinct_from_observed_zero(self):
        for raw, expected in (
            ("", None),
            ("full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n", None),
            ("some avg10=unavailable\n", None),
            ("some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n", 0),
            ("some avg10=12.50 avg60=0.00 avg300=0.00 total=100\n", 125),
        ):
            with self.subTest(raw=raw), patch.object(b, "read_text", return_value=raw):
                self.assertEqual(b.read_pressure(), expected)

    def test_local_account_uid_is_not_assumed_to_be_1000(self):
        for uid in (1001, 20000, 65534):
            with self.subTest(uid=uid), patch.object(b.os, "getuid", return_value=uid):
                sampler = b.Sampler(core_bin="/nonexistent/swarm-test-core")
            try:
                self.assertEqual(sampler.uid, uid)
                # Other users' visible memory remains in machine totals, but is
                # not mistaken for this account's generic desktop processes.
                snap = sampler.aggregate(
                    {10: process(10, uid, mem=32), 20: process(20, uid + 1, mem=64)},
                    {"total": 8192, "available": 6144, "swap_total": 0, "swap_free": 0},
                    0,
                    None,
                )
                owners = {o["kind"]: o for o in snap["owners"]}
                self.assertEqual(owners["Desktop"]["mem"], 32)
                self.assertEqual(owners["System"]["mem"], 64)
                self.assertEqual(len(snap["agent_jobs"]), 0)
                self.assertIsNone(snap["gpu"])
                self.assertTrue(snap["validity"]["memory"])
            finally:
                sampler.close()

    def test_running_sampler_refreshes_ram_and_swap_policy_each_tick(self):
        sampler = b.Sampler(core_bin="/nonexistent/swarm-test-core")
        cases = ((8192, 0, 30, 15), (131072, 32768, 20, 8), (4096, 2048, 30, 15))
        try:
            # Simulate swap configuration changes/hot-plugged RAM on one live
            # sampler, with no driver thread or real /proc process enumeration.
            with (
                patch.object(
                    sampler,
                    "_poll_enrichment",
                    return_value=(None, None, "unavailable"),
                ),
                patch.object(b, "read_pressure", return_value=0),
                patch.object(b, "read_processes", return_value={}),
            ):
                for total, swap, amber, red in cases:
                    mem = {
                        "total": total,
                        "available": total // 2,
                        "swap_total": swap,
                        "swap_free": swap,
                    }
                    with (
                        self.subTest(total=total, swap=swap),
                        patch.object(b, "read_meminfo", return_value=mem),
                    ):
                        snap = sampler.snapshot()
                        self.assertEqual(snap["mem"], mem)
                        self.assertEqual(snap["policy"]["amberPct"], amber)
                        self.assertEqual(snap["policy"]["redPct"], red)
                        self.assertEqual(
                            snap["core"]["amberLine"], (total * amber + 99) // 100
                        )
                        self.assertEqual(
                            snap["core"]["redLine"], (total * red + 99) // 100
                        )
                        self.assertEqual(snap["gpu_status"], "unavailable")
                        self.assertFalse(snap["proved"])
                self.assertEqual(sampler._sample_count, len(cases))
        finally:
            sampler.close()

    def test_session_names_follow_current_home_and_working_folder(self):
        home = "/home/another user"
        with patch.object(b, "HOME", home):
            for cwd, expected in (
                (home, "Codex · ~"),
                (home + "/Projects/Example Work", "Codex · Example Work"),
            ):
                with self.subTest(cwd=cwd):
                    self.assertEqual(
                        b.owner_label(
                            "Codex", 10, {10: process(10, 20000, "codex", cwd=cwd)}, {}
                        ),
                        expected,
                    )


class LeanHardwareMatrixTests(unittest.TestCase):
    def test_actual_lean_lines_and_capacity_follow_each_machine(self):
        core = b.CoreClient()
        if not Path(core.core_bin).is_file() or not os.access(core.core_bin, os.X_OK):
            self.skipTest(
                "Built Lean core unavailable; run lake build in core/ or set SWARM_METER_CORE"
            )
        try:
            for ram_gib in RAM_GIB:
                total = ram_gib * 1024
                for swap_gib, amber_pct, red_pct in SWAP_GIB_AND_POLICY:
                    policy = b.derive_policy(total, swap_gib * 1024)
                    red_line = (total * red_pct + 99) // 100
                    amber_line = (total * amber_pct + 99) // 100
                    cases = (
                        (amber_line, 0, "GREEN"),
                        (amber_line - 1, 0, "AMBER"),
                        (red_line, 0, "AMBER"),
                        (red_line - 1, 0, "RED"),
                        (total, 10, "AMBER"),
                        (total, 100, "RED"),
                    )
                    for available, pressure, expected in cases:
                        with self.subTest(
                            ram_gib=ram_gib,
                            swap_gib=swap_gib,
                            available=available,
                            pressure=pressure,
                        ):
                            answer = core.ask(total, available, pressure, 512, policy)
                            # A fallback match cannot pass this integration gate.
                            self.assertTrue(answer["proved"], answer["reason"])
                            self.assertEqual(answer["level"], expected)
                            self.assertEqual(answer["redLine"], red_line)
                            self.assertEqual(answer["amberLine"], amber_line)
                            self.assertEqual(
                                answer["room"], max(0, available - red_line) // 512
                            )
                            # The returned count fits, and one more crosses the
                            # memory budget. This verifies the displayed unit.
                            self.assertLessEqual(
                                answer["room"] * 512, max(0, available - red_line)
                            )
                            self.assertGreater(
                                (answer["room"] + 1) * 512, max(0, available - red_line)
                            )
        finally:
            core.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
