"""AfterburnerControlClient pure-layer unit tests (plan task 20.1/20.3 runtime side).

Every control transaction runs first against an injected mutable byte buffer (the pure
`*_to_region` functions), so all validation, capability gating, unit conversion, no-op, and
reset branches are proven without touching a real GPU — including the guarantee that **no
write is attempted before the signature/version/layout checks pass**.
"""
from __future__ import annotations

import ctypes
import struct

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from afterburner.integration import macm
from afterburner.models import ControlFeature, ErrorCode, InterfaceStatus, PluginError

H = macm.MACM_SHARED_MEMORY_HEADER
G = macm.MACM_SHARED_MEMORY_GPU_ENTRY
ALL_FLAGS = 0xFFFFFFFF


def _make_region(
    *,
    version: int = macm.MACM_VERSION,
    signature: int = macm.MACM_SIGNATURE,
    gpu_flags: int = ALL_FLAGS,
    num_gpu: int = 1,
    power_cur: int = 100,
    power_min: int = 50,
    power_max: int = 120,
    power_def: int = 100,
    boost_min: int = -300000,
    boost_max: int = 300000,
    fan_cur: int = 30,
    fan_flags_cur: int = macm.MACM_SHARED_MEMORY_GPU_ENTRY_FAN_FLAG_AUTO,
    fan_min: int = 0,
    fan_max: int = 100,
    fan_def: int = 50,
    fan_flags_def: int = macm.MACM_SHARED_MEMORY_GPU_ENTRY_FAN_FLAG_AUTO,
    gpu_id: bytes = b"VEN_10DE&DEV_2684&SUBSYS_00000000&REV_00&BUS_1&DEV_0&FN_0",
) -> bytearray:
    header = H(
        dwSignature=signature,
        dwVersion=version,
        dwHeaderSize=ctypes.sizeof(H),
        dwNumGpuEntries=num_gpu,
        dwGpuEntrySize=ctypes.sizeof(G),
        dwMasterGpu=0,
        dwFlags=macm.MACM_SHARED_MEMORY_FLAG_SYNC,
        time=0,
        dwCommand=0,
    )
    entry = G(dwFlags=gpu_flags)
    entry.dwPowerLimitCur, entry.dwPowerLimitMin = power_cur, power_min
    entry.dwPowerLimitMax, entry.dwPowerLimitDef = power_max, power_def
    entry.dwCoreClockBoostCur = 0
    entry.dwCoreClockBoostMin, entry.dwCoreClockBoostMax, entry.dwCoreClockBoostDef = (
        boost_min,
        boost_max,
        0,
    )
    entry.dwMemoryClockBoostCur = 0
    entry.dwMemoryClockBoostMin, entry.dwMemoryClockBoostMax, entry.dwMemoryClockBoostDef = (
        boost_min,
        boost_max,
        0,
    )
    entry.dwCoreVoltageCur, entry.dwCoreVoltageDef = 0, 0
    entry.dwCoreVoltageMin, entry.dwCoreVoltageMax = 0, 1100
    entry.dwFanSpeedCur, entry.dwFanFlagsCur = fan_cur, fan_flags_cur
    entry.dwFanSpeedMin, entry.dwFanSpeedMax, entry.dwFanSpeedDef = fan_min, fan_max, fan_def
    entry.dwFanFlagsDef = fan_flags_def
    entry.szGpuId = gpu_id.ljust(260, b"\x00")
    return bytearray(
        ctypes.string_at(ctypes.byref(header), ctypes.sizeof(H))
        + ctypes.string_at(ctypes.byref(entry), ctypes.sizeof(G))
    )


def _entry(region: bytearray) -> G:
    offset = ctypes.sizeof(H) if macm.validate_control_region(bytes(region)).header_size else 0
    return G.from_buffer(region, offset)


def _cmd(region: bytearray) -> int:
    return macm.MACM_SHARED_MEMORY_HEADER.from_buffer(region).dwCommand


# ---------------------------------------------------------------------------
# Runtime validation (task 20.3 runtime side) — typed errors, no writes
# ---------------------------------------------------------------------------


class TestRuntimeValidation:
    def test_deallocated_map_raises_disconnected(self) -> None:
        with pytest.raises(PluginError) as excinfo:
            macm.validate_control_region(bytes(_make_region(signature=macm.MACM_DEAD_SIGNATURE)))
        assert excinfo.value.code is ErrorCode.DISCONNECTED

    def test_wrong_signature_raises_interface_unavailable(self) -> None:
        with pytest.raises(PluginError) as excinfo:
            macm.validate_control_region(bytes(_make_region(signature=0x11223344)))
        assert excinfo.value.code is ErrorCode.INTERFACE_UNAVAILABLE

    def test_unsupported_version_rejected(self) -> None:
        for version in (0x00010000, 0x00030000, 0x00020004 + 0x10000):
            with pytest.raises(PluginError) as excinfo:
                macm.validate_control_region(bytes(_make_region(version=version)))
            assert excinfo.value.code is ErrorCode.UNSUPPORTED_VERSION

    def test_v2_1_and_v2_3_minor_versions_accepted(self) -> None:
        # Afterburner 4.6.7 runs the map at 0x00020003 (v2.3) — verified live.
        for version in (0x00020000, 0x00020001, 0x00020003):
            parsed = macm.validate_control_region(bytes(_make_region(version=version)))
            assert parsed.version == version

    def test_region_smaller_than_header_rejected(self) -> None:
        with pytest.raises(PluginError) as excinfo:
            macm.validate_control_region(b"\x00" * (ctypes.sizeof(H) - 1))
        assert excinfo.value.code is ErrorCode.INTERFACE_UNAVAILABLE

    def test_zero_gpu_entry_size_rejected(self) -> None:
        region = _make_region()
        region[16:20] = struct.pack("<I", 0)  # dwGpuEntrySize
        with pytest.raises(PluginError) as excinfo:
            macm.validate_control_region(bytes(region))
        assert excinfo.value.code is ErrorCode.INTERFACE_UNAVAILABLE

    def test_oversized_gpu_entry_size_is_unsupported_version(self) -> None:
        region = _make_region()
        region[16:20] = struct.pack("<I", ctypes.sizeof(G) + 16)
        with pytest.raises(PluginError) as excinfo:
            macm.validate_control_region(bytes(region))
        assert excinfo.value.code is ErrorCode.UNSUPPORTED_VERSION
        assert "binding size" in excinfo.value.detail

    def test_declared_region_exceeding_mapped_size_rejected(self) -> None:
        with pytest.raises(PluginError) as excinfo:
            macm.validate_control_region(bytes(_make_region())[: ctypes.sizeof(H) + 4])
        assert excinfo.value.code is ErrorCode.INTERFACE_UNAVAILABLE

    def test_no_write_attempted_before_validation_passes(self) -> None:
        cases = {
            "dead": _make_region(signature=macm.MACM_DEAD_SIGNATURE),
            "bad_signature": _make_region(signature=0x11223344),
            "unsupported_version": _make_region(version=0x00030000),
            "bad_entry_size": _make_region(),
        }
        cases["bad_entry_size"][16:20] = struct.pack("<I", 9999)
        for label, region in cases.items():
            before = bytes(region)
            with pytest.raises(PluginError):
                macm.apply_control_to_region(region, 0, ControlFeature.POWER_LIMIT, 90.0)
            assert bytes(region) == before, f"{label}: region was modified"

    @given(st.binary(min_size=0, max_size=4096))
    @settings(max_examples=100)
    def test_adversarial_regions_never_crash(self, blob: bytes) -> None:
        try:
            macm.validate_control_region(blob)
        except PluginError:
            pass
        try:
            macm.build_capabilities_from_region(blob, 0)
        except PluginError:
            pass
        try:
            region = bytearray(blob)
            outcome = macm.apply_control_to_region(region, 0, ControlFeature.POWER_LIMIT, 80.0)
            assert outcome.action in ("noop", "applied")
        except PluginError:
            pass


# ---------------------------------------------------------------------------
# Single-control writes (named fields, FLUSH last, units, no-op)
# ---------------------------------------------------------------------------


class TestApplyControl:
    def test_power_limit_write_and_flush_set_last(self) -> None:
        region = _make_region(power_cur=100)
        outcome = macm.apply_control_to_region(region, 0, ControlFeature.POWER_LIMIT, 105.0)
        assert outcome.action == "applied"
        assert outcome.replaced == 100.0
        entry = _entry(region)
        assert entry.dwPowerLimitCur == 105
        assert _cmd(region) == macm.MACM_SHARED_MEMORY_COMMAND_FLUSH
        # Only the named field word + the command word changed.
        assert outcome.word_offsets == (
            ctypes.sizeof(H) + macm._field_offset(G, "dwPowerLimitCur"),
            macm._field_offset(H, "dwCommand"),
        )

    def test_core_offset_writes_khz_field(self) -> None:
        region = _make_region()
        outcome = macm.apply_control_to_region(region, 0, ControlFeature.CORE_OFFSET, 150.0)
        assert outcome.action == "applied"
        assert _entry(region).dwCoreClockBoostCur == 150000  # MHz -> KHz

    def test_memory_offset_writes_khz_field(self) -> None:
        region = _make_region()
        macm.apply_control_to_region(region, 0, ControlFeature.MEMORY_OFFSET, -100.0)
        assert _entry(region).dwMemoryClockBoostCur == -100000

    def test_noop_when_already_equal_within_tolerance(self) -> None:
        region = _make_region(power_cur=100)
        outcome = macm.apply_control_to_region(region, 0, ControlFeature.POWER_LIMIT, 100.0)
        assert outcome.action == "noop"
        assert outcome.word_offsets == ()
        assert _cmd(region) == 0  # no FLUSH issued
        assert _entry(region).dwPowerLimitCur == 100

    def test_noop_writes_nothing_for_fan_when_manual_and_equal(self) -> None:
        region = _make_region(fan_cur=55, fan_flags_cur=0)  # manual, 55%
        outcome = macm.apply_control_to_region(region, 0, ControlFeature.FAN_PERCENT, 55.0)
        assert outcome.action == "noop"
        assert _cmd(region) == 0

    def test_fan_write_clears_auto_flag(self) -> None:
        region = _make_region(fan_cur=30)
        outcome = macm.apply_control_to_region(region, 0, ControlFeature.FAN_PERCENT, 60.0)
        assert outcome.action == "applied"
        entry = _entry(region)
        assert entry.dwFanSpeedCur == 60
        assert entry.dwFanFlagsCur & macm.MACM_SHARED_MEMORY_GPU_ENTRY_FAN_FLAG_AUTO == 0
        assert _cmd(region) == macm.MACM_SHARED_MEMORY_COMMAND_FLUSH

    def test_unsupported_feature_when_flag_not_advertised(self) -> None:
        region = _make_region(gpu_flags=macm.MACM_SHARED_MEMORY_GPU_ENTRY_FLAG_FAN_SPEED)
        before = bytes(region)
        with pytest.raises(PluginError) as excinfo:
            macm.apply_control_to_region(region, 0, ControlFeature.POWER_LIMIT, 90.0)
        assert excinfo.value.code is ErrorCode.UNSUPPORTED_FEATURE
        assert bytes(region) == before

    def test_out_of_range_gpu_rejected(self) -> None:
        with pytest.raises(PluginError) as excinfo:
            macm.apply_control_to_region(_make_region(), 4, ControlFeature.POWER_LIMIT, 90.0)
        assert excinfo.value.code is ErrorCode.UNSUPPORTED_GPU

    def test_fan_curve_feature_has_no_macm_field(self) -> None:
        with pytest.raises(PluginError) as excinfo:
            macm.apply_control_to_region(_make_region(), 0, ControlFeature.FAN_CURVE, 0.0)
        assert excinfo.value.code is ErrorCode.UNSUPPORTED_FEATURE


class TestReset:
    def test_reset_restores_defaults_and_flushes(self) -> None:
        region = _make_region(power_cur=118, fan_cur=80)
        outcome = macm.reset_control_region(region, 0)
        assert outcome.action == "applied"
        entry = _entry(region)
        assert entry.dwPowerLimitCur == 100  # def
        assert entry.dwFanSpeedCur == 50  # def
        assert entry.dwFanFlagsCur == macm.MACM_SHARED_MEMORY_GPU_ENTRY_FAN_FLAG_AUTO
        assert _cmd(region) == macm.MACM_SHARED_MEMORY_COMMAND_FLUSH

    def test_reset_noop_when_already_defaults(self) -> None:
        region = _make_region(power_cur=100, fan_cur=50)
        outcome = macm.reset_control_region(region, 0)
        assert outcome.action == "noop"
        assert _cmd(region) == 0


# ---------------------------------------------------------------------------
# Capabilities & tuning-state reads
# ---------------------------------------------------------------------------


class TestReads:
    def test_capabilities_gate_on_flags_and_ranges(self) -> None:
        region = _make_region(gpu_flags=macm.MACM_SHARED_MEMORY_GPU_ENTRY_FLAG_POWER_LIMIT)
        caps = macm.build_capabilities_from_region(bytes(region), 0)
        assert caps.limits.power_limit_pct.min == 50.0
        assert caps.limits.power_limit_pct.max == 120.0
        assert caps.supported_controls == frozenset(
            {ControlFeature.POWER_LIMIT, ControlFeature.PROFILE_LOAD, ControlFeature.PROFILE_RESET}
        )
        assert caps.control_interface_status is InterfaceStatus.OK

    def test_capabilities_include_all_advertised_features(self) -> None:
        region = _make_region()
        caps = macm.build_capabilities_from_region(bytes(region), 0)
        assert ControlFeature.FAN_CURVE not in caps.supported_controls  # no MACM field
        assert caps.limits.core_offset_mhz.min == -300.0  # KHz -> MHz
        assert caps.limits.core_offset_mhz.max == 300.0

    def test_tuning_state_reads_live_values(self) -> None:
        region = _make_region(power_cur=110, boost_min=-300000)
        entry = _entry(region)
        entry.dwCoreClockBoostCur = 95000
        entry.dwMemoryClockBoostCur = 200000
        state = macm.tuning_state_from_region(bytes(region), 0)
        assert state.power_limit_pct == 110.0
        assert state.core_offset_mhz == 95.0
        assert state.memory_offset_mhz == 200.0
        assert state.fan_mode == "auto"
        assert state.fan_percent == 30.0

    def test_tuning_state_manual_fan_mode(self) -> None:
        region = _make_region(fan_flags_cur=0)
        state = macm.tuning_state_from_region(bytes(region), 0)
        assert state.fan_mode == "manual"


# ---------------------------------------------------------------------------
# FLUSH completion timeout (OS layer) — elevation mismatch is a fast, targeted error
# ---------------------------------------------------------------------------


class _FakeMutex:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeView:
    """A fake map view whose dwCommand never clears (drives the completion deadline)."""

    def __init__(self, command: int) -> None:
        header = H(dwSignature=macm.MACM_SIGNATURE, dwCommand=command)
        self._data = ctypes.string_at(ctypes.byref(header), ctypes.sizeof(H))

    def read_bytes(self) -> bytes:
        return self._data

    def mutex(self) -> _FakeMutex:
        return _FakeMutex()

    def write_words(self, offsets, data) -> None:
        pass


class _FakeMap(_FakeView):
    """A fake _ControlMap replacement: read-only region + recorded write_words calls."""

    def __init__(self, region: bytes) -> None:
        super().__init__(0)
        self._data = region
        self.writes: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def write_words(self, offsets, data) -> None:
        self.writes.append((list(offsets), bytes(data)))


class TestFlushCompletionTimeout:
    def _client(self):
        return macm.AfterburnerControlClient(
            flush_timeout=0.05, poll_interval=0.01
        )

    def test_apply_preflights_elevation_mismatch_before_any_write(self, monkeypatch) -> None:
        fake = _FakeMap(bytes(_make_region(power_cur=100)))
        monkeypatch.setattr(macm, "_ControlMap", lambda: fake)
        monkeypatch.setattr(macm, "_afterburner_elevation_mismatch", lambda: True)
        client = self._client()
        with pytest.raises(PluginError) as excinfo:
            client.apply_control(0, ControlFeature.POWER_LIMIT, 103.0)
        assert excinfo.value.code is ErrorCode.ACCESS_DENIED
        assert fake.writes == []  # no write, no FLUSH — fail fast

    def test_apply_noop_still_succeeds_with_elevation_mismatch(self, monkeypatch) -> None:
        fake = _FakeMap(bytes(_make_region(power_cur=100)))
        monkeypatch.setattr(macm, "_ControlMap", lambda: fake)
        monkeypatch.setattr(macm, "_afterburner_elevation_mismatch", lambda: True)
        client = self._client()
        result = client.apply_control(0, ControlFeature.POWER_LIMIT, 100.0)
        assert result.applied is False and "no change" in result.message
        assert fake.writes == []

    def test_timeout_with_elevation_mismatch_raises_access_denied(self, monkeypatch) -> None:
        monkeypatch.setattr(macm, "_afterburner_elevation_mismatch", lambda: True)
        client = self._client()
        with pytest.raises(PluginError) as excinfo:
            client._wait_for_completion(_FakeView(macm.MACM_SHARED_MEMORY_COMMAND_FLUSH))
        assert excinfo.value.code is ErrorCode.ACCESS_DENIED
        assert "administrator" in excinfo.value.user_message
        assert "elevated" in excinfo.value.detail

    def test_timeout_without_mismatch_stays_interface_unavailable(self, monkeypatch) -> None:
        monkeypatch.setattr(macm, "_afterburner_elevation_mismatch", lambda: None)
        client = self._client()
        with pytest.raises(PluginError) as excinfo:
            client._wait_for_completion(_FakeView(macm.MACM_SHARED_MEMORY_COMMAND_FLUSH))
        assert excinfo.value.code is ErrorCode.INTERFACE_UNAVAILABLE
        assert "didn't confirm" in excinfo.value.user_message
        assert "timed out" in excinfo.value.detail

    def test_flush_timeout_error_mapping_is_pure(self) -> None:
        code, message, detail = macm.flush_timeout_error(True)
        assert code is ErrorCode.ACCESS_DENIED and "elevated" in detail
        for mismatch in (False, None):
            code, message, detail = macm.flush_timeout_error(mismatch)
            assert code is ErrorCode.INTERFACE_UNAVAILABLE
            assert "timed out" in detail
            assert message  # non-empty, actionable
