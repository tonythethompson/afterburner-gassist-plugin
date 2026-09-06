"""AfterburnerMonitoringClient / MAHM parser unit tests (plan task 18.1, 18.4 runtime side).

All tests drive the pure `parse_mahm(bytes)` validator/parser with injected buffers — no real
Afterburner or GPU required. The OS mapping layer is exercised only by the opt-in integration
test (task 18.2); here we prove every invalid-header case returns a typed `PluginError` and
never crashes, plus the read-only (no `FILE_MAP_WRITE`) structural guarantee.
"""
from __future__ import annotations

import ctypes
import math
import struct
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from afterburner.integration import mahm
from afterburner.models import ErrorCode, GpuTelemetry, PluginError

H = mahm.MAHM_SHARED_MEMORY_HEADER
E = mahm.MAHM_SHARED_MEMORY_ENTRY
G = mahm.MAHM_SHARED_MEMORY_GPU_ENTRY


def _header(num_entries: int = 0, num_gpu: int = 0) -> bytes:
    h = H(
        dwSignature=mahm.MAHM_SIGNATURE,
        dwVersion=mahm.MAHM_VERSION,
        dwHeaderSize=ctypes.sizeof(H),
        dwNumEntries=num_entries,
        dwEntrySize=ctypes.sizeof(E),
        time=12345,
        dwNumGpuEntries=num_gpu,
        dwGpuEntrySize=ctypes.sizeof(G),
    )
    return ctypes.string_at(ctypes.byref(h), ctypes.sizeof(H))


def _entry(src_id: int, gpu: int, data: float, name: bytes = b"src") -> bytes:
    e = E()
    e.szSrcName = name.ljust(260, b"\x00")
    e.szSrcUnits = b"\x00" * 260
    e.data = data
    e.dwFlags = 1
    e.dwGpu = gpu
    e.dwSrcId = src_id
    return ctypes.string_at(ctypes.byref(e), ctypes.sizeof(E))


def _gpu_entry(
    *,
    gpu_id: bytes = b"VEN_10DE&DEV_2684&SUBSYS_00000000&REV_00&BUS_1&DEV_0&FN_0",
    device: bytes = b"GeForce RTX 4090",
    driver: bytes = b"31.0.15.3713",
    mem_kb: int = 24 * 1024 * 1024,
) -> bytes:
    g = G()
    g.szGpuId = gpu_id.ljust(260, b"\x00")
    g.szFamily = b"\x00" * 260
    g.szDevice = device.ljust(260, b"\x00")
    g.szDriver = driver.ljust(260, b"\x00")
    g.szBIOS = b"\x00" * 260
    g.dwMemAmount = mem_kb
    return ctypes.string_at(ctypes.byref(g), ctypes.sizeof(G))


# Sample sources: (src_id, gpu, value) covering every telemetry field.
_FULL_SOURCES = [
    (0x00, 62.0),   # GPU temperature (C)
    (0x30, 97.0),   # GPU usage (%)
    (0x20, 2715.0),  # core clock (MHz)
    (0x22, 10501.0),  # memory clock (MHz)
    (0x40, 1043.0),  # GPU voltage (mV)
    (0x61, 350.0),  # GPU power (W)
    (0x71, 100.0),  # power limit (%)
    (0x10, 55.0),   # fan speed (%)
    (0x11, 1800.0),  # fan tachometer (RPM)
    (0x31, 5120.0),  # memory usage (MB)
]

FLT_MAX = 3.4028234663852886e38


def _full_buffer() -> bytes:
    entries = b"".join(_entry(sid, 0, v) for sid, v in _FULL_SOURCES)
    entries += _entry(0xFFFFFFFF, mahm.MAHM_SOURCE_ID_GLOBAL, 144.0)  # global framerate
    return _header(num_entries=len(_FULL_SOURCES) + 1, num_gpu=1) + entries + _gpu_entry()


def _with_signature(buf: bytes, value: int) -> bytes:
    return buf[:0] + struct.pack("<I", value) + buf[4:]


# ---------------------------------------------------------------------------
# Validation error paths (task 18.4 runtime side)
# ---------------------------------------------------------------------------


class TestRuntimeValidation:
    def test_deallocated_map_0xdead_is_not_running(self) -> None:
        with pytest.raises(PluginError) as excinfo:
            mahm.parse_mahm(_with_signature(_header(), mahm.MAHM_DEAD_SIGNATURE))
        assert excinfo.value.code is ErrorCode.NOT_RUNNING

    def test_wrong_signature_is_interface_unavailable(self) -> None:
        with pytest.raises(PluginError) as excinfo:
            mahm.parse_mahm(_with_signature(_header(), 0x11223344))
        assert excinfo.value.code is ErrorCode.INTERFACE_UNAVAILABLE

    def test_unsupported_version_rejected(self) -> None:
        buf = _header()
        buf = buf[:4] + struct.pack("<I", 0x00020001) + buf[8:]
        with pytest.raises(PluginError) as excinfo:
            mahm.parse_mahm(buf)
        assert excinfo.value.code is ErrorCode.UNSUPPORTED_VERSION

    def test_region_smaller_than_header_rejected(self) -> None:
        with pytest.raises(PluginError) as excinfo:
            mahm.parse_mahm(b"\x00" * (ctypes.sizeof(H) - 1))
        assert excinfo.value.code is ErrorCode.INTERFACE_UNAVAILABLE

    def test_zero_entry_size_with_entries_present_rejected(self) -> None:
        buf = _header(num_entries=1)
        # dwEntrySize is field index 4 (offset 16).
        buf = buf[:16] + struct.pack("<I", 0) + buf[20:]
        with pytest.raises(PluginError) as excinfo:
            mahm.parse_mahm(buf + _entry(0x00, 0, 1.0))
        assert excinfo.value.code is ErrorCode.INTERFACE_UNAVAILABLE

    def test_zero_gpu_entry_size_with_gpus_present_rejected(self) -> None:
        buf = _header(num_gpu=1)
        # dwGpuEntrySize is the last field (offset 28).
        buf = buf[:28] + struct.pack("<I", 0)
        with pytest.raises(PluginError) as excinfo:
            mahm.parse_mahm(buf + _gpu_entry())
        assert excinfo.value.code is ErrorCode.INTERFACE_UNAVAILABLE

    def test_oversized_entry_size_is_unsupported_version(self) -> None:
        buf = _header(num_entries=1)
        buf = buf[:16] + struct.pack("<I", ctypes.sizeof(E) + 8) + buf[20:]
        with pytest.raises(PluginError) as excinfo:
            mahm.parse_mahm(buf + _entry(0x00, 0, 1.0))
        assert excinfo.value.code is ErrorCode.UNSUPPORTED_VERSION
        assert "binding size" in excinfo.value.detail

    def test_oversized_gpu_entry_size_is_unsupported_version(self) -> None:
        buf = _header(num_gpu=1)
        buf = buf[:28] + struct.pack("<I", ctypes.sizeof(G) + 8)
        with pytest.raises(PluginError) as excinfo:
            mahm.parse_mahm(buf + _gpu_entry())
        assert excinfo.value.code is ErrorCode.UNSUPPORTED_VERSION

    def test_declared_region_exceeding_mapped_size_rejected(self) -> None:
        header = _header(num_entries=1, num_gpu=1)
        with pytest.raises(PluginError) as excinfo:
            mahm.parse_mahm(header)  # truncated: no entries follow
        assert excinfo.value.code is ErrorCode.INTERFACE_UNAVAILABLE
        assert "exceeds mapped size" in excinfo.value.detail

    def test_implausible_entry_count_rejected(self) -> None:
        buf = _header(num_entries=mahm._MAX_ENTRIES + 1)
        with pytest.raises(PluginError) as excinfo:
            mahm.parse_mahm(buf)
        assert excinfo.value.code is ErrorCode.INTERFACE_UNAVAILABLE

    def test_header_size_below_struct_rejected(self) -> None:
        buf = _header(num_entries=1)
        buf = buf[:8] + struct.pack("<I", ctypes.sizeof(H) - 4) + buf[12:]
        with pytest.raises(PluginError) as excinfo:
            mahm.parse_mahm(buf + _entry(0x00, 0, 1.0))
        assert excinfo.value.code is ErrorCode.INTERFACE_UNAVAILABLE

    @given(st.binary(min_size=0, max_size=4096))
    @settings(max_examples=100)
    def test_adversarial_buffers_never_crash(self, blob: bytes) -> None:
        try:
            snapshot = mahm.parse_mahm(blob)
            assert snapshot.gpus is not None
        except PluginError:
            pass  # every malformed header must surface as a typed error, never a crash


# ---------------------------------------------------------------------------
# Well-formed parsing + telemetry mapping
# ---------------------------------------------------------------------------


class TestParsingAndTelemetry:
    def test_parses_full_sample_into_typed_telemetry(self) -> None:
        snapshot = mahm.parse_mahm(_full_buffer())
        assert snapshot.version == mahm.MAHM_VERSION
        assert snapshot.header_size == ctypes.sizeof(H)
        assert len(snapshot.gpus) == 1
        assert snapshot.gpus[0].gpu_id.startswith("VEN_10DE&")
        assert len(snapshot.sources) == len(_FULL_SOURCES) + 1

        telemetry = mahm.telemetry_from_snapshot(snapshot, 0)
        assert isinstance(telemetry, GpuTelemetry)
        assert telemetry.gpu_index == 0
        assert telemetry.gpu_name == "GeForce RTX 4090"
        assert telemetry.driver_version == "31.0.15.3713"
        assert telemetry.temperature_c == 62.0
        assert telemetry.utilization_pct == 97.0
        assert telemetry.core_clock_mhz == 2715.0
        assert telemetry.memory_clock_mhz == 10501.0
        assert telemetry.voltage_mv == 1043.0
        assert telemetry.power_watts == 350.0
        assert telemetry.power_limit_pct == 100.0
        assert telemetry.fan_percent == 55.0
        assert telemetry.fan_rpm == 1800.0
        assert telemetry.memory_used_mb == 5120.0
        assert telemetry.memory_total_mb == 24576.0  # dwMemAmount KB -> MB

    def test_missing_sources_stay_none(self) -> None:
        buf = _header(num_entries=1, num_gpu=1) + _entry(0x00, 0, 50.0) + _gpu_entry()
        telemetry = mahm.telemetry_from_snapshot(mahm.parse_mahm(buf), 0)
        assert telemetry.temperature_c == 50.0
        assert telemetry.fan_percent is None
        assert telemetry.power_watts is None
        assert telemetry.memory_used_mb is None

    def test_flt_max_value_means_not_available(self) -> None:
        # Header documents: data is set to FLT_MAX when not available.
        buf = _header(num_entries=1, num_gpu=1) + _entry(0x10, 0, FLT_MAX) + _gpu_entry()
        telemetry = mahm.telemetry_from_snapshot(mahm.parse_mahm(buf), 0)
        assert telemetry.fan_percent is None
        buf = _header(num_entries=1, num_gpu=1) + _entry(0x00, 0, FLT_MAX) + _gpu_entry()
        telemetry = mahm.telemetry_from_snapshot(mahm.parse_mahm(buf), 0)
        assert telemetry.temperature_c is None

    def test_fan_fallback_source_ids(self) -> None:
        # Only FAN_SPEED2 (0x12) present: mapping falls back to it.
        buf = (
            _header(num_entries=1, num_gpu=1)
            + _entry(0x12, 0, 77.0)
            + _gpu_entry()
        )
        telemetry = mahm.telemetry_from_snapshot(mahm.parse_mahm(buf), 0)
        assert telemetry.fan_percent == 77.0

    def test_second_gpu_entry_parses_by_index(self) -> None:
        gpu_a = _gpu_entry(device=b"GeForce RTX 4090", mem_kb=24 * 1024 * 1024)
        gpu_b = _gpu_entry(
            device=b"GeForce RTX 4080",
            driver=b"31.0.15.3713",
            mem_kb=16 * 1024 * 1024,
            gpu_id=b"VEN_10DE&DEV_2705&SUBSYS_00000000&REV_00&BUS_2&DEV_0&FN_0",
        )
        buf = _header(num_entries=2, num_gpu=2)
        buf += _entry(0x00, 0, 62.0) + _entry(0x00, 1, 58.0) + gpu_a + gpu_b
        snapshot = mahm.parse_mahm(buf)
        assert len(snapshot.gpus) == 2
        t0 = mahm.telemetry_from_snapshot(snapshot, 0)
        t1 = mahm.telemetry_from_snapshot(snapshot, 1)
        assert t0.gpu_name == "GeForce RTX 4090"
        assert t1.gpu_name == "GeForce RTX 4080"
        assert t0.temperature_c == 62.0
        assert t1.temperature_c == 58.0
        assert t1.memory_total_mb == 16384.0

    def test_out_of_range_gpu_raises_typed_error(self) -> None:
        snapshot = mahm.parse_mahm(_full_buffer())
        with pytest.raises(PluginError) as excinfo:
            mahm.telemetry_from_snapshot(snapshot, 3)
        assert excinfo.value.code is ErrorCode.UNSUPPORTED_GPU

    def test_control_operations_degrade_typed(self) -> None:
        client = mahm.AfterburnerMonitoringClient(install_dir="")
        for op in (
            lambda: client.read_capabilities(0),
            lambda: client.read_tuning_state(0),
            lambda: client.list_profiles(),
            lambda: client.load_profile(1),
            lambda: client.reset_tuning(0),
            lambda: client.apply_control(0, None, 1.0),  # type: ignore[arg-type]
            lambda: client.apply_fan_curve(0, None),  # type: ignore[arg-type]
        ):
            with pytest.raises(PluginError) as excinfo:
                op()
            assert excinfo.value.code is ErrorCode.INTERFACE_UNAVAILABLE
            assert excinfo.value.detail  # honest reason recorded


# ---------------------------------------------------------------------------
# Read-only structural guarantee (no FILE_MAP_WRITE anywhere)
# ---------------------------------------------------------------------------


class TestReadOnlyGuarantee:
    def test_module_never_requests_write_access(self) -> None:
        source = Path(mahm.__file__).read_text(encoding="utf-8")
        assert "FILE_MAP_WRITE" not in source
        assert "FILE_MAP_ALL_ACCESS" not in source
        assert "FILE_MAP_READ" in source
        assert "OpenFileMappingW" in source
        # No other mapping primitives appear (defensive).
        assert "CreateFileMappingW" not in source

    def test_view_open_uses_read_only_access_constant(self) -> None:
        assert mahm.FILE_MAP_READ == 0x0004
