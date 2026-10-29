#!/usr/bin/env python3
"""SWARM METER sampler and decision adapter, extracted from the Amadeus prototype.

The /proc, host matching, Lean wire protocol, and SQLite schema are retained.
Astra's candidate adds stable identities, honest unavailable states, bounded core
requests, per-swarm mean-size capacity, and timestamp-based trends. No GUI imports
or process-termination controls belong here. RAM values are MiB throughout.

Public API: CoreClient.ask/close; Sampler.snapshot/close; History.add/close;
TrendTracker.add/summary/series; make_demo_snapshot. Existing snapshot names stay
compatible. Every verdict includes valid/proved/version/reason; UNKNOWN means a
required input is missing, so a UI must not turn it into a reassuring GREEN.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import selectors
import sqlite3
import subprocess
import threading
import time
from collections import deque

from config import core_paths

from process_roles import (
    ROLES,
    argv_tokens,
    command_identity,
    classify_process,
    agent_host_kind,
)

try:
    import gpu_reader as _gpu_reader
except ImportError:
    _gpu_reader = None

HOME = os.path.expanduser("~")
C_CLAUDE, C_CODEX, C_GEMINI = "#f0a24c", "#6fb3ff", "#7fe0c0"
C_OPENCLAW, C_OLLAMA, C_DOCKER = "#c084fc", "#e8e2d4", "#4dd0e1"
C_DESKTOP, C_SYSTEM, C_ORPHAN = "#8a8f98", "#555a63", "#e0563f"
GREEN, AMBER, RED = "#43d477", "#ecab3e", "#e0563f"
PSS_LARGE_PROCESS_MB = (
    48  # Sampling-priority threshold only; never a workload-count cutoff.
)
TREND_SECS = 90
_STATUS_RE = re.compile(r"^(Name|PPid|Uid|VmRSS):\s*(.*)$", re.M)


def read_text(path):
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def read_meminfo():
    """Keep MemAvailable: it includes reclaimable memory, unlike MemFree."""
    info = {}
    for line in read_text("/proc/meminfo").splitlines():
        k, _, value = line.partition(":")
        parts = value.split()
        if parts and parts[0].isdigit():
            info[k] = int(parts[0]) // 1024
    return {
        "total": info.get("MemTotal", 0),
        "available": info.get("MemAvailable", -1),
        "swap_total": info.get("SwapTotal", 0),
        "swap_free": info.get("SwapFree", 0),
    }


def read_pressure():
    """PSI some avg10 in tenths of a percent; None is unavailable, never zero."""
    match = re.search(r"some avg10=([\d.]+)", read_text("/proc/pressure/memory"))
    if not match:
        return None
    try:
        return int(round(float(match.group(1)) * 10))
    except ValueError:
        return None


def cpu_threads():
    return os.cpu_count() or 1


def process_starttime(stat_text):
    """Linux field 22 identifies a PID incarnation; comm may contain spaces/) ."""
    try:
        return int(stat_text[stat_text.rindex(")") + 1 :].split()[19])
    except (ValueError, IndexError):
        return 0


def read_processes(
    own_uid,
    memory_cache=None,
    agent_identities=None,
    metadata_cache=None,
    pss_budget_s=0.008,
):
    """Fresh identity/RSS every tick; expensive PSS refreshes have a CPU budget.

    Physical MemAvailable/pressure never use this cache. Attribution starts from
    current RSS or a labelled PSS baseline plus every positive RSS change. The
    refresh queue favours large agent-owned processes, then large desktop processes,
    and lastly small bookkeeping processes. We report actual baseline ages:
    four/thirty seconds are refresh targets, not unmeasured freshness promises.
    """
    procs = {}
    cache = memory_cache if memory_cache is not None else {}
    metadata = metadata_cache if metadata_cache is not None else {}
    clock = time.monotonic()
    seen_identities = set()
    try:
        uptime = float(read_text("/proc/uptime").split()[0])
    except (ValueError, IndexError):
        uptime = 0.0
    ticks = os.sysconf("SC_CLK_TCK")
    for ent in os.listdir("/proc"):
        if not ent.isdigit():
            continue
        base = "/proc/" + ent
        raw_stat = read_text(base + "/stat")
        started = process_starttime(raw_stat)
        status = read_text(base + "/status")
        if not status or not started:
            continue
        fields = dict(_STATUS_RE.findall(status))
        try:
            ppid = int(fields.get("PPid", "0") or 0)
            uid = int((fields.get("Uid", "0").split() or ["0"])[0])
            rss = (
                (int(fields["VmRSS"].split()[0]) + 1023) // 1024
                if "VmRSS" in fields
                else None
            )
        except ValueError:
            continue
        identity = (int(ent), started)
        name = fields.get("Name", "?")
        seen_identities.add(identity)
        cached_meta = metadata.get(identity)
        # Identity, name, and parent changes invalidate labels immediately.
        # Other command/folder changes are observed within ten seconds. Never
        # cache RSS, PPid, status, or process incarnation: those drive the meter.
        if (
            cached_meta
            and cached_meta["name"] == name
            and cached_meta["ppid"] == ppid
            and clock - cached_meta["at"] < 10
        ):
            cmd, cg, cwd = cached_meta["cmd"], cached_meta["cg"], cached_meta["cwd"]
            argv = (
                cached_meta["argv"]
                if "argv" in cached_meta
                else argv_tokens({"cmd": cmd, "name": name})
            )
            metadata_age = clock - cached_meta["at"]
        else:
            try:
                cwd = os.readlink(base + "/cwd")
            except OSError:
                cwd = ""
            raw_cmd = read_text(base + "/cmdline")
            argv = raw_cmd.rstrip("\0").split("\0") if raw_cmd else []
            cmd = " ".join(argv)[:4096]
            cg = read_text(base + "/cgroup").strip()
            metadata[identity] = {
                "name": name,
                "ppid": ppid,
                "cmd": cmd,
                "argv": argv,
                "cg": cg,
                "cwd": cwd,
                "at": clock,
            }
            metadata_age = 0.0
        cached = cache.get(identity)
        if cached and rss is not None:
            growth = rss - cached["rss"]
            mem = cached["pss"] + max(0, growth)
            memory_kind = "PSS" if growth == 0 else "PSS + RSS growth estimate"
            memory_age = clock - cached["at"]
        else:
            mem, memory_kind, memory_age = (
                rss,
                "RSS upper bound" if rss is not None else "RAM unavailable",
                0.0,
            )
        record = {
            "pid": int(ent),
            "name": name,
            "ppid": ppid,
            "uid": uid,
            "mem": mem,
            "rss": rss,
            "memory_kind": memory_kind,
            "memory_age_s": memory_age,
            "cmd": cmd,
            "argv": argv,
            "state": raw_stat[raw_stat.rfind(")") + 2 :].split()[0]
            if raw_stat
            else "?",
            "cg": cg,
            "cwd": cwd,
            "metadata_age_s": metadata_age,
            "starttime": started,
            "identity": identity,
            "age_s": max(0.0, uptime - started / ticks),
        }
        # Command purpose follows the existing metadata lifetime, while memory,
        # identity and parent are still refreshed every tick. The parser checks
        # the complete argv signature before reuse, including after same-PID exec.
        # Keeping its cache on this identity's metadata avoids repeated pathlib /
        # service analysis and discards it when that process incarnation exits.
        if cached_meta and cached_meta.get("_command_identity_cache"):
            record["_command_identity_cache"] = cached_meta["_command_identity_cache"]
        if process_starttime(read_text(base + "/stat")) == started:
            procs[int(ent)] = record
    # Choose refresh order after the fast pass, so directory enumeration cannot
    # starve later PIDs. A single kernel read can exceed the small budget; after
    # it finishes no further PSS reads run this tick. No thread or job is killed.
    due, attribution = [], {}
    for pid, record in procs.items():
        if record["uid"] != own_uid or record["rss"] is None or record["rss"] <= 0:
            continue
        identity = record["identity"]
        cached = cache.get(identity)
        is_agent = (
            agent_identities is None
            or identity in agent_identities
            or resolve_owner(pid, procs, attribution) is not None
        )
        large_agent_process = is_agent and record["rss"] >= PSS_LARGE_PROCESS_MB
        target_age = 4.0 if large_agent_process else 30.0
        age = clock - cached["at"] if cached else float("inf")
        growth = record["rss"] - cached["rss"] if cached else record["rss"]
        eligible = (
            not cached
            or age >= target_age
            or growth >= 64
            or clock >= cached["refresh_at"]
        )
        if eligible:
            # Aging eventually outranks a perpetually changing large host.
            priority = (
                0
                if large_agent_process
                else 1
                if record["rss"] >= PSS_LARGE_PROCESS_MB
                else 2
            )
            urgency = (age / target_age) if cached else 1e6
            due.append((priority, -urgency, pid))
    spent = 0.0
    for _priority, _urgency, pid in sorted(due):
        if spent >= pss_budget_s:
            break
        record = procs[pid]
        before = time.process_time()
        rollup = read_text(f"/proc/{pid}/smaps_rollup")
        match = re.search(r"^Pss:\s*(\d+)", rollup, re.M)
        rss_match = re.search(r"^Rss:\s*(\d+)", rollup, re.M)
        spent += time.process_time() - before
        if (
            not match
            or not rss_match
            or process_starttime(read_text(f"/proc/{pid}/stat")) != record["starttime"]
        ):
            continue
        identity = record["identity"]
        # Lean plans in integer MiB. Round positive footprints upward so tiny
        # recognized jobs are not silently reduced to a zero-sized estimate.
        pss = (int(match.group(1)) + 1023) // 1024
        is_agent = (
            agent_identities is None
            or identity in agent_identities
            or resolve_owner(pid, procs, attribution) is not None
        )
        target_age = 4.0 if is_agent and record["rss"] >= PSS_LARGE_PROCESS_MB else 30.0
        phase = ((identity[0] * 2654435761) ^ identity[1]) % 1000 / 1000
        # Stagger first refreshes; later ones retain their assigned phase.
        ttl = target_age if identity in cache else target_age * (0.5 + 0.5 * phase)
        # Pair RSS and PSS from this SAME kernel read. Pairing new PSS with
        # older fast-pass RSS can hide a subsequent re-growth after a shrink.
        paired_rss = (int(rss_match.group(1)) + 1023) // 1024
        cache[identity] = {
            "rss": paired_rss,
            "pss": pss,
            "at": clock,
            "refresh_at": clock + ttl,
        }
        record.update(mem=pss, memory_kind="PSS", memory_age_s=0.0)
    for record in procs.values():
        command_identity(record)  # Validate changed commands before sharing the cache.
        parsed = record.get("_command_identity_cache")
        if parsed is not None:
            # aggregate() adds intrinsic-role facts to this shared command object.
            metadata[record["identity"]]["_command_identity_cache"] = parsed
    for identities in (cache, metadata):
        for identity in list(identities):
            if identity not in seen_identities:
                del identities[identity]
    return procs


# Retained host matchers: a new host product can be added in one place.
HOSTS = [
    ("Claude Code", C_CLAUDE),
    ("Codex", C_CODEX),
    ("Gemini CLI", C_GEMINI),
    ("OpenClaw", C_OPENCLAW),
    ("Ollama", C_OLLAMA),
    ("Docker", C_DOCKER),
]
AGENT_KINDS = {h[0] for h in HOSTS}
HOST_COLOR = {h[0]: h[1] for h in HOSTS}
HOST_COLOR.update({"Orphans": C_ORPHAN, "Desktop": C_DESKTOP, "System": C_SYSTEM})
AGENT_CG_HINTS = ("claude", "chatgpt", "codex", "openclaw", "sg0-decode", "gemini")


def _docker_id(cg):
    match = re.search(r"docker-([0-9a-f]{12})", cg)
    return match.group(1) if match else "container"


def _match_host(process):
    """Identify the running CLI token, never a prompt mentioning an agent path."""
    kind = agent_host_kind(process)
    if kind:
        return kind
    if "docker-" in process.get("cg", ""):
        return "Docker"
    return None


def resolve_owner(pid, procs, cache):
    """Keep parent walking, but cache only the chain for the requested PID.

    The prototype cached a descendant's entire chain on all ancestors. That
    changed an ancestor's displayed chain depending on enumeration order.
    """
    if pid in cache:
        return cache[pid]
    chain, cur, seen = [], pid, set()
    result = None
    while cur in procs and cur not in seen and len(chain) < 60:
        seen.add(cur)
        process = procs[cur]
        kind = _match_host(process)
        if kind:
            key = _docker_id(process["cg"]) if kind == "Docker" else cur
            result = (kind, key, chain + [cur])
            break
        chain.append(cur)
        if process["ppid"] <= 1:
            break
        cur = process["ppid"]
    cache[pid] = result
    return result


def inherited_owner(pid, procs, previous_owners):
    """Carry recorded provenance through a surviving or newly extended subtree."""
    current, path, seen = pid, [], set()
    while current in procs and current not in seen and len(path) < 60:
        seen.add(current)
        process = procs[current]
        previous = previous_owners.get((current, process.get("starttime", 0)))
        if previous:
            if not path:
                return previous
            return {
                **previous,
                "chain": previous["chain"] + list(reversed(path)),
                "inherited": True,
            }
        path.append(process["name"])
        current = process["ppid"]
    return None


def orphan_evidence(process, procs, previous=None):
    """Observed lost ownership survives intact even when only an ancestor moves.

    The old immediate-parent-only test lost grandchildren when a host exited:
    bash reparented to init, but its Python child still had bash as its parent.
    Historical ownership is stronger evidence and must be considered first.
    """
    if previous:
        host = procs.get(previous.get("host_pid"))
        host_alive = host and host.get("starttime") == previous.get("host_starttime")
        if not host_alive:
            return {
                "confidence": "observed",
                "label": "Host exited",
                "reason": (
                    "An ancestor was observed under the agent host before that host exited."
                    if previous.get("inherited")
                    else "This same process was observed under the agent host before it exited."
                ),
                "previous_owner": previous["key"],
                "previous_label": previous["label"],
            }
        return None
    # Weaker first-observation heuristics remain explicitly 'possible'. Walking
    # the surviving chain also retains children subsequently born to that job.
    current, seen = process, set()
    while current and current["pid"] not in seen and len(seen) < 60:
        seen.add(current["pid"])
        parent = procs.get(current["ppid"])
        detached = parent is None or current["ppid"] <= 1 or parent["name"] == "systemd"
        agent_group = any(k in current["cg"].lower() for k in AGENT_CG_HINTS)
        virtual_python = "venv" in current["cmd"] and "python" in current["name"]
        if detached and (agent_group or virtual_python):
            inherited = current["pid"] != process["pid"]
            return {
                "confidence": "possible",
                "label": "Possible orphan",
                "reason": (
                    "An ancestor is a detached, possibly agent-associated process; ownership is unconfirmed."
                    if inherited
                    else "An agent-associated control group remains, but its parent host was not observed."
                    if agent_group
                    else "A detached virtual-environment Python process; agent ownership is unconfirmed."
                ),
            }
        current = parent
    return None


def is_orphan(process, procs):
    """Compatibility helper; callers needing certainty must inspect evidence."""
    return orphan_evidence(process, procs) is not None


def owner_label(kind, key, procs, docker_names):
    if kind in ("Claude Code", "Codex", "Gemini CLI"):
        cwd = procs.get(key, {}).get("cwd", "")
        where = (
            "~"
            if cwd == HOME
            else (os.path.basename(cwd) or "/")
            if cwd
            else "Unknown folder"
        )
        return f"{kind} · {where}"
    if kind == "OpenClaw":
        return "OpenClaw gateway"
    if kind == "Ollama":
        return "Ollama · model server"
    if kind == "Docker":
        return f"Docker · {docker_names.get(key, key)}"
    return kind


def docker_name_map():
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.ID}} {{.Names}}"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return dict(
            line.split(None, 1) for line in result.stdout.splitlines() if " " in line
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}


def gpu_snapshot_cli():
    """Diagnostic-only original NVIDIA's aggregate and compute-process queries; missing != zero.

    The compute list does not account for every graphics allocation. Per-job
    VRAM is therefore None when the driver did not report that process.
    """
    try:
        query = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.used,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if query.returncode:
            return None
        name, total, used, util = [
            part.strip() for part in query.stdout.strip().splitlines()[0].split(",")
        ]
        apps = subprocess.run(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,used_memory",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,
        )
        per_pid = {}
        if not apps.returncode:
            for line in apps.stdout.splitlines():
                parts = [x.strip() for x in line.split(",")]
                if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                    per_pid[int(parts[0])] = int(parts[1])
        return {
            "name": name,
            "total": int(float(total)),
            "used": int(float(used)),
            "util": int(float(util)) if util.replace(".", "").isdigit() else None,
            "per_pid": per_pid,
            "process_data_valid": not apps.returncode,
        }
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None


def gpu_snapshot():
    """Persistent read-only NVML avoids spawning expensive NVIDIA CLI helpers.

    The old CLI query is available above for explicit diagnostics only. Missing
    NVML returns unavailable; automatic fallback would reintroduce the measured
    CPU regression and would hide which sampler produced the data.
    """
    return _gpu_reader.gpu_snapshot() if _gpu_reader is not None else None


def derive_policy(total_mb, swap_mb):
    """Retained machine-policy choice. Lean applies it and returns every line.

    Policy choice is not itself proved. It is deliberately outside the UI, and
    the unchanged decision core receives these exact percentages each request.
    """
    if swap_mb < 4096:
        amber, red, why = (
            30,
            15,
            f"Only {swap_mb / 1024:.1f} GiB of swap; keep more RAM available.",
        )
    elif swap_mb < 16384:
        amber, red, why = (
            25,
            10,
            f"{swap_mb / 1024:.1f} GiB of swap provides a limited reserve.",
        )
    else:
        amber, red, why = (
            20,
            8,
            f"{swap_mb / 1024:.1f} GiB of swap provides a larger reserve.",
        )
    return {"amberPct": amber, "redPct": red, "amberPSI": 10, "redPSI": 100, "why": why}


class CoreClient:
    """One persistent proved binary; only a valid answer earns PROVED status."""

    def __init__(self, core_bin=None, core_dir=None, timeout=0.35):
        self.core_dir, self.core_bin = core_paths(core_bin, core_dir)
        toolchain = read_text(self.core_dir / "lean-toolchain").strip()
        self.version = toolchain.split(":")[-1] if toolchain else "unknown"
        self.timeout = timeout
        self.proc = None
        self.proved = False
        self.status = "Not sampled"
        self._buffer = b""
        self._retry_at = 0.0

    @staticmethod
    def replica(total, avail, pressure, jobsize, pol):
        """The sole unproved replica; matches existing Lean natural arithmetic."""
        red = avail * 100 < total * pol["redPct"] or pressure >= pol["redPSI"]
        amber = avail * 100 < total * pol["amberPct"] or pressure >= pol["amberPSI"]
        level = "RED" if red else "AMBER" if amber else "GREEN"
        red_line = (total * pol["redPct"] + 99) // 100
        amber_line = (total * pol["amberPct"] + 99) // 100
        return {
            "level": level,
            "rank": {"GREEN": 0, "AMBER": 1, "RED": 2}[level],
            "redLine": red_line,
            "amberLine": amber_line,
            "room": max(0, avail - red_line) // max(1, jobsize),
            "jobSize": max(1, jobsize),
        }

    def _start(self):
        if time.monotonic() < self._retry_at:
            return False
        try:
            self.proc = subprocess.Popen(
                [str(self.core_bin)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
            os.set_blocking(self.proc.stdout.fileno(), False)
            self._buffer = b""
            return True
        except OSError as exc:
            self.status = f"Core unavailable: {exc.strerror or type(exc).__name__}"
            self._retry_at = time.monotonic() + 5
            self.proc = None
            return False

    def _readline(self):
        deadline = time.monotonic() + self.timeout
        with selectors.DefaultSelector() as selector:
            selector.register(self.proc.stdout, selectors.EVENT_READ)
            while b"\n" not in self._buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise TimeoutError("Core did not answer before deadline")
                chunk = os.read(self.proc.stdout.fileno(), 4096)
                if not chunk:
                    raise OSError("Core closed its response stream")
                self._buffer += chunk
                if len(self._buffer) > 8192:
                    raise ValueError("Oversized core response")
        line, self._buffer = self._buffer.split(b"\n", 1)
        return line.decode("ascii")

    @staticmethod
    def _parse(line, jobsize):
        fields = dict(token.split("=", 1) for token in line.split())
        required = {"level", "rank", "redLine", "amberLine", "room", "jobSize"}
        if set(fields) != required or fields["level"] not in ("GREEN", "AMBER", "RED"):
            raise ValueError("Incomplete or invalid core response")
        answer = {
            key: (value if key == "level" else int(value))
            for key, value in fields.items()
        }
        if any(answer[key] < 0 for key in required - {"level"}):
            raise ValueError("Negative core result")
        if answer["rank"] != {"GREEN": 0, "AMBER": 1, "RED": 2}[answer["level"]]:
            raise ValueError("Inconsistent core level/rank")
        if answer["jobSize"] != jobsize or answer["redLine"] > answer["amberLine"]:
            raise ValueError("Inconsistent core size/line order")
        return answer

    def ask(self, total, avail, pressure, jobsize, pol):
        jobsize = max(1, int(jobsize or 1))
        memory_valid = (
            isinstance(total, int)
            and isinstance(avail, int)
            and total > 0
            and 0 <= avail <= total
        )
        pressure_valid = isinstance(pressure, int) and 0 <= pressure <= 1000
        policy_valid = (
            0 <= pol["redPct"] <= pol["amberPct"] <= 100
            and 0 < pol["amberPSI"] <= pol["redPSI"] <= 1000
        )
        if not (memory_valid and pressure_valid and policy_valid):
            self.proved = False
            self.status = (
                "Required memory or pressure sample unavailable"
                if policy_valid
                else "Invalid policy"
            )
            return {
                "level": "UNKNOWN",
                "rank": None,
                "redLine": None,
                "amberLine": None,
                "room": None,
                "jobSize": jobsize,
                "valid": False,
                "proved": False,
                "version": self.version,
                "reason": self.status,
            }
        wire = f"{total} {avail} {pressure} {jobsize} {pol['amberPct']} {pol['redPct']} {pol['amberPSI']} {pol['redPSI']}\n"
        if self.proc is None or self.proc.poll() is not None:
            self.close()
            self._start()
        if self.proc is not None:
            try:
                self.proc.stdin.write(wire.encode("ascii"))
                self.proc.stdin.flush()
                answer = self._parse(self._readline(), jobsize)
                self.proved, self.status = True, "PROVED"
                return {
                    **answer,
                    "valid": True,
                    "proved": True,
                    "version": self.version,
                    "reason": "",
                }
            except (OSError, ValueError, TimeoutError, UnicodeError) as exc:
                self.status = str(exc)
                self.close()
                self._retry_at = time.monotonic() + 5
        self.proved = False
        return {
            **self.replica(total, avail, pressure, jobsize, pol),
            "valid": True,
            "proved": False,
            "version": self.version,
            "reason": self.status,
        }

    def close(self):
        """Release only the child core we started; no monitored job is signaled."""
        proc, self.proc = self.proc, None
        if proc is not None:
            try:
                proc.stdin.close()
                proc.wait(timeout=0.1)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    proc.terminate()  # A wedged private protocol helper, never a job.
                    proc.wait(timeout=0.1)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            finally:
                if proc.stdout:
                    proc.stdout.close()
        self.proved = False


class Sampler:
    """Reuse the original aggregation; enrich snapshots before the UI sees them."""

    def __init__(self, core_bin=None, core_dir=None):
        self.uid = os.getuid()
        self.core = CoreClient(core_bin=core_bin, core_dir=core_dir)
        self.docker_names = {}
        self._docker_at = 0.0
        self.policy = None
        self._previous_owners = {}
        self._agent_identities = set()
        self.gpu_name = ""
        self._memory_cache = {}
        self._metadata_cache = {}
        self._sample_count = 0
        self._enrichment_thread = None
        self._enrichment_result = None
        self._enrichment_at = 0.0
        self._closed = False

    def _enrichment(self):
        """Only device/container lookups run here; they never block RAM samples.

        This is one app-owned worker, not an installed daemon. Each external
        query has a timeout; closing the app prevents further work being queued.
        """
        observed_at = time.monotonic()
        gpu = gpu_snapshot()
        completed = time.monotonic()
        if gpu is not None:
            observed_at = gpu.get("sampled_at_monotonic", observed_at)
        names = None
        if not self._closed and completed - self._docker_at > 60:
            names = docker_name_map()
        self._enrichment_result = (gpu, observed_at, names)

    def _poll_enrichment(self):
        result = self._enrichment_result
        if result:
            gpu, completed, names = result
            if names is not None:
                self.docker_names = names
                self._docker_at = completed
            age = time.monotonic() - completed
            # Stale VRAM is never passed off as current. The UI also receives
            # its age while valid, because RAM and GPU sampling are independent.
            if age > 10:
                gpu = None
        else:
            gpu, age = None, None
        running = self._enrichment_thread and self._enrichment_thread.is_alive()
        if (
            not self._closed
            and not running
            and time.monotonic() - self._enrichment_at >= 1.5
        ):
            self._enrichment_at = time.monotonic()
            self._enrichment_thread = threading.Thread(
                target=self._enrichment, name="swarm-device-sample", daemon=True
            )
            self._enrichment_thread.start()
        status = (
            "pending"
            if result is None
            else "stale"
            if age is not None and age > 10
            else "unavailable"
            if gpu is None
            else "current"
        )
        return gpu, age, status

    def aggregate(self, procs, mem, pressure, gpu, now=None):
        """Pure attribution/aggregation seam for deterministic synthetic tests."""
        now = time.time() if now is None else now
        self.policy = derive_policy(mem["total"], mem["swap_total"])
        owners, cache, observed = {}, {}, {}
        agent_identities = set()

        def bucket(key, kind, label, host=None):
            if key not in owners:
                host = host or {}
                owners[key] = {
                    "key": key,
                    "kind": kind,
                    "label": label,
                    "color": HOST_COLOR.get(kind, C_SYSTEM),
                    "mem": 0,
                    "count": 0,
                    "vram": 0 if gpu and gpu.get("process_data_valid", True) else None,
                    "jobs": [],
                    "processes": [],
                    "cwd": host.get("cwd", ""),
                    "age_s": host.get("age_s"),
                    "host_pid": host.get("pid"),
                    "host_starttime": host.get("starttime"),
                    "vram_complete": False,
                    "memory_estimated": False,
                    "memory_age_s": 0.0,
                }
            return owners[key]

        for pid, process in procs.items():
            identity = (pid, process.get("starttime", 0))
            result = resolve_owner(pid, procs, cache)
            previous = inherited_owner(pid, procs, self._previous_owners)
            previous_host = procs.get(previous.get("host_pid")) if previous else None
            prior_host_alive = bool(
                previous_host
                and previous_host.get("starttime") == previous.get("host_starttime")
            )
            evidence = None
            if result:
                kind, key, chain = result
                host = procs[chain[-1]]
                owner = bucket(
                    f"{kind}:{key}",
                    kind,
                    owner_label(kind, key, procs, self.docker_names),
                    host,
                )
                chain_names = [procs[c]["name"] for c in reversed(chain)]
                observed[identity] = {
                    "key": owner["key"],
                    "label": owner["label"],
                    "kind": kind,
                    "host_pid": host["pid"],
                    "host_starttime": host.get("starttime"),
                    "chain": chain_names,
                }
            elif prior_host_alive:
                # A worker may detach intentionally while its known host is
                # still alive. Preserve observed provenance instead of calling
                # it an orphan simply because its new parent is init/systemd.
                owner = bucket(
                    previous["key"], previous["kind"], previous["label"], previous_host
                )
                chain_names = previous["chain"]
                observed[identity] = previous
            else:
                evidence = orphan_evidence(process, procs, previous)
                if evidence:
                    owner = bucket(
                        "orphans", "Orphans", "Jobs without an observed host"
                    )
                    chain_names = [
                        "(host exited)"
                        if evidence["confidence"] == "observed"
                        else "(host unknown)",
                        process["name"],
                    ]
                    if evidence["confidence"] == "observed":
                        observed[identity] = previous
                elif process["uid"] == self.uid:
                    owner = bucket("desktop", "Desktop", "Desktop apps & shell")
                    chain_names = [process["name"]]
                else:
                    owner = bucket("system", "System", "System services")
                    chain_names = [process["name"]]
            if owner["kind"] in AGENT_KINDS or owner["kind"] == "Orphans":
                agent_identities.add(identity)
            owner["mem"] += (
                process["mem"]
                if isinstance(process.get("mem"), (int, float)) and process["mem"] >= 0
                else 0
            )
            owner["memory_estimated"] |= (
                "estimate" in process.get("memory_kind", "")
                or "RSS" in process.get("memory_kind", "")
                or process.get("memory_age_s", 0.0) > 0
            )
            owner["memory_age_s"] = max(
                owner["memory_age_s"], process.get("memory_age_s", 0.0)
            )
            gpu_identity_matches = (
                gpu
                and gpu.get("process_data_valid", True)
                and gpu.get("pid_starttimes", {}).get(pid) == process.get("starttime")
            )
            process_vram = gpu["per_pid"].get(pid) if gpu_identity_matches else None
            if process_vram is not None and owner["vram"] is not None:
                owner["vram"] += process_vram
            role, role_reason = classify_process(
                process, procs, owner["kind"], owner.get("host_pid")
            )
            guarded = "sg0-decode" in process["cg"]
            owner["processes"].append(
                {
                    "pid": pid,
                    "starttime": identity[1],
                    "identity": identity,
                    "name": process["name"],
                    "role": role,
                    "role_reason": role_reason,
                    "state": process.get("state", "?"),
                    "parent_pid": process.get("ppid"),
                    "mem": process["mem"],
                    "cmd": process["cmd"],
                    "memory_kind": process.get("memory_kind", "PSS/RSS"),
                    "metadata_age_s": process.get("metadata_age_s", 0.0),
                    "memory_age_s": process.get("memory_age_s", 0.0),
                    "chain": chain_names,
                    "owner": owner["key"],
                    "kind": owner["kind"],
                    "cwd": process.get("cwd", ""),
                    "age_s": process.get("age_s"),
                    "vram": process_vram,
                    "guarded": guarded,
                    "guard_evidence": "sg0-decode control group" if guarded else None,
                    "orphan_evidence": evidence,
                    "is_demo": False,
                }
            )
        self._previous_owners = observed
        self._agent_identities = agent_identities
        by_label = {}
        for owner in owners.values():
            by_label.setdefault(owner["label"], []).append(owner)
        for label, group in by_label.items():
            if len(group) > 1:
                for owner in group:
                    owner["label"] = f"{label} · PID {owner['host_pid']}"
        snapshot = {
            "ts": now,
            "mem": mem,
            "pressure": pressure,
            "gpu": gpu,
            "policy": self.policy,
            "owners": sorted(owners.values(), key=lambda o: -o["mem"]),
            "core_version": self.core.version,
            "nproc": len(procs),
            "validity": {
                "memory": mem["total"] > 0 and 0 <= mem["available"] <= mem["total"],
                "pressure": pressure is not None,
                "gpu": gpu is not None,
            },
            "demo": False,
            "sample_duration_s": None,
        }
        return recompute_workload_metrics(snapshot, self.core)

    def snapshot(self):
        started = time.monotonic()
        mem, pressure = read_meminfo(), read_pressure()
        gpu, gpu_age, gpu_status = self._poll_enrichment()
        if gpu:
            self.gpu_name = gpu["name"]
        procs = read_processes(
            self.uid, self._memory_cache, self._agent_identities, self._metadata_cache
        )
        snapshot = self.aggregate(procs, mem, pressure, gpu)
        self._sample_count += 1
        snapshot["gpu_age_s"] = gpu_age
        snapshot["gpu_status"] = gpu_status
        snapshot["gpu_pending"] = self._enrichment_result is None
        snapshot["sample_duration_s"] = time.monotonic() - started
        return snapshot

    def close(self):
        self._closed = True
        self.core.close()
        # Existing external lookups have a combined six-second GPU timeout.
        # They cannot queue Docker work after close; join the app-owned worker
        # so no continuing sampler work is left behind when shutdown returns.
        if (
            self._enrichment_thread
            and self._enrichment_thread is not threading.current_thread()
        ):
            self._enrichment_thread.join(timeout=6.5)
        if _gpu_reader is not None and not (
            self._enrichment_thread and self._enrichment_thread.is_alive()
        ):
            _gpu_reader.close_gpu_reader()


def recompute_workload_metrics(snapshot, core_client):
    """One accounting path for live samples and scaled, inert demo fixtures.

    Every attributed process remains inspectable and contributes known RAM to
    its owner. Only recognized workload processes supply the job count, average,
    planning size, and capacity input. Unknown roles are never silently promoted
    to jobs. Process count is not an exact count of logical agents or requests.
    """

    def measured(value):
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value >= 0
        )

    def total(items):
        values = [p.get("mem") for p in items]
        return sum(values) if all(measured(v) for v in values) else None

    owners = snapshot["owners"]
    for owner in owners:
        processes = owner.get("processes", [])
        # Do not discard old opaque records: missing role evidence stays visible.
        for process in processes:
            if process.get("role") not in ROLES:
                process.update(
                    role="unclassified",
                    role_reason="No reliable role classification available",
                )
        owner["process_count"] = len(processes)
        owner["mem"] = sum(p["mem"] for p in processes if measured(p.get("mem")))
        owner["memory_complete"] = all(measured(p.get("mem")) for p in processes)
        owner["memory_estimated"] = not owner["memory_complete"] or any(
            "estimate" in p.get("memory_kind", "")
            or "RSS" in p.get("memory_kind", "")
            or p.get("memory_age_s", 0) > 0
            for p in processes
        )
        for role in ROLES:
            members = [p for p in processes if p["role"] == role]
            owner[role + "_count"] = len(members)
            owner[role + "_mb"] = total(members)
        owner["jobs"] = [p for p in processes if p["role"] == "workload"]
        owner["count"] = owner["active_count"] = len(owner["jobs"])
        owner["count_basis"] = "identified_workload_processes_v1"
        owner["workload_memory_estimated"] = any(
            "estimate" in p.get("memory_kind", "")
            or "RSS" in p.get("memory_kind", "")
            or p.get("memory_age_s", 0) > 0
            for p in owner["jobs"]
        )
        owner["mean_mb"] = (
            owner["workload_mb"] / owner["count"]
            if owner["count"] and owner["workload_mb"] is not None
            else None
        )
        # A process whose current footprint is zero has no usable planning size.
        owner["mean_size_mb"] = (
            math.ceil(owner["mean_mb"])
            if owner["mean_mb"] is not None and owner["mean_mb"] > 0
            else None
        )
        owner["planning_mb"] = (
            max(owner["mean_size_mb"], math.ceil(max(p["mem"] for p in owner["jobs"])))
            if owner["mean_size_mb"] is not None
            else None
        )
        owner["overhead_mb"] = sum(
            p["mem"]
            for p in processes
            if p["role"] != "workload" and measured(p.get("mem"))
        )
        owner["overhead_count"] = owner["process_count"] - owner["count"]
        is_agent = owner["kind"] in AGENT_KINDS or owner["kind"] == "Orphans"
        for key, size in (
            ("capacity", owner["mean_size_mb"]),
            ("planning_capacity", owner["planning_mb"]),
        ):
            owner[key] = (
                core_client.ask(
                    snapshot["mem"]["total"],
                    snapshot["mem"]["available"],
                    snapshot["pressure"],
                    size,
                    snapshot["policy"],
                )
                if is_agent and size is not None
                else None
            )
        owner["capacity_room"] = (
            owner["capacity"]["room"] if owner["capacity"] else None
        )

    agent_owners = [
        o for o in owners if o["kind"] in AGENT_KINDS or o["kind"] == "Orphans"
    ]
    agent_processes = [p for owner in agent_owners for p in owner["processes"]]
    agent_jobs = sorted(
        [p for p in agent_processes if p["role"] == "workload"],
        key=lambda p: p["mem"] if measured(p.get("mem")) else -1,
        reverse=True,
    )
    workload_mb = total(agent_jobs)
    job_size = (
        math.ceil(workload_mb / len(agent_jobs))
        if agent_jobs and workload_mb is not None and workload_mb > 0
        else None
    )
    verdict = core_client.ask(
        snapshot["mem"]["total"],
        snapshot["mem"]["available"],
        snapshot["pressure"],
        job_size or 1,
        snapshot["policy"],
    )
    if job_size is None:
        # The internal size1 is only required by the binary's lamp/line request.
        # No observed size means no public capacity: never display a fake default.
        verdict = dict(
            verdict,
            room=None,
            jobSize=None,
            capacity_valid=False,
            capacity_reason="No observed workload footprint",
        )
    else:
        verdict = dict(
            verdict,
            capacity_valid=verdict.get("valid", False),
            capacity_reason="Identified workload process average",
        )
    snapshot.update(
        agent_jobs=agent_jobs,
        agent_processes=agent_processes,
        agent_mb=sum(o["mem"] for o in agent_owners),
        job_size=job_size,
        workload_mb=workload_mb,
        count_basis="identified_workload_processes_v1",
        core=verdict,
        core_version=core_client.version,
        proved=verdict["proved"]
        and all(
            result["proved"]
            for owner in agent_owners
            for result in (owner["capacity"], owner["planning_capacity"])
            if result is not None
        ),
    )
    for role in ROLES:
        members = [p for p in agent_processes if p["role"] == role]
        snapshot[role + "_count"] = len(members)
        snapshot[role + "_mb"] = total(members)
    return snapshot


class History:
    """Keep the existing nine-column schema and expose failures to the UI."""

    def __init__(self, path):
        self.path = str(path)
        self.db = None
        self.healthy = False
        self.status = "Not opened"
        self.last_write_ts = None
        self._pruned_at = 0.0
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(self.path)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("""CREATE TABLE IF NOT EXISTS samples (
                ts REAL PRIMARY KEY, total INT, available INT, pressure INT, level TEXT,
                vram_used INT, agent_mb INT, jobs INT, owners TEXT)""")
            self.db.execute("DELETE FROM samples WHERE ts < ?", (time.time() - 86400,))
            self.db.commit()
            self.healthy, self.status = True, "24 h history ready"
        except (OSError, sqlite3.Error) as exc:
            self.status = f"History unavailable: {exc}"

    def add(self, snap):
        if self.db is None:
            return False
        try:
            owners = [
                {
                    "label": o["label"],
                    "mem": o["mem"],
                    "count": o["count"],
                    "vram": o["vram"],
                    "count_basis": o.get(
                        "count_basis", "identified_workload_processes_v1"
                    ),
                    "mean_mb": o.get("mean_mb"),
                    **{
                        role + suffix: o.get(role + suffix)
                        for role in ROLES
                        for suffix in ("_mb", "_count")
                    },
                    "process_count": o.get("process_count"),
                }
                for o in snap["owners"]
            ]
            self.db.execute(
                "INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    snap["ts"],
                    snap["mem"]["total"],
                    snap["mem"]["available"],
                    snap["pressure"],
                    snap["core"]["level"],
                    snap["gpu"]["used"] if snap["gpu"] else None,
                    snap["agent_mb"],
                    len(snap["agent_jobs"]),
                    json.dumps(owners),
                ),
            )
            # Prune by elapsed time, not 150 samples, so interval changes do not
            # alter the 24-hour retention promise. Prune immediately at startup.
            if time.monotonic() - self._pruned_at >= 60:
                self.db.execute(
                    "DELETE FROM samples WHERE ts < ?", (time.time() - 86400,)
                )
                self._pruned_at = time.monotonic()
            self.db.commit()
            self.healthy, self.status, self.last_write_ts = (
                True,
                "24 h history recording",
                snap["ts"],
            )
            return True
        except sqlite3.Error as exc:
            self.healthy, self.status = False, f"History write failed: {exc}"
            return False

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None


def slope_mb_per_s(points):
    """Retain the prototype's least-squares forecast; it is an estimate."""
    if len(points) < 3:
        return 0.0
    t0 = points[0][0]
    xs, ys = [p[0] - t0 for p in points], [p[1] for p in points]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    denominator = sum((x - mx) ** 2 for x in xs)
    return (
        sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denominator
        if denominator
        else 0.0
    )


class TrendTracker:
    """Time-based windows keep the graph honest at 1, 2, or 4 second sampling."""

    def __init__(self):
        self._points = deque()
        self._last = None

    @property
    def series(self):
        """Rows: ts, available MiB, agent MiB, job count; exact last 300 seconds."""
        return list(self._points)

    def add(self, snapshot):
        ts = snapshot["ts"]
        if self._points and ts <= self._points[-1][0]:
            # Clock corrections start a fresh window rather than inventing slope.
            self._points.clear()
        self._last = snapshot
        self._points.append(
            (
                ts,
                snapshot["mem"]["available"],
                snapshot["agent_mb"],
                len(snapshot["agent_jobs"]),
            )
        )
        while self._points and ts - self._points[0][0] > 300:
            self._points.popleft()
        return self.summary()

    def summary(self):
        result = {
            "trend_text": "Learning trend…",
            "rate_mib_s": None,
            "eta_s": None,
            "learning": True,
            "ramp_delta": 0,
            "ramp_span_s": 0.0,
            "ramp_text": "",
            "estimated": True,
            "span_s": 0.0,
        }
        if not self._points or not self._last:
            return result
        now = self._points[-1][0]
        recent = [p for p in self._points if now - p[0] <= 90 and p[1] >= 0]
        minute = [p for p in self._points if now - p[0] <= 60]
        if len(minute) >= 2:
            delta, span = minute[-1][3] - minute[0][3], minute[-1][0] - minute[0][0]
            result.update(ramp_delta=delta, ramp_span_s=span)
            if delta >= 4:
                result["ramp_text"] = f"Net +{delta} jobs over {span:.0f} s observed"
        span = recent[-1][0] - recent[0][0] if recent else 0
        result["span_s"] = span
        if len(recent) < 3 or span < 10 or not self._last["core"]["valid"]:
            return result
        slope = slope_mb_per_s([(p[0], p[1]) for p in recent])
        per_minute = slope * 60
        result.update(learning=False, rate_mib_s=slope)
        if per_minute <= -150:
            red_line = self._last["core"]["redLine"]
            available = self._last["mem"]["available"]
            eta = (
                max(0.0, (available - red_line) / -slope)
                if red_line is not None
                else None
            )
            result["eta_s"] = eta
            result["trend_text"] = (
                f"RAM falling {abs(per_minute) / 1024:.1f} GiB/min · estimate"
            )
        elif per_minute >= 150:
            result["trend_text"] = (
                f"RAM recovering {per_minute / 1024:.1f} GiB/min · estimate"
            )
        else:
            result["trend_text"] = "RAM steady over the observed window"
        return result


def make_demo_snapshot(core_client=None):
    """Reconstruct the prototype's crash workload, truthfully asking the core.

    This is synthetic historical illustration, not a current measurement. The
    returned jobs carry is_demo so action controls must refuse live delivery.
    """
    own_core = core_client is None
    sampler = Sampler()
    if core_client is not None:
        sampler.core = core_client
    uid, procs = os.getuid(), {}

    def process(pid, name, parent, mem, cwd="", cmd=None, cg=""):
        procs[pid] = {
            "pid": pid,
            "name": name,
            "ppid": parent,
            "uid": uid,
            "mem": mem,
            "cmd": cmd or name,
            "cg": cg,
            "cwd": cwd,
            "starttime": 10000 + pid,
            "identity": (pid, 10000 + pid),
            "age_s": 900.0,
            "memory_kind": "Illustrative memory value",
        }

    process(4383, "claude", 1, 1366, HOME + "/projects/example-project")
    for i, size in enumerate(
        [
            8192,
            3600,
            3600,
            3400,
            3400,
            3300,
            3300,
            3100,
            3100,
            3000,
            3000,
            2900,
            2900,
            2800,
            2800,
        ]
    ):
        process(
            665093 + i * 97,
            "python",
            4383,
            size,
            HOME + "/projects/example-project",
            "venv2/bin/python dec.py --src both 12.3-15.6",
        )
    process(6729, "codex", 1, 297, HOME)
    for i, (name, mem) in enumerate(
        (("node", 410), ("bash", 60), ("python3", 520), ("node", 380), ("rg", 70)), 1
    ):
        process(
            6729 + i,
            name,
            6729,
            mem,
            HOME,
            cmd={
                1: "node analyze.js",
                2: "bash",
                3: "python3 train.py",
                4: "node transform.js",
                5: "rg pattern",
            }[i],
        )
    process(
        647092,
        "python",
        1,
        3960,
        cmd="venv2/bin/python cpuvox.py",
        cg="0::/app-claude.scope",
    )
    process(2303, "node", 1, 1068, cmd="node node_modules/openclaw gateway")
    process(
        3612,
        "python3",
        1,
        497,
        cmd="uvicorn open_webui.main:app",
        cg="0::/docker-1db25afbf94e.scope",
    )
    sampler.docker_names = {"1db25afbf94e": "open-webui"}
    mem = {"total": 63727, "available": 2200, "swap_total": 2047, "swap_free": 0}
    gpu = {
        "name": "NVIDIA GeForce RTX 5090 Laptop GPU",
        "total": 24463,
        "used": 3480,
        "util": 12,
        "per_pid": {},
        "process_data_valid": True,
    }
    try:
        snapshot = sampler.aggregate(procs, mem, 240, gpu)
    finally:
        if own_core:
            sampler.close()
    snapshot.update(
        demo=True,
        demo_label="Illustrative reconstruction of the 25 October crash",
        gpu_age_s=0.0,
        gpu_status="demo",
        gpu_pending=False,
    )
    for owner in snapshot["owners"]:
        for process in owner["processes"]:
            process["is_demo"] = True
    return snapshot


# Compatibility spelling used by earlier screenshot entry points.
_demo_snapshot = make_demo_snapshot
