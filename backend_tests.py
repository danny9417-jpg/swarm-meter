#!/usr/bin/env python3
"""Behavioral regressions for the reused sampler; no GUI and no job signals.

Run: python3 backend_tests.py. Synthetic process trees make every attribution
case deterministic; the final parity case separately requires the built Lean
binary. Failure to find that binary is a failure, not a silent test skip.
"""

import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import backend as b


class RecordingCore:
    """A clearly unproved test double; captures the sizes sent for capacities."""

    version = "test-double"
    proved = False

    def __init__(self):
        self.calls = []

    def ask(self, total, avail, pressure, jobsize, policy):
        self.calls.append(jobsize)
        return {
            **b.CoreClient.replica(total, avail, pressure, jobsize, policy),
            "valid": True,
            "proved": False,
            "version": self.version,
            "reason": "Test double",
        }

    def close(self):
        pass


def process(
    pid, name, parent=1, mem=100, cwd="/work/project", cmd=None, cg="", start=None
):
    started = pid * 10 if start is None else start
    return {
        "pid": pid,
        "name": name,
        "ppid": parent,
        "mem": mem,
        "uid": os.getuid(),
        "cmd": cmd or (name + " worker.py" if name in ("python", "python3") else name),
        "cg": cg,
        "cwd": cwd,
        "starttime": started,
        "identity": (pid, started),
        "age_s": 30.0,
        "memory_kind": "PSS",
    }


def memory():
    return {"total": 63727, "available": 46089, "swap_total": 2047, "swap_free": 2000}


class AttributionTests(unittest.TestCase):
    def setUp(self):
        self.sampler = b.Sampler()
        self.sampler.core = RecordingCore()

    def tearDown(self):
        self.sampler.close()

    def test_every_host_kind(self):
        cases = [
            ("Claude Code", "claude", None, ""),
            ("Codex", "codex", None, ""),
            ("Gemini CLI", "gemini", None, ""),
            ("OpenClaw", "node", "node node_modules/openclaw gateway", ""),
            ("Ollama", "ollama", None, ""),
            ("Docker", "python", "uvicorn server:app", "0::/docker-aabbccddeeff.scope"),
        ]
        for expected, name, cmd, cg in cases:
            with self.subTest(kind=expected):
                tree = {
                    10: process(10, name, cmd=cmd, cg=cg),
                    20: process(20, "bash", 10),
                    30: process(30, "python", 20, 1024),
                }
                answer = b.resolve_owner(30, tree, {})
                self.assertEqual(answer[0], expected)
                self.assertEqual(answer[2], [30, 20, 10])
                snapshot = self.sampler.aggregate(tree, memory(), 0, None)
                owner = next(o for o in snapshot["owners"] if o["kind"] == expected)
                self.assertEqual(owner["count"], 1)
                self.assertEqual(owner["process_count"], 3)
                self.assertEqual(owner["mem"], 1224)

    def test_descendant_first_cache_keeps_each_chain(self):
        tree = {
            30: process(30, "python", 20),
            20: process(20, "bash", 10),
            10: process(10, "claude"),
        }
        cache = {}
        self.assertEqual(b.resolve_owner(30, tree, cache)[2], [30, 20, 10])
        self.assertEqual(b.resolve_owner(20, tree, cache)[2], [20, 10])
        self.assertEqual(b.resolve_owner(10, tree, cache)[2], [10])

    def test_two_sessions_same_folder_stay_separate(self):
        tree = {
            10: process(10, "claude"),
            11: process(11, "python", 10),
            20: process(20, "claude"),
            21: process(21, "python", 20),
        }
        owners = self.sampler.aggregate(tree, memory(), 0, None)["owners"]
        self.assertEqual(len(owners), 2)
        self.assertNotEqual(owners[0]["label"], owners[1]["label"])
        self.assertEqual({o["host_pid"] for o in owners}, {10, 20})
        self.assertTrue(
            all(o["cwd"] == "/work/project" and o["age_s"] == 30 for o in owners)
        )

    def test_orphan_rule_distinguishes_observed_from_possible(self):
        tree = {
            10: process(10, "claude"),
            20: process(20, "python", 10, cmd="venv/bin/python run.py"),
        }
        self.sampler.aggregate(tree, memory(), 0, None)
        detached = copy.deepcopy(tree[20])
        detached["ppid"] = 1
        first = self.sampler.aggregate({20: detached}, memory(), 0, None)
        self.assertEqual(
            first["agent_jobs"][0]["orphan_evidence"]["confidence"], "observed"
        )
        # Reuse of the numeric PID must not inherit the old host's identity.
        detached["starttime"] += 1
        second = self.sampler.aggregate({20: detached}, memory(), 0, None)
        self.assertEqual(
            second["agent_jobs"][0]["orphan_evidence"]["confidence"], "possible"
        )
        self.assertFalse(
            b.is_orphan(process(40, "python", cmd="python ordinary.py"), {})
        )

    def test_detaching_while_known_host_lives_is_not_orphaned(self):
        tree = {10: process(10, "claude"), 20: process(20, "python", 10)}
        self.sampler.aggregate(tree, memory(), 0, None)
        tree[20]["ppid"] = 1
        snapshot = self.sampler.aggregate(tree, memory(), 0, None)
        child = next(j for j in snapshot["agent_jobs"] if j["pid"] == 20)
        self.assertEqual(child["kind"], "Claude Code")
        self.assertIsNone(child["orphan_evidence"])

    def test_surviving_and_new_orphan_descendants_keep_agent_attribution(self):
        tree = {
            10: process(10, "claude"),
            20: process(20, "bash", 10),
            30: process(30, "python", 20, 1024),
        }
        self.sampler.aggregate(tree, memory(), 0, None)
        del tree[10]
        tree[20]["ppid"] = 1
        # A new worker appears under the surviving orphan shell as well.
        tree[40] = process(40, "python", 20, 512)
        snapshot = self.sampler.aggregate(tree, memory(), 0, None)
        self.assertEqual({j["pid"] for j in snapshot["agent_jobs"]}, {30, 40})
        self.assertEqual(snapshot["owners"][0]["helper_count"], 1)
        self.assertTrue(
            all(
                j["orphan_evidence"]["confidence"] == "observed"
                for j in snapshot["agent_jobs"]
            )
        )
        self.assertEqual({o["kind"] for o in snapshot["owners"]}, {"Orphans"})

    def test_possible_orphans_use_agent_cache_without_becoming_observed(self):
        tree = {
            20: process(20, "python", cmd="venv/bin/python worker.py"),
            30: process(30, "python", 20),
        }
        self.sampler.aggregate(tree, memory(), 0, None)
        self.assertEqual(self.sampler._agent_identities, {(20, 200), (30, 300)})
        self.assertEqual(self.sampler._previous_owners, {})
        second = self.sampler.aggregate(tree, memory(), 0, None)
        self.assertTrue(
            all(
                j["orphan_evidence"]["confidence"] == "possible"
                for j in second["agent_jobs"]
            )
        )

    def test_guard_and_vram_evidence_are_per_job(self):
        tree = {
            10: process(10, "claude"),
            20: process(20, "python", 10, 4096, cg="0::/sg0-decode-1.scope"),
        }
        gpu = {
            "total": 24000,
            "used": 8192,
            "per_pid": {20: 4096},
            "pid_starttimes": {20: 200},
            "process_data_valid": True,
        }
        snapshot = self.sampler.aggregate(tree, memory(), 0, gpu)
        jobs = {j["pid"]: j for j in snapshot["agent_jobs"]}
        self.assertTrue(jobs[20]["guarded"])
        self.assertEqual(jobs[20]["vram"], 4096)
        self.assertIsNone(
            next(p for p in snapshot["owners"][0]["processes"] if p["pid"] == 10)[
                "vram"
            ]
        )
        self.assertEqual(jobs[20]["identity"], (20, 200))
        self.assertEqual(snapshot["owners"][0]["vram"], 4096)

    def test_cached_gpu_memory_does_not_transfer_to_reused_pid(self):
        tree = {
            10: process(10, "claude"),
            20: process(20, "python", 10, 4096, start=201),
        }
        gpu = {
            "total": 24000,
            "used": 8192,
            "per_pid": {20: 4096},
            "pid_starttimes": {20: 200},
            "process_data_valid": True,
        }
        snapshot = self.sampler.aggregate(tree, memory(), 0, gpu)
        reused = next(j for j in snapshot["agent_jobs"] if j["pid"] == 20)
        self.assertIsNone(reused["vram"])
        self.assertEqual(snapshot["owners"][0]["vram"], 0)

    def test_mean_and_largest_scenarios_are_sent_to_core(self):
        tree = {
            10: process(10, "claude", mem=100),
            20: process(20, "python", 10, 300),
            30: process(30, "bash", 10, 20),
        }
        owner = self.sampler.aggregate(tree, memory(), 0, None)["owners"][0]
        self.assertEqual(owner["active_count"], 1)
        self.assertEqual(owner["mean_mb"], 300)  # Only identified workload RAM.
        self.assertEqual(owner["mem"], 420)
        self.assertEqual(owner["host_mb"], 100)
        self.assertEqual(owner["helper_mb"], 20)
        self.assertEqual(owner["capacity"]["jobSize"], 300)
        self.assertEqual(owner["planning_mb"], 300)
        self.assertEqual(owner["planning_capacity"]["jobSize"], 300)
        self.assertIn(300, self.sampler.core.calls)

    def test_cycle_and_missing_parent_do_not_loop(self):
        tree = {10: process(10, "bash", 20), 20: process(20, "python", 10)}
        self.assertIsNone(b.resolve_owner(20, tree, {}))
        self.assertIsNone(b.resolve_owner(80, tree, {}))

    def test_starttime_parser_handles_unusual_process_name(self):
        remainder = ["S"] + ["0"] * 18 + ["123456"] + ["0"] * 10
        self.assertEqual(
            b.process_starttime("77 (a name ) tricky) " + " ".join(remainder)), 123456
        )


class WorkloadRoleTests(unittest.TestCase):
    """A local job is recognized work, never an RSS threshold or CPU guess."""

    def setUp(self):
        self.sampler = b.Sampler()
        self.sampler.core = RecordingCore()

    def tearDown(self):
        self.sampler.close()

    def sample(self, tree):
        return self.sampler.aggregate(tree, memory(), 0, None)

    def codex_owner(self, snapshot):
        return next(o for o in snapshot["owners"] if o["kind"] == "Codex")

    def test_reported_sixteen_processes_are_one_host_and_fifteen_helpers(self):
        tree = {10: process(10, "codex", mem=1341)}
        for pid in range(20, 35):
            tree[pid] = process(
                pid, "python", 10, 59, cmd="python /opt/tools/decomp mcp"
            )
        first = self.sample(tree)
        owner = self.codex_owner(first)
        self.assertEqual(
            (owner["count"], owner["host_count"], owner["helper_count"]), (0, 1, 15)
        )
        self.assertEqual(owner["process_count"], 16)
        self.assertEqual(owner["mem"], 1341 + 15 * 59)
        self.assertEqual(owner["host_mb"] + owner["helper_mb"], owner["mem"])
        self.assertIsNone(owner["mean_mb"])
        self.assertIsNone(owner["capacity"])
        self.assertIsNone(first["job_size"])
        self.assertIsNone(first["core"]["room"])
        self.assertTrue(first["core"]["valid"])  # Lamp remains a real core answer.
        # Two workload processes contribute exactly their own 4/6 GiB RAM.
        tree[50] = process(50, "python", 20, 4096, cmd="python decode.py")
        tree[60] = process(60, "python", 20, 6144, cmd="python train.py")
        second = self.codex_owner(self.sample(tree))
        self.assertEqual((second["count"], second["mean_mb"]), (2, 5120))
        self.assertEqual(second["planning_mb"], 6144)
        tree[20]["mem"] = 8192  # A helper's growth must not change the work average.
        self.assertEqual(self.codex_owner(self.sample(tree))["mean_mb"], 5120)
        del tree[50]
        final = self.codex_owner(self.sample(tree))
        self.assertEqual((final["count"], final["mean_mb"]), (1, 6144))
        self.assertEqual(final["capacity"]["jobSize"], 6144)

    def test_small_sleeping_work_stays_counted_across_old_threshold(self):
        tree = {
            10: process(10, "codex", mem=9000),
            20: process(20, "python", 10, 1, cmd="python decode.py"),
            30: process(30, "node", 10, 8000, cmd="node"),
        }
        tree[20].update(state="S", cpu_ticks=0)
        for size in (1, 16, 47, 48, 49, 100):
            tree[20]["mem"] = size
            owner = self.codex_owner(self.sample(tree))
            self.assertEqual((owner["count"], owner["mean_mb"]), (1, size))
            self.assertEqual(owner["unclassified_count"], 1)
            self.assertEqual(owner["mem"], 17000 + size)
            self.assertEqual(sum(owner[r + "_mb"] for r in b.ROLES), owner["mem"])

    def test_mcp_descendants_and_explicit_child_work(self):
        tree = {
            10: process(10, "codex"),
            20: process(20, "python", 10, cmd="python /opt/tools/decomp mcp"),
            30: process(30, "node", 20, cmd="node worker.js"),
            40: process(40, "bash", 20),
            50: process(50, "python", 40, cmd="python worker.py"),
            60: process(60, "ffmpeg", 40),
            70: process(70, "python", 20, cmd="python /project/mcp/decode.py"),
        }
        owner = self.codex_owner(self.sample(tree))
        self.assertEqual({p["pid"] for p in owner["jobs"]}, {60, 70})
        self.assertEqual(owner["helper_count"], 4)
        self.assertTrue(all(p["role_reason"] for p in owner["processes"]))

    def test_modules_packages_inline_text_and_interpreter_options(self):
        cases = [
            ("python -m mcp.server", "helper"),
            ("python -m pytest tests", "workload"),
            ("python -m torch.distributed.run train.py", "helper"),
            (
                "node /opt/node_modules/@modelcontextprotocol/server-filesystem/dist/index.js",
                "helper",
            ),
            ("node /opt/node_modules/mcp-server-x/server.js", "helper"),
            ("python /project/mcp/decode.py", "workload"),
            ("python -c \"print('train.py codex')\"", "unclassified"),
            ("python -W ignore -X dev train.py", "workload"),
            ("node --require hook.js analyze.js", "workload"),
            ("python", "unclassified"),
            ("node /tmp/.tmpABC/kernel.js", "unclassified"),
            ("node /tmp/.tmpABC/trusted-worker.js", "unclassified"),
            ("unknown-daemon", "unclassified"),
        ]
        for command, expected in cases:
            with self.subTest(command=command):
                tree = {
                    10: process(10, "codex"),
                    20: process(20, "python", 10, cmd=command),
                }
                result = self.codex_owner(self.sample(tree))["processes"]
                self.assertEqual(
                    next(p["role"] for p in result if p["pid"] == 20), expected
                )

    def test_codex_cua_repl_package_is_helper_without_hiding_unknown_servers(self):
        tree = {
            10: process(10, "codex"),
            20: process(
                20,
                "MainThread",
                10,
                600,
                cmd="node /opt/node_modules/@oai/cua-repl/dist/cua-repl.mjs",
            ),
            30: process(30, "MainThread", 10, 400, cmd="node /opt/tools/server.mjs"),
            40: process(40, "node", 10, 100, cmd="node /project/cua-repl.mjs"),
            50: process(50, "python", 20, 4096, cmd="python train.py"),
        }
        owner = self.codex_owner(self.sample(tree))
        roles = {p["pid"]: p["role"] for p in owner["processes"]}
        self.assertEqual(
            roles,
            {
                10: "host",
                20: "helper",
                30: "unclassified",
                40: "workload",
                50: "workload",
            },
        )
        self.assertEqual(owner["helper_mb"], 600)
        self.assertEqual(owner["unclassified_count"], 1)

    def test_actual_argument_boundaries_override_display_string(self):
        worker = process(20, "python", 10, cmd="python train.py")
        worker["argv"] = ["python", "-c", "print('train.py codex')"]
        snapshot = self.sample({10: process(10, "codex"), 20: worker})
        self.assertEqual(self.codex_owner(snapshot)["count"], 0)
        worker["argv"] = ["python", "train.py"]
        snapshot = self.sample({10: process(10, "codex"), 20: worker})
        self.assertEqual(self.codex_owner(snapshot)["count"], 1)
        self.assertEqual(snapshot["agent_jobs"][0]["identity"], (20, 200))

    def test_agent_paths_in_arguments_or_project_names_do_not_create_hosts(self):
        commands = [
            "bash -lc 'echo /opt/resources/codex'",
            "rg /opt/resources/codex file.txt",
            "tail /opt/resources/codex",
            "python /project/openclaw/decode.py",
            "python /project/claude-code/train.py",
            "node /project/gemini-cli/analyze.js",
        ]
        for command in commands:
            with self.subTest(command=command):
                child = process(20, "python", 10, cmd=command)
                self.assertIsNone(b._match_host(child))
                self.assertEqual(
                    b.resolve_owner(20, {10: process(10, "codex"), 20: child}, {})[:2],
                    ("Codex", 10),
                )
        host = process(10, "codex", cmd="codex --prompt 'python train.py openclaw'")
        self.assertEqual(self.codex_owner(self.sample({10: host}))["host_count"], 1)

    def test_npm_agent_entrypoints_are_hosts(self):
        for command, kind in [
            ("node /opt/node_modules/@openai/codex/bin/codex.js", "Codex"),
            ("node /opt/node_modules/@anthropic-ai/claude-code/cli.js", "Claude Code"),
            ("node /opt/node_modules/@google/gemini-cli/dist/index.js", "Gemini CLI"),
            ("node /opt/node_modules/openclaw/openclaw.mjs", "OpenClaw"),
        ]:
            with self.subTest(kind=kind):
                host = process(10, "node", cmd=command)
                self.assertEqual(b._match_host(host), kind)
                owner = self.sample({10: host})["owners"][0]
                self.assertEqual((owner["host_count"], owner["count"]), (1, 0))

    def test_launchers_and_compiler_driver_do_not_duplicate_their_work(self):
        tree = {
            10: process(10, "codex"),
            20: process(20, "uv", 10, cmd="uv run python train.py"),
            30: process(30, "python", 20, cmd="python train.py"),
            40: process(40, "gcc", 10, cmd="gcc program.c"),
            50: process(50, "cc1", 40, cmd="cc1 program.c"),
        }
        owner = self.codex_owner(self.sample(tree))
        self.assertEqual({p["pid"] for p in owner["jobs"]}, {30, 50})
        self.assertEqual(owner["helper_count"], 2)

    def test_monitor_is_support_and_exited_work_is_not_live(self):
        tree = {
            10: process(10, "codex"),
            20: process(20, "python", 10, cmd="python /opt/swarm-meter/swarm_meter.py"),
            30: process(
                30, "core", 20, cmd="/opt/swarm-meter/core/.lake/build/bin/core"
            ),
            40: process(40, "python", 10, cmd="python decode.py"),
        }
        tree[40]["state"] = "Z"
        owner = self.codex_owner(self.sample(tree))
        self.assertEqual(
            (owner["count"], owner["helper_count"], owner["unclassified_count"]),
            (0, 2, 1),
        )

    def test_model_server_and_runner_are_distinguished_without_activity_claim(self):
        snapshot = self.sample(
            {
                10: process(10, "ollama", cmd="ollama serve"),
                20: process(20, "ollama", 10, cmd="ollama runner --model model.gguf"),
                30: process(30, "llama-server", 10),
            }
        )
        self.assertEqual(snapshot["host_count"], 1)
        self.assertEqual(snapshot["workload_count"], 2)
        self.assertTrue(
            all(
                "does not prove an active request" in p["role_reason"]
                for p in snapshot["agent_jobs"]
            )
        )

    def test_unknown_and_zero_memory_do_not_fabricate_capacity(self):
        for size in (None, 0):
            with self.subTest(size=size):
                snapshot = self.sample(
                    {10: process(10, "codex"), 20: process(20, "python", 10, mem=size)}
                )
                owner = self.codex_owner(snapshot)
                self.assertEqual(owner["count"], 1)
                self.assertEqual(owner["mean_mb"], size)
                self.assertEqual(owner["memory_complete"], size is not None)
                self.assertIsNone(owner["capacity"])
                self.assertIsNone(snapshot["job_size"])
                self.assertIsNone(snapshot["core"]["jobSize"])
                self.assertIsNone(snapshot["core"]["room"])

    def test_history_persists_count_semantics_and_role_totals(self):
        snapshot = self.sample(
            {10: process(10, "codex"), 20: process(20, "python", 10, 16)}
        )
        with tempfile.TemporaryDirectory(prefix="swarm-role-history-") as folder:
            history = b.History(Path(folder) / "history.db")
            try:
                self.assertTrue(history.add(snapshot))
                jobs, encoded = history.db.execute(
                    "SELECT jobs, owners FROM samples"
                ).fetchone()
                saved = json.loads(encoded)[0]
                self.assertEqual(jobs, 1)
                self.assertEqual(
                    saved["count_basis"], "identified_workload_processes_v1"
                )
                self.assertEqual(saved["workload_mb"], 16)
                self.assertEqual(saved["host_count"], 1)
                self.assertEqual(saved["mean_mb"], 16)
            finally:
                history.close()

    def test_metadata_role_cache_observes_exec_and_never_caches_ram_or_ancestry(self):
        state = {
            "clock": 100.0,
            "cmd": "python\0train.py\0",
            "rss": 1024,
            "start": 123,
            "parent": 1,
        }
        metadata, memory_cache = {}, {}

        def fake_read(path):
            if path == "/proc/uptime":
                return "1000 0"
            if path.endswith("/stat"):
                return "10 (python) " + " ".join(
                    ["S"] + ["0"] * 18 + [str(state["start"])] + ["0"] * 10
                )
            if path.endswith("/status"):
                return f"Name: python\nPPid: {state['parent']}\nUid: {os.getuid()}\nVmRSS: {state['rss']} kB\n"
            if path.endswith("/cmdline"):
                return state["cmd"]
            return ""

        with (
            patch.object(b, "read_text", side_effect=fake_read),
            patch.object(b.os, "listdir", return_value=["10"]),
            patch.object(b.os, "readlink", return_value="/work"),
            patch.object(b.time, "monotonic", side_effect=lambda: state["clock"]),
        ):

            def sampled():
                rows = b.read_processes(
                    os.getuid(), memory_cache, metadata_cache=metadata, pss_budget_s=0
                )
                snapshot = self.sample(rows)
                return rows[10], next(
                    p for o in snapshot["owners"] for p in o["processes"]
                )

            first, item = sampled()
            self.assertEqual(item["role"], "workload")
            state.update(clock=102, cmd="python\0-m\0mcp.server\0", rss=2048)
            second, item = sampled()
            self.assertEqual(
                item["role"], "workload"
            )  # Existing 10-second metadata window.
            self.assertEqual(second["metadata_age_s"], 2)
            self.assertEqual(
                second["mem"], 2
            )  # RAM is fresh despite cached command facts.
            state["clock"] = 111
            third, item = sampled()
            self.assertEqual(item["role"], "helper")
            state["clock"] = 112
            fourth, item = sampled()
            self.assertEqual(item["role"], "helper")
            self.assertEqual(fourth["metadata_age_s"], 1)
            state.update(clock=113, start=124, parent=99, cmd="python\0train.py\0")
            final, item = sampled()
            self.assertEqual(item["role"], "workload")
            self.assertEqual(final["identity"], (10, 124))
            self.assertEqual(final["ppid"], 99)
            self.assertEqual(set(metadata), {(10, 124)})

    def test_sampler_preserves_missing_and_rounds_tiny_positive_memory(self):
        stat = "10 (python) " + " ".join(["S"] + ["0"] * 18 + ["123"] + ["0"] * 10)
        for rss_line, rollup, expected in [
            ("VmRSS: 900 kB\n", "Rss: 900 kB\nPss: 768 kB\n", 1),
            ("", "", None),
        ]:
            with self.subTest(expected=expected):

                def fake_read(path):
                    return {
                        "/proc/uptime": "1000 0",
                        "/proc/10/stat": stat,
                        "/proc/10/status": f"Name: python\nPPid: 1\nUid: {os.getuid()}\n"
                        + rss_line,
                        "/proc/10/cmdline": "python\0train.py\0",
                        "/proc/10/smaps_rollup": rollup,
                    }.get(path, "")

                with (
                    patch.object(b, "read_text", side_effect=fake_read),
                    patch.object(b.os, "listdir", return_value=["10"]),
                    patch.object(b.os, "readlink", return_value="/work"),
                ):
                    item = b.read_processes(os.getuid())[10]
                self.assertEqual(item["mem"], expected)
                self.assertEqual(item["argv"], ["python", "train.py"])
                self.assertEqual(item["state"], "S")


class MemoryCacheTests(unittest.TestCase):
    def test_cache_keeps_identity_and_refreshes_on_growth_or_age(self):
        state = {"rss": 100, "pss": 80, "start": 123, "clock": 100.0, "reads": 0}

        def fake_read(path):
            if path == "/proc/uptime":
                return "1000 0"
            if path.endswith("/stat"):
                rest = ["S"] + ["0"] * 18 + [str(state["start"])] + ["0"] * 10
                return "10 (python) " + " ".join(rest)
            if path.endswith("/status"):
                return f"Name:\tpython\nPPid:\t1\nUid:\t{os.getuid()}\nVmRSS:\t{state['rss'] * 1024} kB\n"
            if path.endswith("/smaps_rollup"):
                state["reads"] += 1
                return f"Rss: {state['rss'] * 1024} kB\nPss: {state['pss'] * 1024} kB\n"
            return ""

        cache = {}
        with (
            patch.object(b, "read_text", side_effect=fake_read),
            patch.object(b.os, "listdir", return_value=["10"]),
            patch.object(b.os, "readlink", return_value="/work"),
            patch.object(b.time, "monotonic", side_effect=lambda: state["clock"]),
        ):
            self.assertEqual(b.read_processes(os.getuid(), cache)[10]["mem"], 80)
            state["clock"] += 2
            self.assertEqual(
                b.read_processes(os.getuid(), cache)[10]["memory_age_s"], 2
            )
            self.assertEqual(state["reads"], 1)
            state.update(rss=200, pss=180)
            self.assertEqual(b.read_processes(os.getuid(), cache)[10]["mem"], 180)
            self.assertEqual(state["reads"], 2)
            state["clock"] += 16
            b.read_processes(os.getuid(), cache)
            self.assertEqual(state["reads"], 3)
            state["start"] += 1
            b.read_processes(os.getuid(), cache)
            self.assertEqual(state["reads"], 4)
            self.assertEqual(len(cache), 1)

    def test_desktop_growth_is_visible_shrink_conservative_then_refreshes(self):
        state = {"rss": 100, "pss": 80, "clock": 100.0, "reads": 0}

        def fake_read(path):
            if path == "/proc/uptime":
                return "1000 0"
            if path.endswith("/stat"):
                return "10 (python) " + " ".join(
                    ["S"] + ["0"] * 18 + ["123"] + ["0"] * 10
                )
            if path.endswith("/status"):
                return f"Name:\tpython\nPPid:\t1\nUid:\t{os.getuid()}\nVmRSS:\t{state['rss'] * 1024} kB\n"
            if path.endswith("/smaps_rollup"):
                state["reads"] += 1
                return f"Rss: {state['rss'] * 1024} kB\nPss: {state['pss'] * 1024} kB\n"
            return ""

        cache = {}
        with (
            patch.object(b, "read_text", side_effect=fake_read),
            patch.object(b.os, "listdir", return_value=["10"]),
            patch.object(b.os, "readlink", return_value="/work"),
            patch.object(b.time, "monotonic", side_effect=lambda: state["clock"]),
        ):
            self.assertEqual(b.read_processes(os.getuid(), cache, set())[10]["mem"], 80)
            state.update(rss=120, pss=90, clock=102)
            record = b.read_processes(os.getuid(), cache, set())[10]
            self.assertEqual(record["mem"], 100)
            self.assertIn("estimate", record["memory_kind"])
            self.assertEqual(state["reads"], 1)
            state.update(rss=90, pss=60)
            self.assertEqual(b.read_processes(os.getuid(), cache, set())[10]["mem"], 80)
            # Attribution reduces the maximum age to four seconds. Shrinkage
            # remains conservative until a fresh PSS measurement is due.
            self.assertEqual(
                b.read_processes(os.getuid(), cache, {(10, 123)})[10]["mem"], 80
            )
            state["clock"] = 104
            self.assertEqual(
                b.read_processes(os.getuid(), cache, {(10, 123)})[10]["mem"], 60
            )
            self.assertEqual(state["reads"], 2)
            state.update(rss=200, pss=180, clock=104.1)
            self.assertEqual(
                b.read_processes(os.getuid(), cache, {(10, 123)})[10]["mem"], 180
            )
            self.assertEqual(state["reads"], 3)  # >=64 MiB growth bypasses cache.
            state.update(clock=135, pss=175)
            self.assertEqual(
                b.read_processes(os.getuid(), cache, set())[10]["mem"], 175
            )
            self.assertEqual(state["reads"], 4)

    def test_exhausted_pss_budget_keeps_rss_growth_visible(self):
        state = {"clock": 100.0, "cpu": 0.0, "reads": 0, "rss": {10: 100, 20: 300}}

        def fake_read(path):
            if path == "/proc/uptime":
                return "1000 0"
            pid = int(path.split("/")[2])
            if path.endswith("/stat"):
                return f"{pid} (python) " + " ".join(
                    ["S"] + ["0"] * 18 + [str(pid * 10)] + ["0"] * 10
                )
            if path.endswith("/status"):
                return f"Name:\tpython\nPPid:\t1\nUid:\t{os.getuid()}\nVmRSS:\t{state['rss'][pid] * 1024} kB\n"
            if path.endswith("/smaps_rollup"):
                state["reads"] += 1
                state["cpu"] += 0.02  # One slow read consumes the whole budget.
                return f"Rss: {state['rss'][pid] * 1024} kB\nPss: {(state['rss'][pid] - 20) * 1024} kB\n"
            return ""

        cache = {}
        with (
            patch.object(b, "read_text", side_effect=fake_read),
            patch.object(b.os, "listdir", return_value=["10", "20"]),
            patch.object(b.os, "readlink", return_value="/work"),
            patch.object(b.time, "monotonic", side_effect=lambda: state["clock"]),
            patch.object(b.time, "process_time", side_effect=lambda: state["cpu"]),
        ):
            first = b.read_processes(os.getuid(), cache, pss_budget_s=0.008)
            self.assertEqual(state["reads"], 1)
            self.assertEqual(first[10]["mem"], 80)
            self.assertEqual(first[20]["mem"], 300)
            self.assertEqual(first[20]["memory_kind"], "RSS upper bound")
            state["rss"][10] = 200
            state["clock"] += 2
            second = b.read_processes(os.getuid(), cache, pss_budget_s=0)
            self.assertEqual(state["reads"], 1)
            self.assertEqual(second[10]["mem"], 180)
            self.assertIn("estimate", second[10]["memory_kind"])
            self.assertEqual(second[10]["memory_age_s"], 2)
            # A later budgeted tick prioritizes the not-yet-measured process.
            third = b.read_processes(os.getuid(), cache, pss_budget_s=0.008)
            self.assertEqual(third[20]["mem"], 280)
            self.assertEqual(state["reads"], 2)

    def test_rollup_rss_is_paired_with_pss_so_regrowth_is_not_hidden(self):
        state = {"fast_rss": 1000, "rollup_rss": 100, "pss": 100, "clock": 100.0}

        def fake_read(path):
            if path == "/proc/uptime":
                return "1000 0"
            if path.endswith("/stat"):
                return "10 (python) " + " ".join(
                    ["S"] + ["0"] * 18 + ["123"] + ["0"] * 10
                )
            if path.endswith("/status"):
                return f"Name:\tpython\nPPid:\t1\nUid:\t{os.getuid()}\nVmRSS:\t{state['fast_rss'] * 1024} kB\n"
            if path.endswith("/smaps_rollup"):
                return f"Rss: {state['rollup_rss'] * 1024} kB\nPss: {state['pss'] * 1024} kB\n"
            return ""

        cache = {}
        with (
            patch.object(b, "read_text", side_effect=fake_read),
            patch.object(b.os, "listdir", return_value=["10"]),
            patch.object(b.os, "readlink", return_value="/work"),
            patch.object(b.time, "monotonic", side_effect=lambda: state["clock"]),
        ):
            self.assertEqual(b.read_processes(os.getuid(), cache)[10]["mem"], 100)
            self.assertEqual(cache[(10, 123)]["rss"], 100)
            state.update(fast_rss=900, clock=102)
            next_sample = b.read_processes(os.getuid(), cache, pss_budget_s=0)
            self.assertEqual(next_sample[10]["mem"], 900)
            self.assertIn("estimate", next_sample[10]["memory_kind"])


class EnrichmentTests(unittest.TestCase):
    def test_gpu_work_does_not_block_first_ram_sample_and_stale_is_visible(self):
        sampler = b.Sampler()
        sampler.core = RecordingCore()
        release = threading.Event()
        gpu = {
            "total": 24000,
            "used": 1000,
            "name": "Test GPU",
            "per_pid": {},
            "process_data_valid": True,
        }

        def delayed_gpu():
            release.wait(1)
            time.sleep(0.02)
            return gpu

        try:
            with (
                patch.object(b, "gpu_snapshot", side_effect=delayed_gpu),
                patch.object(b, "docker_name_map", return_value={}),
                patch.object(b, "read_meminfo", return_value=memory()),
                patch.object(b, "read_pressure", return_value=0),
                patch.object(b, "read_processes", return_value={}),
            ):
                began = time.monotonic()
                snap = sampler.snapshot()
                self.assertLess(time.monotonic() - began, 0.2)
                self.assertEqual(snap["gpu_status"], "pending")
                self.assertIsNone(snap["gpu"])
                release.set()
                sampler._enrichment_thread.join(timeout=1)
                snap = sampler.snapshot()
                self.assertEqual(snap["gpu_status"], "current")
                self.assertEqual(snap["gpu"]["used"], 1000)
                self.assertGreaterEqual(
                    snap["gpu_age_s"], 0.02
                )  # Query latency counts toward age.
                sampler._enrichment_result = (gpu, time.monotonic() - 11, None)
                snap = sampler.snapshot()
                self.assertEqual(snap["gpu_status"], "stale")
                self.assertIsNone(snap["gpu"])
        finally:
            release.set()
            sampler.close()
        self.assertFalse(sampler._enrichment_thread.is_alive())


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.policy = b.derive_policy(63727, 2047)

    def test_missing_binary_is_explicit_fallback(self):
        core = b.CoreClient(core_bin="/nonexistent/swarm-meter-test-core")
        try:
            answer = core.ask(63727, 46089, 0, 1024, self.policy)
            self.assertFalse(answer["proved"])
            self.assertTrue(answer["valid"])
            self.assertEqual(answer["room"], 35)
            self.assertIn("unavailable", answer["reason"])
        finally:
            core.close()

    def test_missing_required_data_never_says_green(self):
        core = b.CoreClient(core_bin="/nonexistent/swarm-meter-test-core")
        try:
            for args in (
                (63727, 46089, None),
                (0, 0, 0),
                (63727, -1, 0),
                (63727, 70000, 0),
            ):
                with self.subTest(args=args):
                    answer = core.ask(*args, 1024, self.policy)
                    self.assertEqual(answer["level"], "UNKNOWN")
                    self.assertIsNone(answer["room"])
                    self.assertFalse(answer["valid"])
        finally:
            core.close()

    def test_pressure_read_failure_is_none(self):
        with patch.object(b, "read_text", return_value=""):
            self.assertIsNone(b.read_pressure())

    def test_bad_protocol_response_is_rejected(self):
        for line in (
            "error=usage",
            "level=GREEN rank=0",
            "level=RED rank=0 redLine=1 amberLine=2 room=3 jobSize=1",
        ):
            with self.subTest(line=line):
                with self.assertRaises(ValueError):
                    b.CoreClient._parse(line, 1)

    def test_hung_core_request_is_bounded_and_falls_back(self):
        with tempfile.TemporaryDirectory(prefix="swarm-backend-core-") as folder:
            path = Path(folder) / "core"
            path.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(2)\n")
            path.chmod(0o700)
            core = b.CoreClient(core_bin=path, timeout=0.02)
            start = time.monotonic()
            try:
                answer = core.ask(63727, 46089, 0, 1024, self.policy)
                self.assertFalse(answer["proved"])
                self.assertIn("deadline", answer["reason"])
                self.assertLess(time.monotonic() - start, 0.8)
            finally:
                core.close()

    def test_actual_built_lean_boundaries_and_replica_parity(self):
        core = b.CoreClient()
        try:
            for avail, pressure, expected in (
                (46089, 0, "GREEN"),
                (19119, 0, "GREEN"),
                (19118, 0, "AMBER"),
                (9560, 0, "AMBER"),
                (9559, 0, "RED"),
                (46089, 10, "AMBER"),
                (46089, 100, "RED"),
            ):
                with self.subTest(available=avail, pressure=pressure):
                    answer = core.ask(63727, avail, pressure, 1024, self.policy)
                    self.assertTrue(answer["proved"], answer["reason"])
                    self.assertEqual(answer["level"], expected)
                    replica = b.CoreClient.replica(
                        63727, avail, pressure, 1024, self.policy
                    )
                    self.assertEqual({key: answer[key] for key in replica}, replica)
            self.assertEqual(core.ask(63727, 46089, 0, 0, self.policy)["jobSize"], 1)
        finally:
            core.close()


class TrendHistoryDemoTests(unittest.TestCase):
    def snapshot(self, ts, available=20000, jobs=2):
        policy = b.derive_policy(63727, 2047)
        return {
            "ts": ts,
            "mem": {"total": 63727, "available": available},
            "pressure": 0,
            "gpu": None,
            "agent_mb": 10000,
            "agent_jobs": [{}] * jobs,
            "owners": [],
            "core": {
                **b.CoreClient.replica(63727, available, 0, 1024, policy),
                "valid": True,
            },
        }

    def test_learning_state_and_actual_ramp_span(self):
        tracker = b.TrendTracker()
        self.assertTrue(tracker.add(self.snapshot(100))["learning"])
        summary = tracker.add(self.snapshot(102, jobs=8))
        self.assertTrue(summary["learning"])
        self.assertEqual(summary["ramp_span_s"], 2)
        self.assertEqual(summary["ramp_text"], "Net +6 jobs over 2 s observed")

    def test_forecast_is_estimate_and_uses_timestamps(self):
        tracker = b.TrendTracker()
        for k in range(16):
            summary = tracker.add(self.snapshot(100 + 2 * k, 20000 - 100 * k))
        self.assertFalse(summary["learning"])
        self.assertAlmostEqual(summary["rate_mib_s"], -50)
        self.assertAlmostEqual(summary["eta_s"], (18500 - 9560) / 50)
        self.assertTrue(summary["estimated"])

    def test_scope_stays_300_seconds_across_interval_change(self):
        tracker = b.TrendTracker()
        for ts in list(range(0, 201)) + list(range(204, 605, 4)):
            tracker.add(self.snapshot(ts))
        self.assertLessEqual(tracker.series[-1][0] - tracker.series[0][0], 300)
        self.assertGreater(tracker.series[-1][0] - tracker.series[0][0], 295)

    def test_unknown_data_returns_learning_instead_of_steady(self):
        tracker = b.TrendTracker()
        for ts in (0, 10, 20):
            snap = self.snapshot(ts)
            snap["core"]["valid"] = False
            summary = tracker.add(snap)
        self.assertTrue(summary["learning"])
        self.assertIsNone(summary["rate_mib_s"])

    def test_history_writes_missing_gpu_as_null_and_prunes(self):
        with tempfile.TemporaryDirectory(prefix="swarm-backend-history-") as folder:
            path = Path(folder) / "history.db"
            history = b.History(path)
            now = time.time()
            self.assertTrue(history.add(self.snapshot(now)))
            history.db.execute("INSERT INTO samples(ts) VALUES (?)", (now - 90000,))
            history.db.commit()
            history.close()
            history = b.History(path)
            self.assertEqual(
                history.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 1
            )
            self.assertIsNone(
                history.db.execute("SELECT vram_used FROM samples").fetchone()[0]
            )
            self.assertTrue(history.healthy)
            history.close()

    def test_history_failure_is_visible(self):
        with tempfile.TemporaryDirectory(prefix="swarm-backend-history-") as folder:
            history = b.History(folder)  # A directory cannot be a SQLite file.
            self.assertFalse(history.healthy)
            self.assertIn("unavailable", history.status)
            self.assertFalse(history.add(self.snapshot(time.time())))
            history.close()

    def test_demo_uses_supplied_core_and_marks_every_job(self):
        core = RecordingCore()
        snap = b.make_demo_snapshot(core)
        self.assertTrue(snap["demo"])
        self.assertFalse(snap["proved"])
        self.assertEqual(snap["core"]["level"], "RED")
        self.assertTrue(all(j["is_demo"] for j in snap["agent_jobs"]))
        self.assertGreater(len(core.calls), 1)
        self.assertLessEqual(
            snap["agent_mb"], snap["mem"]["total"] - snap["mem"]["available"]
        )
        self.assertTrue(
            all(
                j["memory_kind"] == "Illustrative memory value"
                for j in snap["agent_jobs"]
            )
        )
        self.assertTrue(all(o["count"] == len(o["jobs"]) for o in snap["owners"]))
        json.dumps(snap)  # Snapshot remains queue/persistence friendly.


if __name__ == "__main__":
    unittest.main(verbosity=2)
