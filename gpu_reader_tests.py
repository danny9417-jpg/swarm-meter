"""Injected NVML ABI/error tests: no driver or process signals are used."""

import ctypes as C
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gpu_reader as g


class Function:
    # Match ctypes function attributes while keeping every C call inspectable.
    def __init__(self, callback):
        self.callback = callback
        self.calls = 0

    def __call__(self, *args):
        self.calls += 1
        return self.callback(*args)


class Driver:
    def __init__(self, *, rows=None, v2=True, compute=True):
        self.rows = [] if rows is None else rows
        self.init_code = self.handle_code = self.memory_code = self.util_code = 0
        self.total = 24 * 1024 * g.MIB
        self.used = 31 * g.MIB
        self.reserved = 480 * g.MIB
        self.seen_versions = []
        self.seen_capacities = []
        self.compute_results = []
        self.nvmlInit_v2 = Function(lambda: self.init_code)
        self.nvmlShutdown = Function(lambda: 0)
        self.nvmlErrorString = Function(lambda code: b"Injected driver error")
        self.device_count = 1
        self.nvmlDeviceGetCount_v2 = Function(self.count)
        self.nvmlDeviceGetHandleByIndex_v2 = Function(self.handle)
        self.nvmlDeviceGetMemoryInfo = Function(self.memory)
        if v2:
            self.nvmlDeviceGetMemoryInfo_v2 = Function(self.memory_v2)
        self.nvmlDeviceGetUtilizationRates = Function(self.utilization)
        if compute:
            self.nvmlDeviceGetComputeRunningProcesses_v2 = Function(self.processes)

    def count(self, pointer):
        C.cast(pointer, C.POINTER(C.c_uint))[0] = self.device_count
        return 0

    def handle(self, index, pointer):
        assert index == 0
        C.cast(pointer, C.POINTER(C.c_void_p))[0] = C.c_void_p(17)
        return self.handle_code

    def memory(self, device, pointer):
        memory = C.cast(pointer, C.POINTER(g._Memory)).contents
        memory.total, memory.used = self.total, self.used + self.reserved
        memory.free = self.total - memory.used
        return self.memory_code

    def memory_v2(self, device, pointer):
        memory = C.cast(pointer, C.POINTER(g._MemoryV2)).contents
        self.seen_versions.append(memory.version)
        memory.total, memory.used, memory.reserved = (
            self.total,
            self.used,
            self.reserved,
        )
        memory.free = self.total - self.used - self.reserved
        return self.memory_code

    def utilization(self, device, pointer):
        C.cast(pointer, C.POINTER(g._Utilization)).contents.gpu = 42
        return self.util_code

    def processes(self, device, count_pointer, array):
        count = C.cast(count_pointer, C.POINTER(C.c_uint))
        capacity = count.contents.value
        self.seen_capacities.append(capacity)
        if self.compute_results:
            code, rows = self.compute_results.pop(0)
        else:
            code, rows = 0, self.rows
        if code == g.INSUFFICIENT_SIZE or len(rows) > capacity:
            count.contents.value = len(rows)
            return g.INSUFFICIENT_SIZE
        if code:
            return code
        count.contents.value = len(rows)
        for index, (pid, used) in enumerate(rows):
            array[index].pid = pid
            array[index].usedGpuMemory = used
        return 0


class MultiDriver(Driver):
    """Route opaque fake handles to independent cards, including failures."""

    def __init__(self, *devices):
        self.devices = list(devices)
        super().__init__()
        self.nvmlDeviceGetName = Function(self.name)

    def count(self, pointer):
        C.cast(pointer, C.POINTER(C.c_uint))[0] = len(self.devices)
        return 0

    def handle(self, index, pointer):
        C.cast(pointer, C.POINTER(C.c_void_p))[0] = C.c_void_p(17 + index)
        return self.devices[index].handle_code

    def selected(self, handle):
        return self.devices[handle.value - 17]

    def name(self, device, pointer, length):
        value = f"Fake GPU {device.value - 17}".encode()
        C.memmove(pointer, value + b"\0", len(value) + 1)
        return 0

    def memory(self, device, pointer):
        return self.selected(device).memory(device, pointer)

    def memory_v2(self, device, pointer):
        return self.selected(device).memory_v2(device, pointer)

    def utilization(self, device, pointer):
        return self.selected(device).utilization(device, pointer)

    def processes(self, device, count_pointer, array):
        return self.selected(device).processes(device, count_pointer, array)


class NVMLTests(unittest.TestCase):
    def reader(self, driver=None, **kwargs):
        driver = driver or Driver()
        reader = g.NVMLReader(library=driver, **kwargs)
        self.addCleanup(reader.close)
        return reader, driver

    def test_native_abi_sizes_offsets_and_version(self):
        self.assertEqual(C.sizeof(g._Memory), 24)
        self.assertEqual(C.sizeof(g._MemoryV2), 40)
        self.assertEqual(C.sizeof(g._ProcessInfoV2), 24)
        self.assertEqual(
            [
                getattr(g._MemoryV2, name).offset
                for name in ("version", "total", "reserved", "free", "used")
            ],
            [0, 8, 16, 24, 32],
        )
        self.assertEqual(
            [
                getattr(g._ProcessInfoV2, name).offset
                for name in (
                    "pid",
                    "usedGpuMemory",
                    "gpuInstanceId",
                    "computeInstanceId",
                )
            ],
            [0, 8, 16, 20],
        )
        reader, driver = self.reader()
        snapshot = reader.snapshot()
        self.assertEqual(driver.seen_versions, [40 | (2 << 24)])
        self.assertEqual(snapshot["used"], 31)
        self.assertEqual(snapshot["reserved"], 480)
        self.assertFalse(snapshot["used_includes_reserved"])
        function = driver.nvmlDeviceGetComputeRunningProcesses_v2
        self.assertEqual(
            function.argtypes,
            [C.c_void_p, C.POINTER(C.c_uint), C.POINTER(g._ProcessInfoV2)],
        )
        self.assertIs(function.restype, C.c_int)

    def test_connection_initialized_once_and_shutdown_balanced(self):
        reader, driver = self.reader()
        self.assertTrue(reader.snapshot()["process_data_valid"])
        self.assertEqual(reader.snapshot()["per_pid"], {})
        reader.close()
        reader.close()
        self.assertEqual(driver.nvmlInit_v2.calls, 1)
        self.assertEqual(driver.nvmlShutdown.calls, 1)

    def test_stable_identity_has_memory_and_starttime(self):
        reader, _ = self.reader(
            Driver(rows=[(73, 3 * g.MIB)]), identity_reader=lambda pid: 567
        )
        snapshot = reader.snapshot()
        self.assertEqual(snapshot["per_pid"], {73: 3})
        self.assertEqual(snapshot["pid_starttimes"], {73: 567})
        self.assertTrue(snapshot["process_data_valid"])

    def test_reused_pid_cannot_inherit_gpu_memory(self):
        identities = iter([567, 568])
        reader, _ = self.reader(
            Driver(rows=[(73, 3 * g.MIB)]), identity_reader=lambda pid: next(identities)
        )
        snapshot = reader.snapshot()
        self.assertEqual(snapshot["per_pid"], {})
        self.assertEqual(snapshot["pid_starttimes"], {})
        self.assertFalse(snapshot["process_data_valid"])

    def test_new_pid_between_queries_stays_unattributed(self):
        driver = Driver()
        driver.compute_results = [(0, []), (0, [(73, g.MIB)])]
        reader, _ = self.reader(driver, identity_reader=lambda pid: 567)
        snapshot = reader.snapshot()
        self.assertEqual(snapshot["per_pid"], {})
        self.assertFalse(snapshot["process_data_valid"])

    def test_unreadable_identity_is_not_zero(self):
        reader, _ = self.reader(
            Driver(rows=[(73, 3 * g.MIB)]), identity_reader=lambda pid: None
        )
        snapshot = reader.snapshot()
        self.assertEqual(snapshot["per_pid"], {})
        self.assertFalse(snapshot["process_data_valid"])

    def test_insufficient_size_reallocates_and_remembers_capacity(self):
        rows = [(pid, g.MIB) for pid in range(100, 140)]
        reader, driver = self.reader(
            Driver(rows=rows), identity_reader=lambda pid: pid * 10
        )
        snapshot = reader.snapshot()
        self.assertTrue(snapshot["process_data_valid"])
        self.assertEqual(len(snapshot["per_pid"]), 40)
        self.assertEqual(driver.seen_capacities, [16, 48, 48])

    def test_unavailable_gpu_memory_sentinel_is_not_a_huge_value(self):
        reader, _ = self.reader(
            Driver(rows=[(73, g.UNAVAILABLE_MEMORY)]), identity_reader=lambda pid: 567
        )
        snapshot = reader.snapshot()
        self.assertEqual(snapshot["per_pid"], {})
        self.assertFalse(snapshot["process_data_valid"])
        self.assertIn("unavailable", snapshot["process_error"])

    def test_duplicate_instance_contexts_sum(self):
        reader, _ = self.reader(
            Driver(rows=[(73, g.MIB), (73, 2 * g.MIB)]), identity_reader=lambda pid: 567
        )
        self.assertEqual(reader.snapshot()["per_pid"], {73: 3})

    def test_unavailable_context_does_not_turn_into_partial_total(self):
        for rows in (
            [(73, g.UNAVAILABLE_MEMORY), (73, g.MIB)],
            [(73, g.MIB), (73, g.UNAVAILABLE_MEMORY)],
        ):
            reader, _ = self.reader(Driver(rows=rows), identity_reader=lambda pid: 567)
            self.assertEqual(reader.snapshot()["per_pid"], {})

    def test_unsupported_process_query_preserves_memory_but_marks_unknown(self):
        driver = Driver()
        driver.compute_results = [(g.NOT_SUPPORTED, []), (g.NOT_SUPPORTED, [])]
        reader, _ = self.reader(driver)
        snapshot = reader.snapshot()
        self.assertEqual(snapshot["used"], 31)
        self.assertFalse(snapshot["process_data_valid"])
        self.assertTrue(snapshot["process_error"])

    def test_missing_process_symbol_is_unknown(self):
        reader, _ = self.reader(Driver(compute=False))
        self.assertFalse(reader.snapshot()["process_data_valid"])

    def test_unsupported_utilization_is_none(self):
        driver = Driver()
        driver.util_code = g.NOT_SUPPORTED
        reader, _ = self.reader(driver)
        self.assertIsNone(reader.snapshot()["util"])

    def test_old_memory_api_explicitly_includes_reserved(self):
        reader, _ = self.reader(Driver(v2=False))
        snapshot = reader.snapshot()
        self.assertEqual(snapshot["used"], 511)
        self.assertIsNone(snapshot["reserved"])
        self.assertTrue(snapshot["used_includes_reserved"])

    def test_retry_backoff_is_not_extended_by_intervening_ticks(self):
        clock = [100.0]
        driver = Driver()
        driver.init_code = 9
        reader, _ = self.reader(driver, clock=lambda: clock[0])
        self.assertIsNone(reader.snapshot())
        self.assertEqual(reader._retry_at, 105.0)
        for second in range(101, 105):
            clock[0] = second
            self.assertIsNone(reader.snapshot())
            self.assertEqual(reader._retry_at, 105.0)
        self.assertEqual(driver.nvmlInit_v2.calls, 1)
        driver.init_code = 0
        clock[0] = 105.0
        self.assertIsNotNone(reader.snapshot())
        self.assertEqual(driver.nvmlInit_v2.calls, 2)
        self.assertEqual(driver.nvmlShutdown.calls, 0)

    def test_handle_failure_balances_successful_initialization(self):
        driver = Driver()
        driver.handle_code = 9
        reader, _ = self.reader(driver)
        self.assertIsNone(reader.snapshot())
        self.assertEqual(driver.nvmlShutdown.calls, 1)

    def test_invalid_device_memory_is_unknown(self):
        for total, used in [(0, 0), (g.MIB, 2 * g.MIB)]:
            driver = Driver()
            driver.total, driver.used = total, used
            reader, _ = self.reader(driver)
            self.assertIsNone(reader.snapshot())

    def test_timestamp_covers_initialization_and_query_time(self):
        clock = [100.0]
        driver = Driver()
        driver.nvmlInit_v2 = Function(lambda: clock.__setitem__(0, 103.0) or 0)
        reader, _ = self.reader(driver, clock=lambda: clock[0])
        snapshot = reader.snapshot()
        self.assertEqual(snapshot["sampled_at_monotonic"], 100.0)
        self.assertEqual(snapshot["query_duration_s"], 3.0)

    def test_multiple_cards_sum_memory_and_same_pid_without_pooling_claim(self):
        first = Driver(rows=[(73, 3 * g.MIB + g.MIB // 2)])
        second = Driver(rows=[(73, g.MIB // 2), (74, 2 * g.MIB)])
        second.total, second.used = 8 * 1024 * g.MIB, 3 * g.MIB
        reader, driver = self.reader(
            MultiDriver(first, second), identity_reader=lambda pid: pid
        )
        snapshot = reader.snapshot()
        self.assertEqual(snapshot["device_count"], 2)
        self.assertEqual(snapshot["total"], 32 * 1024)
        self.assertEqual(snapshot["used"], 34)
        self.assertEqual(snapshot["per_pid"], {73: 4, 74: 2})
        self.assertEqual(snapshot["pid_starttimes"], {73: 73, 74: 74})
        self.assertEqual(
            [d["name"] for d in snapshot["devices"]], ["Fake GPU 0", "Fake GPU 1"]
        )
        self.assertIsNone(snapshot["util"])
        self.assertEqual([d["util"] for d in snapshot["devices"]], [42, 42])
        self.assertIn("not pooled", snapshot["aggregation"])
        self.assertTrue(snapshot["process_data_valid"])
        reader.snapshot()
        self.assertEqual(driver.nvmlInit_v2.calls, 1)

    def test_missing_nvml_library_is_unavailable(self):
        reader = g.NVMLReader()
        self.addCleanup(reader.close)
        with mock.patch.object(g.C, "CDLL", side_effect=OSError("No NVML driver")):
            self.assertIsNone(reader.snapshot())
        self.assertIn("No NVML", reader.last_error)

    def test_no_nvidia_devices_is_unavailable_not_zero(self):
        reader, driver = self.reader(MultiDriver())
        self.assertIsNone(reader.snapshot())
        self.assertIn("No NVIDIA", reader.last_error)
        self.assertEqual(driver.nvmlShutdown.calls, 1)

    def test_device_count_change_refreshes_handles(self):
        driver = MultiDriver(Driver())
        reader, _ = self.reader(driver)
        self.assertEqual(reader.snapshot()["device_count"], 1)
        driver.devices.append(Driver())
        self.assertEqual(reader.snapshot()["device_count"], 2)
        self.assertEqual(driver.nvmlInit_v2.calls, 2)
        self.assertEqual(driver.nvmlShutdown.calls, 1)

    def test_failure_on_second_device_does_not_report_partial_memory(self):
        for operation in ("handle_code", "memory_code"):
            second = Driver()
            setattr(second, operation, 9)
            reader, _ = self.reader(MultiDriver(Driver(), second))
            self.assertIsNone(reader.snapshot())

    def test_failed_card_process_query_hides_partial_cross_card_pid_totals(self):
        first, second = Driver(rows=[(73, g.MIB)]), Driver()
        second.compute_results = [(g.NOT_SUPPORTED, []), (g.NOT_SUPPORTED, [])]
        reader, _ = self.reader(
            MultiDriver(first, second), identity_reader=lambda pid: 567
        )
        snapshot = reader.snapshot()
        self.assertEqual(snapshot["used"], 62)
        self.assertEqual(snapshot["per_pid"], {})
        self.assertFalse(snapshot["process_data_valid"])
        self.assertTrue(snapshot["process_error"])

    def test_unknown_cross_card_context_invalidates_only_affected_pid(self):
        first = Driver(rows=[(73, g.MIB), (74, 2 * g.MIB)])
        second = Driver(rows=[(73, g.UNAVAILABLE_MEMORY)])
        reader, _ = self.reader(
            MultiDriver(first, second), identity_reader=lambda pid: pid
        )
        snapshot = reader.snapshot()
        self.assertEqual(snapshot["per_pid"], {74: 2})
        self.assertFalse(snapshot["process_data_valid"])

    def test_pid_reuse_across_devices_cannot_inherit_memory(self):
        first, second = Driver(rows=[(73, g.MIB)]), Driver(rows=[(73, 2 * g.MIB)])
        identities = iter([567, 568])
        reader, _ = self.reader(
            MultiDriver(first, second), identity_reader=lambda pid: next(identities)
        )
        snapshot = reader.snapshot()
        self.assertEqual(snapshot["per_pid"], {})
        self.assertFalse(snapshot["process_data_valid"])

    def test_memory_v2_unsupported_uses_explicit_old_api_semantics(self):
        driver = Driver()
        driver.nvmlDeviceGetMemoryInfo_v2 = Function(lambda *args: g.NOT_SUPPORTED)
        reader, _ = self.reader(driver)
        snapshot = reader.snapshot()
        self.assertEqual(snapshot["used"], 511)
        self.assertIsNone(snapshot["reserved"])
        self.assertTrue(snapshot["used_includes_reserved"])
        self.assertEqual(driver.nvmlDeviceGetMemoryInfo.calls, 1)

    def test_memory_v2_failure_is_not_hidden_by_old_api(self):
        driver = Driver()
        driver.nvmlDeviceGetMemoryInfo_v2 = Function(lambda *args: 9)
        reader, _ = self.reader(driver)
        self.assertIsNone(reader.snapshot())
        self.assertEqual(driver.nvmlDeviceGetMemoryInfo.calls, 0)

    def test_unreasonable_device_count_is_rejected_before_enumeration(self):
        driver = Driver()
        driver.device_count = 1025
        reader, _ = self.reader(driver)
        self.assertIsNone(reader.snapshot())
        self.assertEqual(driver.nvmlDeviceGetHandleByIndex_v2.calls, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
