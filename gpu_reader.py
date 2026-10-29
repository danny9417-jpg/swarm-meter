"""Low-overhead, read-only NVIDIA telemetry through the installed NVML driver.

The original sampler repeatedly started nvidia-smi. NVML is the same driver's
query interface, accessed here with stdlib ctypes and one retained connection.
No management/setter API is bound. No package installation or helper process is
required, and unavailable measurements remain unavailable rather than zero.

ABI references (reviewed 2026-10-29):
https://docs.nvidia.com/deploy/nvml-api/latest/api/group__nvmlDeviceQueries.html
https://docs.nvidia.com/deploy/archive/R510/nvml-api/structnvmlProcessInfo__t.html
https://github.com/NVIDIA/go-nvml/blob/main/pkg/nvml/nvml.h

These are independently written Python bindings to documented C declarations,
not a redistributed NVIDIA header. The explicit v2 process symbol takes the
24-byte process-info layout below on Linux x86_64. Do not substitute a different
versioned symbol without checking its matching structure and calling convention.
"""

from __future__ import annotations

import atexit
import ctypes as C
from pathlib import Path
import threading
import time


SUCCESS = 0
INSUFFICIENT_SIZE = 7
NOT_SUPPORTED = 3
UNAVAILABLE_MEMORY = (1 << 64) - 1
MIB = 1024 * 1024


class _Memory(C.Structure):
    # Preserve the documented native alignment; never use _pack_ here.
    _fields_ = [
        ("total", C.c_ulonglong),
        ("free", C.c_ulonglong),
        ("used", C.c_ulonglong),
    ]


class _MemoryV2(C.Structure):
    _fields_ = [
        ("version", C.c_uint),
        ("total", C.c_ulonglong),
        ("reserved", C.c_ulonglong),
        ("free", C.c_ulonglong),
        ("used", C.c_ulonglong),
    ]


class _Utilization(C.Structure):
    _fields_ = [("gpu", C.c_uint), ("memory", C.c_uint)]


class _ProcessInfoV2(C.Structure):
    _fields_ = [
        ("pid", C.c_uint),
        ("usedGpuMemory", C.c_ulonglong),
        ("gpuInstanceId", C.c_uint),
        ("computeInstanceId", C.c_uint),
    ]


def _starttime(pid: int) -> int | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        return int(raw[raw.rfind(")") + 2 :].split()[19])
    except (OSError, ValueError, IndexError):
        return None


class NvmlError(RuntimeError):
    pass


class NVMLReader:
    """One connection covering every detected physical NVIDIA device.

    Memory totals are sums across devices, not a shared allocation pool. Per-card
    figures remain in ``devices``. A multi-card utilization percentage would be
    misleading without workload weighting, so only per-card utilization is given.

    ``library`` and ``identity_reader`` are injection seams for ABI/error tests.
    A failed process query does not discard valid device memory, but explicitly
    marks per-process accounting incomplete. A failed device-memory query returns
    None. The caller can display ``last_error`` through its details/status view.
    """

    def __init__(
        self, *, library=None, identity_reader=_starttime, clock=time.monotonic
    ):
        self._lock = threading.RLock()
        self._library = library
        self._identity_reader = identity_reader
        self._clock = clock
        self._initialized = False
        self._devices = []
        self._process_capacity = 16
        self._retry_at = 0.0
        self.last_error = "Not sampled"
        self.name = "NVIDIA GPU"
        self._functions = {}

    def _bind(self, name, arguments, result=C.c_int, optional=False):
        try:
            function = getattr(self._library, name)
        except AttributeError:
            if optional:
                return None
            raise NvmlError("Installed NVML does not provide " + name)
        function.argtypes = arguments
        function.restype = result
        self._functions[name] = function
        return function

    def _error(self, code, operation):
        text = None
        error_string = self._functions.get("nvmlErrorString")
        if error_string:
            try:
                raw = error_string(code)
                text = raw.decode("utf-8", "replace") if raw else None
            except (TypeError, ValueError):
                pass
        return f"{operation}: {text or ('NVML error ' + str(code))}"

    def _check(self, code, operation):
        if code != SUCCESS:
            raise NvmlError(self._error(code, operation))

    def _initialize(self):
        if self._initialized:
            return
        if self._clock() < self._retry_at:
            raise NvmlError(self.last_error)
        # c_void_p represents the opaque device handle. These calls return an
        # integer nvmlReturn_t; pointer-return defaults must never be inferred.
        if self._library is None:
            self._library = C.CDLL("libnvidia-ml.so.1")
        self._bind("nvmlErrorString", [C.c_int], C.c_char_p, optional=True)
        initialize = self._bind("nvmlInit_v2", [])
        self._shutdown = self._bind("nvmlShutdown", [])
        self._count = self._bind("nvmlDeviceGetCount_v2", [C.POINTER(C.c_uint)])
        handle = self._bind(
            "nvmlDeviceGetHandleByIndex_v2", [C.c_uint, C.POINTER(C.c_void_p)]
        )
        self._memory = self._bind(
            "nvmlDeviceGetMemoryInfo", [C.c_void_p, C.POINTER(_Memory)]
        )
        self._memory_v2 = self._bind(
            "nvmlDeviceGetMemoryInfo_v2",
            [C.c_void_p, C.POINTER(_MemoryV2)],
            optional=True,
        )
        self._util = self._bind(
            "nvmlDeviceGetUtilizationRates",
            [C.c_void_p, C.POINTER(_Utilization)],
            optional=True,
        )
        self._processes = self._bind(
            "nvmlDeviceGetComputeRunningProcesses_v2",
            [C.c_void_p, C.POINTER(C.c_uint), C.POINTER(_ProcessInfoV2)],
            optional=True,
        )
        get_name = self._bind(
            "nvmlDeviceGetName",
            [C.c_void_p, C.POINTER(C.c_char), C.c_uint],
            optional=True,
        )
        self._check(initialize(), "NVML initialization")
        # Every successful init has exactly one matching shutdown, even if the
        # subsequent handle lookup fails before a snapshot can be returned.
        self._initialized = True
        try:
            count = self._device_count()
            devices = []
            for index in range(count):
                device = C.c_void_p()
                self._check(handle(index, C.byref(device)), f"GPU device {index}")
                label = f"NVIDIA GPU {index}"
                if get_name:
                    name = C.create_string_buffer(256)
                    if get_name(device, name, len(name)) == SUCCESS:
                        label = name.value.decode("utf-8", "replace") or label
                devices.append((device, label))
            self._devices = devices
            self.name = (
                devices[0][1] if count == 1 else f"{count} NVIDIA GPUs (combined)"
            )
        except Exception:
            self.close()
            raise

    def _device_count(self):
        count = C.c_uint()
        self._check(self._count(C.byref(count)), "GPU enumeration")
        if count.value == 0:
            raise NvmlError("No NVIDIA GPUs detected")
        if count.value > 1024:
            raise NvmlError("NVML returned an unreasonable device count")
        return count.value

    def _compute_processes(
        self, device
    ) -> tuple[dict[int, int | None], bool, str | None]:
        if self._processes is None:
            return {}, False, "NVML compute-process v2 query is unavailable"
        capacity = self._process_capacity
        for _ in range(4):
            # NVML writes at most count entries. INSUFFICIENT_SIZE returns the
            # required count; grow a fresh correctly typed array, with a bound.
            count = C.c_uint(capacity)
            array = (_ProcessInfoV2 * capacity)()
            code = self._processes(device, C.byref(count), array)
            if code == INSUFFICIENT_SIZE:
                capacity = max(capacity * 2, count.value + 8)
                if capacity > 65536:
                    return {}, False, "NVML returned an unreasonable process count"
                continue
            if code != SUCCESS:
                return {}, False, self._error(code, "GPU process query")
            if count.value > capacity:
                return {}, False, "NVML process count exceeded its supplied buffer"
            self._process_capacity = capacity
            values = {}
            complete = True
            for item in array[: count.value]:
                pid, used = int(item.pid), int(item.usedGpuMemory)
                if used == UNAVAILABLE_MEMORY:
                    values[pid] = None
                    complete = False
                elif pid not in values:
                    values[pid] = used
                elif values[pid] is not None:
                    # One process may own contexts in several GPU instances.
                    values[pid] += used
            return (
                values,
                complete,
                None if complete else "Some per-process GPU memory is unavailable",
            )
        return (
            {},
            False,
            "GPU process list changed too quickly to obtain a complete sample",
        )

    def snapshot(self) -> dict | None:
        with self._lock:
            started = self._clock()
            # Frequent sampler ticks must not push a failed driver's retry time
            # forward forever. Only an actual retry may start a new backoff.
            if not self._initialized and started < self._retry_at:
                return None
            try:
                self._initialize()
                # Enumeration is cheap and refreshes handles after a device-count
                # change. Never silently keep monitoring only the first GPU.
                if self._device_count() != len(self._devices):
                    self.close()
                    self._initialize()
                devices = []
                for index, (device, name) in enumerate(self._devices):
                    devices.append(self._device_memory(index, device, name))

                # Bracket all cards with the same PID lifetime observations. A
                # process can allocate on several cards; sum bytes before MiB
                # rounding, but discard that PID if any context is unreadable.
                initial = [self._compute_processes(d) for d, _ in self._devices]
                pids = {pid for values, _, _ in initial for pid in values}
                identities_before = {pid: self._identity_reader(pid) for pid in pids}
                measured = [self._compute_processes(d) for d, _ in self._devices]
                byte_values, unavailable = {}, set()
                complete = all(valid for _, valid, _ in initial + measured)
                errors = [error for _, _, error in initial + measured if error]
                # A failed whole-card query could hide a contribution for any
                # PID. Do not show partial cross-card totals as exact readings.
                query_failed = any(
                    not valid and not values for values, valid, _ in initial + measured
                )
                for values, valid, error in measured:
                    for pid, byte_count in values.items():
                        if byte_count is None:
                            unavailable.add(pid)
                        else:
                            byte_values[pid] = byte_values.get(pid, 0) + byte_count
                values, identities = {}, {}
                if not query_failed:
                    for pid, byte_count in byte_values.items():
                        after = self._identity_reader(pid)
                        before = identities_before.get(pid)
                        if pid in unavailable or before is None or before != after:
                            complete = False
                            continue
                        values[pid] = byte_count // MIB
                        identities[pid] = before
                error = "; ".join(dict.fromkeys(errors)) or None
                if not complete and error is None:
                    error = (
                        "Some GPU process identities changed or could not be verified"
                    )
                for row, (_, valid, detail) in zip(devices, measured):
                    row.update(process_data_valid=valid, process_error=detail)
                self.last_error = error or ""
                reserved = [row["reserved"] for row in devices]
                return {
                    "name": self.name,
                    "device_count": len(devices),
                    "devices": devices,
                    "aggregation": "sum of detected physical NVIDIA devices; not pooled capacity",
                    "total": sum(row["total"] for row in devices),
                    "used": sum(row["used"] for row in devices),
                    "free": sum(row["free"] for row in devices),
                    "util": devices[0]["util"] if len(devices) == 1 else None,
                    "per_pid": values,
                    "pid_starttimes": identities,
                    "reserved": sum(reserved)
                    if all(v is not None for v in reserved)
                    else None,
                    "used_includes_reserved": any(
                        row["used_includes_reserved"] for row in devices
                    ),
                    "process_data_valid": complete,
                    "process_error": error,
                    "sampled_at_monotonic": started,
                    "query_duration_s": self._clock() - started,
                    "source": "NVML · compute contexts",
                }
            except (OSError, NvmlError, ValueError) as exc:
                self.last_error = str(exc)
                self._retry_at = self._clock() + 5.0
                self.close()
                return None

    def _device_memory(self, index, device, name):
        # Drivers may expose the v2 symbol but lack support on one card. Only
        # NOT_SUPPORTED permits using the documented v1 query for that card;
        # permission, GPU-loss and other failures stay unavailable.
        memory = _MemoryV2()
        memory.version = C.sizeof(_MemoryV2) | (2 << 24)
        code = (
            self._memory_v2(device, C.byref(memory))
            if self._memory_v2
            else NOT_SUPPORTED
        )
        if code == NOT_SUPPORTED:
            memory = _Memory()
            self._check(
                self._memory(device, C.byref(memory)), f"GPU {index} memory query"
            )
            reserved, includes_reserved = None, True
        else:
            self._check(code, f"GPU {index} memory v2 query")
            reserved, includes_reserved = int(memory.reserved // MIB), False
        if memory.total <= 0 or memory.used > memory.total:
            raise NvmlError(f"NVML returned invalid device {index} memory figures")
        utilization = _Utilization()
        util = (
            int(utilization.gpu)
            if self._util
            and self._util(device, C.byref(utilization)) == SUCCESS
            and utilization.gpu <= 100
            else None
        )
        return {
            "index": index,
            "name": name,
            "total": int(memory.total // MIB),
            "used": int(memory.used // MIB),
            "free": int(memory.free // MIB),
            "reserved": reserved,
            "used_includes_reserved": includes_reserved,
            "util": util,
        }

    def close(self):
        # A stuck driver call must not make application shutdown wait on this
        # Python lock. Process exit releases driver resources if it remains busy.
        if not self._lock.acquire(blocking=False):
            return False
        try:
            if self._initialized:
                self._initialized = False
                self._shutdown()
                self._devices = []
            return True
        finally:
            self._lock.release()


_singleton = None
_singleton_lock = threading.Lock()


def gpu_snapshot() -> dict | None:
    """Drop-in sampler function; failed NVML does not start costly subprocesses."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = NVMLReader()
        reader = _singleton
    return reader.snapshot()


def gpu_status() -> str:
    with _singleton_lock:
        return _singleton.last_error if _singleton else "Not sampled"


def close_gpu_reader():
    global _singleton
    with _singleton_lock:
        reader, _singleton = _singleton, None
    if reader is not None:
        reader.close()


atexit.register(close_gpu_reader)
