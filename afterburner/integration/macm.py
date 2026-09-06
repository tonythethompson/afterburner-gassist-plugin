"""`AfterburnerControlClient` — capability-gated MACM control (official SDK header).

One of the three OS-touching modules (plan task 20.1). Writes MSI Afterburner's official
control shared memory (`MACMSharedMemory`, v2.0 `0x00020000`, `'MACM'` signature) exactly as
the shipped `SDK\\Samples\\SharedMemory\\MACMSharedMemorySample` does and as design § "MACM
Control Write Sequence" records — **not** reverse-engineered:

- every read-modify-write transaction runs under the named mutex
  `Global\\Access_MACMSharedMemory`;
- only **named** control fields are written (per the header's flag-gated field table), and
  `dwCommand = MACM_SHARED_MEMORY_COMMAND_FLUSH` is set **last**; there is no generic
  memory-write primitive and no wholesale map copy;
- the write-time Requirement 19.4 no-op is re-checked under the mutex (equality within
  `TUNING_MATCH_TOLERANCE` in model units) and a no-op returns success **without** issuing
  FLUSH;
- Afterburner's completion (dwCommand cleared) is polled within a bounded window, then the
  applied value is re-read only after completion — per the header's ordering guarantee, that
  read sees post-apply state, never a mid-flight value. A mismatch is reported honestly, never
  blindly re-written.

Validation precedes every write: a deallocated map (`0xDEAD`), wrong signature, unsupported
version, zero/oversized `dwGpuEntrySize`, or a declared region exceeding the map raises a
typed `PluginError` and **no write is attempted** (capability gating — Property 2). The struct
bindings are checked against the committed fixture (`tests/fixtures/sdk_layouts/
macm_layout.json`, task 20.3) and the installed header via
`tools/generate_sdk_layout_fixtures.py --diff`.

The write path is split so every invalid-header / gating / unit-conversion / no-op branch is
unit-testable on an injected byte buffer (`*_to_region` pure functions); the thin OS layer
only maps the file, snapshots a region under the mutex, copies back the exact words the pure
function changed (command word last), notifies Afterburner, and polls for completion.
"""
from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from ..models import (
    ControlFeature,
    ControlResult,
    ErrorCode,
    FanCurve,
    GpuCapabilities,
    GpuLimits,
    GpuTelemetry,
    InterfaceStatus,
    PluginError,
    Profile,
    Range,
    TuningState,
)
from .interface import AfterburnerInterface
from .mahm import _file_version, find_install_dir

# ---------------------------------------------------------------------------
# Shared-memory contract (verified against the shipped v2.0 header / fixture)
# ---------------------------------------------------------------------------

MACM_MAP_NAME = "MACMSharedMemory"
MACM_MUTEX_NAME = "Global\\Access_MACMSharedMemory"
MACM_NOTIFY_WINDOW = "MSI Afterburner "  # exact title (trailing space) used by the sample
MACM_NOTIFY_MESSAGE = "MACMCmdNotification"
MACM_SIGNATURE = 0x4D41434D  # 'MACM' as a little-endian DWORD
MACM_DEAD_SIGNATURE = 0x0000DEAD
MACM_VERSION = 0x00020000  # v2.0 minimum the header documents ("must be set to 0x00020000")
# Afterburner advances the minor feature level within the same header layout: the installed
# 4.6.7.17439 runs interface version 0x00020003 (v2.3 — the header's newest documented tier,
# verified live on 2026-09). The binding is to this exact header, and layout safety is
# guaranteed by the dwGpuEntrySize == sizeof(GPU_ENTRY) check below, so any v2.x whose entry
# layout matches is accepted; v1 and unknown majors are rejected as unsupported.
MACM_VERSION_MIN = 0x00020000
MACM_VERSION_MAX_EXCLUSIVE = 0x00030000

MACM_SHARED_MEMORY_COMMAND_INIT = 0x00AB0000
MACM_SHARED_MEMORY_COMMAND_FLUSH = 0x00AB0001
MACM_SHARED_MEMORY_COMMAND_FLUSH_WITHOUT_APPLYING = 0x00AB0002
MACM_SHARED_MEMORY_COMMAND_REFRESH_VF_CURVE = 0x00AB0003

MACM_SHARED_MEMORY_FLAG_SYNC = 0x00000002

MACM_SHARED_MEMORY_GPU_ENTRY_FLAG_CORE_CLOCK = 0x00000001
MACM_SHARED_MEMORY_GPU_ENTRY_FLAG_MEMORY_CLOCK = 0x00000004
MACM_SHARED_MEMORY_GPU_ENTRY_FLAG_FAN_SPEED = 0x00000008
MACM_SHARED_MEMORY_GPU_ENTRY_FLAG_CORE_VOLTAGE = 0x00000010
MACM_SHARED_MEMORY_GPU_ENTRY_FLAG_POWER_LIMIT = 0x00000400
MACM_SHARED_MEMORY_GPU_ENTRY_FLAG_CORE_CLOCK_BOOST = 0x00000800
MACM_SHARED_MEMORY_GPU_ENTRY_FLAG_MEMORY_CLOCK_BOOST = 0x00001000
MACM_SHARED_MEMORY_GPU_ENTRY_FAN_FLAG_AUTO = 0x00000001

FILE_MAP_ALL_ACCESS = 0x0006  # FILE_MAP_READ | FILE_MAP_WRITE (control client, by design)
_ERROR_FILE_NOT_FOUND = 2
_ERROR_ACCESS_DENIED = 5

DEFAULT_FLUSH_TIMEOUT_SECONDS = 5.0  # the shipped sample's wait budget
DEFAULT_FLUSH_POLL_INTERVAL = 0.1  # the shipped sample's 100 ms poll cadence
TUNING_MATCH_TOLERANCE = 1.0  # shared equality tolerance (model units; profiles.py constant)

_MAX_GPU_ENTRIES = 1024


class MACM_SHARED_MEMORY_HEADER(ctypes.Structure):
    """v2.0 control header (size 36)."""

    _fields_ = [
        ("dwSignature", ctypes.c_uint32),
        ("dwVersion", ctypes.c_uint32),
        ("dwHeaderSize", ctypes.c_uint32),
        ("dwNumGpuEntries", ctypes.c_uint32),
        ("dwGpuEntrySize", ctypes.c_uint32),
        ("dwMasterGpu", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_int32),
        ("dwCommand", ctypes.c_uint32),
    ]


class MACM_SHARED_MEMORY_VF_POINT_ENTRY(ctypes.Structure):
    _fields_ = [
        ("dwVoltageuV", ctypes.c_uint32),
        ("dwFrequency", ctypes.c_uint32),
        ("dwFrequencyOffset", ctypes.c_int32),
    ]


class MACM_SHARED_MEMORY_POWER_TUPLE_ENTRY(ctypes.Structure):
    _fields_ = [
        ("dwPowerCur", ctypes.c_uint32),
        ("dwPowerDef", ctypes.c_uint32),
        ("dwFrequencyCur", ctypes.c_uint32),
        ("dwFrequencyDef", ctypes.c_uint32),
    ]


class MACM_SHARED_MEMORY_THERMAL_TUPLE_ENTRY(ctypes.Structure):
    _fields_ = [
        ("dwTemperatureCur", ctypes.c_uint32),
        ("dwTemperatureDef", ctypes.c_uint32),
        ("dwFrequencyCur", ctypes.c_uint32),
        ("dwFrequencyDef", ctypes.c_uint32),
    ]


class MACM_SHARED_MEMORY_VF_CURVE(ctypes.Structure):
    """v2.3 voltage/frequency curve block (size 3224)."""

    _fields_ = [
        ("dwVersion", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("dwPoints", ctypes.c_uint32),
        ("vfPoints", MACM_SHARED_MEMORY_VF_POINT_ENTRY * 256),
        ("dwLockIndex", ctypes.c_uint32),
        ("dwPowerTuples", ctypes.c_uint32),
        ("powerTuples", MACM_SHARED_MEMORY_POWER_TUPLE_ENTRY * 4),
        ("dwThermalTuples", ctypes.c_uint32),
        ("thermalTuples", MACM_SHARED_MEMORY_THERMAL_TUPLE_ENTRY * 4),
    ]


class MACM_SHARED_MEMORY_GPU_ENTRY(ctypes.Structure):
    """v2.0 GPU control entry (size 3760) — field order per the shipped header/fixture."""

    _fields_ = [
        ("dwFlags", ctypes.c_uint32),
        ("dwCoreClockCur", ctypes.c_uint32),
        ("dwCoreClockMin", ctypes.c_uint32),
        ("dwCoreClockMax", ctypes.c_uint32),
        ("dwCoreClockDef", ctypes.c_uint32),
        ("dwShaderClockCur", ctypes.c_uint32),
        ("dwShaderClockMin", ctypes.c_uint32),
        ("dwShaderClockMax", ctypes.c_uint32),
        ("dwShaderClockDef", ctypes.c_uint32),
        ("dwMemoryClockCur", ctypes.c_uint32),
        ("dwMemoryClockMin", ctypes.c_uint32),
        ("dwMemoryClockMax", ctypes.c_uint32),
        ("dwMemoryClockDef", ctypes.c_uint32),
        ("dwFanSpeedCur", ctypes.c_uint32),
        ("dwFanFlagsCur", ctypes.c_uint32),
        ("dwFanSpeedMin", ctypes.c_uint32),
        ("dwFanSpeedMax", ctypes.c_uint32),
        ("dwFanSpeedDef", ctypes.c_uint32),
        ("dwFanFlagsDef", ctypes.c_uint32),
        ("dwCoreVoltageCur", ctypes.c_uint32),
        ("dwCoreVoltageMin", ctypes.c_uint32),
        ("dwCoreVoltageMax", ctypes.c_uint32),
        ("dwCoreVoltageDef", ctypes.c_uint32),
        ("dwMemoryVoltageCur", ctypes.c_uint32),
        ("dwMemoryVoltageMin", ctypes.c_uint32),
        ("dwMemoryVoltageMax", ctypes.c_uint32),
        ("dwMemoryVoltageDef", ctypes.c_uint32),
        ("dwAuxVoltageCur", ctypes.c_uint32),
        ("dwAuxVoltageMin", ctypes.c_uint32),
        ("dwAuxVoltageMax", ctypes.c_uint32),
        ("dwAuxVoltageDef", ctypes.c_uint32),
        ("dwCoreVoltageBoostCur", ctypes.c_int32),
        ("dwCoreVoltageBoostMin", ctypes.c_int32),
        ("dwCoreVoltageBoostMax", ctypes.c_int32),
        ("dwCoreVoltageBoostDef", ctypes.c_int32),
        ("dwMemoryVoltageBoostCur", ctypes.c_int32),
        ("dwMemoryVoltageBoostMin", ctypes.c_int32),
        ("dwMemoryVoltageBoostMax", ctypes.c_int32),
        ("dwMemoryVoltageBoostDef", ctypes.c_int32),
        ("dwAuxVoltageBoostCur", ctypes.c_int32),
        ("dwAuxVoltageBoostMin", ctypes.c_int32),
        ("dwAuxVoltageBoostMax", ctypes.c_int32),
        ("dwAuxVoltageBoostDef", ctypes.c_int32),
        ("dwPowerLimitCur", ctypes.c_int32),
        ("dwPowerLimitMin", ctypes.c_int32),
        ("dwPowerLimitMax", ctypes.c_int32),
        ("dwPowerLimitDef", ctypes.c_int32),
        ("dwCoreClockBoostCur", ctypes.c_int32),
        ("dwCoreClockBoostMin", ctypes.c_int32),
        ("dwCoreClockBoostMax", ctypes.c_int32),
        ("dwCoreClockBoostDef", ctypes.c_int32),
        ("dwMemoryClockBoostCur", ctypes.c_int32),
        ("dwMemoryClockBoostMin", ctypes.c_int32),
        ("dwMemoryClockBoostMax", ctypes.c_int32),
        ("dwMemoryClockBoostDef", ctypes.c_int32),
        ("dwThermalLimitCur", ctypes.c_int32),
        ("dwThermalLimitMin", ctypes.c_int32),
        ("dwThermalLimitMax", ctypes.c_int32),
        ("dwThermalLimitDef", ctypes.c_int32),
        ("dwThermalPrioritizeCur", ctypes.c_uint32),
        ("dwThermalPrioritizeDef", ctypes.c_uint32),
        ("dwAux2VoltageCur", ctypes.c_uint32),
        ("dwAux2VoltageMin", ctypes.c_uint32),
        ("dwAux2VoltageMax", ctypes.c_uint32),
        ("dwAux2VoltageDef", ctypes.c_uint32),
        ("dwAux2VoltageBoostCur", ctypes.c_int32),
        ("dwAux2VoltageBoostMin", ctypes.c_int32),
        ("dwAux2VoltageBoostMax", ctypes.c_int32),
        ("dwAux2VoltageBoostDef", ctypes.c_int32),
        ("vfCurve", MACM_SHARED_MEMORY_VF_CURVE),
        ("szGpuId", ctypes.c_char * 260),
    ]


@dataclass(frozen=True)
class ControlSpec:
    """Named-field mapping for one single-value control feature (design field table)."""

    feature: ControlFeature
    flag: int  # gate bit in entry.dwFlags
    cur: str  # *_Cur field name
    minimum: str  # *_Min field name
    maximum: str  # *_Max field name
    default: str  # *_Def field name
    scale: float = 1.0  # field units per model unit (clock offsets: 1000 KHz/MHz)


_CONTROL_SPECS: Dict[ControlFeature, ControlSpec] = {
    ControlFeature.POWER_LIMIT: ControlSpec(
        ControlFeature.POWER_LIMIT,
        flag=MACM_SHARED_MEMORY_GPU_ENTRY_FLAG_POWER_LIMIT,
        cur="dwPowerLimitCur",
        minimum="dwPowerLimitMin",
        maximum="dwPowerLimitMax",
        default="dwPowerLimitDef",
        scale=1.0,
    ),
    ControlFeature.CORE_OFFSET: ControlSpec(
        ControlFeature.CORE_OFFSET,
        flag=MACM_SHARED_MEMORY_GPU_ENTRY_FLAG_CORE_CLOCK_BOOST,
        cur="dwCoreClockBoostCur",
        minimum="dwCoreClockBoostMin",
        maximum="dwCoreClockBoostMax",
        default="dwCoreClockBoostDef",
        scale=1000.0,  # MHz -> KHz
    ),
    ControlFeature.MEMORY_OFFSET: ControlSpec(
        ControlFeature.MEMORY_OFFSET,
        flag=MACM_SHARED_MEMORY_GPU_ENTRY_FLAG_MEMORY_CLOCK_BOOST,
        cur="dwMemoryClockBoostCur",
        minimum="dwMemoryClockBoostMin",
        maximum="dwMemoryClockBoostMax",
        default="dwMemoryClockBoostDef",
        scale=1000.0,  # MHz -> KHz
    ),
    ControlFeature.VOLTAGE: ControlSpec(
        ControlFeature.VOLTAGE,
        flag=MACM_SHARED_MEMORY_GPU_ENTRY_FLAG_CORE_VOLTAGE,
        cur="dwCoreVoltageCur",
        minimum="dwCoreVoltageMin",
        maximum="dwCoreVoltageMax",
        default="dwCoreVoltageDef",
        scale=1.0,
    ),
    ControlFeature.FAN_PERCENT: ControlSpec(
        ControlFeature.FAN_PERCENT,
        flag=MACM_SHARED_MEMORY_GPU_ENTRY_FLAG_FAN_SPEED,
        cur="dwFanSpeedCur",
        minimum="dwFanSpeedMin",
        maximum="dwFanSpeedMax",
        default="dwFanSpeedDef",
        scale=1.0,
    ),
}


def _unavailable(message: str, detail: str) -> PluginError:
    return PluginError(ErrorCode.INTERFACE_UNAVAILABLE, message, detail=detail)


# ---------------------------------------------------------------------------
# Pure region parsing / validation (byte-buffer testable, mirrors the runtime checks)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedControl:
    version: int
    header_size: int
    num_gpu_entries: int
    gpu_entry_size: int
    master_gpu: int
    flags: int
    command: int


def validate_control_region(data: bytes) -> ParsedControl:
    """Validate a MACM region's header. Raises typed errors; never writes."""
    if len(data) < ctypes.sizeof(MACM_SHARED_MEMORY_HEADER):
        raise _unavailable(
            "MSI Afterburner's control data is incomplete right now. Try again shortly.",
            f"region smaller than MACM header ({len(data)} bytes)",
        )
    header = MACM_SHARED_MEMORY_HEADER.from_buffer_copy(
        data[: ctypes.sizeof(MACM_SHARED_MEMORY_HEADER)]
    )
    if header.dwSignature == MACM_DEAD_SIGNATURE:
        raise PluginError(
            ErrorCode.DISCONNECTED,
            "MSI Afterburner is closing down. Start it and try again.",
            detail="MACM map marked for deallocation (0xDEAD)",
        )
    if header.dwSignature != MACM_SIGNATURE:
        raise _unavailable(
            "MSI Afterburner's control interface isn't initialized yet. Start it and try again.",
            f"bad MACM signature 0x{header.dwSignature:08x}",
        )
    if not (MACM_VERSION_MIN <= header.dwVersion < MACM_VERSION_MAX_EXCLUSIVE):
        raise PluginError(
            ErrorCode.UNSUPPORTED_VERSION,
            "Your Afterburner version doesn't expose the needed interface. Please update.",
            detail=(
                f"MACM version 0x{header.dwVersion:08x}; supported v2.x with the bound "
                "v2.3 header layout (entry size 3760)"
            ),
        )
    if header.dwHeaderSize < ctypes.sizeof(MACM_SHARED_MEMORY_HEADER):
        raise _unavailable(
            "MSI Afterburner's control data is malformed right now. Try again shortly.",
            f"dwHeaderSize {header.dwHeaderSize} < header struct "
            f"{ctypes.sizeof(MACM_SHARED_MEMORY_HEADER)}",
        )
    if header.dwNumGpuEntries > _MAX_GPU_ENTRIES:
        raise _unavailable(
            "MSI Afterburner's control data is malformed right now. Try again shortly.",
            f"implausible GPU entry count {header.dwNumGpuEntries}",
        )
    binding_size = ctypes.sizeof(MACM_SHARED_MEMORY_GPU_ENTRY)
    if header.dwNumGpuEntries > 0:
        if header.dwGpuEntrySize == 0:
            raise _unavailable(
                "MSI Afterburner's control data is malformed right now. Try again shortly.",
                "dwGpuEntrySize is zero with GPU entries present",
            )
        if header.dwGpuEntrySize != binding_size:
            raise PluginError(
                ErrorCode.UNSUPPORTED_VERSION,
                "Your Afterburner version doesn't expose the needed interface. Please update.",
                detail=f"dwGpuEntrySize {header.dwGpuEntrySize} != binding size {binding_size}",
            )
    region_end = header.dwHeaderSize + header.dwNumGpuEntries * header.dwGpuEntrySize
    if region_end > len(data):
        raise _unavailable(
            "MSI Afterburner's control data is incomplete right now. Try again shortly.",
            f"declared region {region_end} exceeds mapped size {len(data)}",
        )
    return ParsedControl(
        version=header.dwVersion,
        header_size=header.dwHeaderSize,
        num_gpu_entries=header.dwNumGpuEntries,
        gpu_entry_size=header.dwGpuEntrySize,
        master_gpu=header.dwMasterGpu,
        flags=header.dwFlags,
        command=header.dwCommand,
    )


def _entry_offset(parsed: ParsedControl, gpu_index: int) -> int:
    if gpu_index < 0 or gpu_index >= parsed.num_gpu_entries:
        raise PluginError(
            ErrorCode.UNSUPPORTED_GPU,
            f"GPU index {gpu_index} isn't available.",
            detail=f"MACM reports {parsed.num_gpu_entries} GPU(s)",
        )
    return parsed.header_size + gpu_index * parsed.gpu_entry_size


def _entry_view(region: bytearray, parsed: ParsedControl, gpu_index: int):
    offset = _entry_offset(parsed, gpu_index)
    return MACM_SHARED_MEMORY_GPU_ENTRY.from_buffer(region, offset)


def _to_model_units(field_value: int, spec: ControlSpec) -> float:
    return field_value / spec.scale


def _to_field_units(value: float, spec: ControlSpec) -> int:
    return int(round(value * spec.scale))


def _range_for(entry, spec: ControlSpec) -> Optional[Range]:
    minimum = _to_model_units(int(getattr(entry, spec.minimum)), spec)
    maximum = _to_model_units(int(getattr(entry, spec.maximum)), spec)
    return Range(minimum, maximum)


# ---------------------------------------------------------------------------
# Pure apply / reset / capability / state logic on an injected byte buffer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AppliedOutcome:
    action: str  # "noop" | "applied"
    replaced: Optional[float]  # previous *_Cur in model units (write-time)
    word_offsets: Tuple[int, ...]  # absolute offsets the caller must copy back (cmd last)


def apply_control_to_region(
    region: bytearray,
    gpu_index: int,
    feature: ControlFeature,
    value: float,
    tolerance: float = TUNING_MATCH_TOLERANCE,
) -> AppliedOutcome:
    """Pure, testable single-control write on an injected mutable region.

    Validates the header first (no write on any validation failure), then — only for the
    flag-gated **named** field — re-checks equality at write time, writes the value field,
    clears the fan-auto bit for a manual fan setpoint, and sets `dwCommand = FLUSH` last.
    """
    parsed = validate_control_region(bytes(region))
    spec = _CONTROL_SPECS.get(feature)
    if spec is None:
        raise PluginError(
            ErrorCode.UNSUPPORTED_FEATURE,
            "That control isn't available on this hardware/version.",
            detail=f"{feature.value} has no MACM named field (fan curves/profile features "
            "are not expressible as a single MACM field write)",
        )
    entry = _entry_view(region, parsed, gpu_index)
    if not (entry.dwFlags & spec.flag):
        raise PluginError(
            ErrorCode.UNSUPPORTED_FEATURE,
            "That control isn't available on this hardware/version.",
            detail=f"MACM flag 0x{spec.flag:08x} not set for {feature.value}",
        )

    cur_value = _to_model_units(int(getattr(entry, spec.cur)), spec)
    fan_manual = feature is not ControlFeature.FAN_PERCENT or not (
        entry.dwFanFlagsCur & MACM_SHARED_MEMORY_GPU_ENTRY_FAN_FLAG_AUTO
    )
    if abs(cur_value - value) <= tolerance and fan_manual:
        return AppliedOutcome(action="noop", replaced=None, word_offsets=())

    replaced = cur_value
    setattr(entry, spec.cur, _to_field_units(value, spec))
    offsets = [_entry_offset(parsed, gpu_index) + _field_offset(MACM_SHARED_MEMORY_GPU_ENTRY, spec.cur)]
    if feature is ControlFeature.FAN_PERCENT:
        entry.dwFanFlagsCur &= ~MACM_SHARED_MEMORY_GPU_ENTRY_FAN_FLAG_AUTO
        offsets.append(
            _entry_offset(parsed, gpu_index)
            + _field_offset(MACM_SHARED_MEMORY_GPU_ENTRY, "dwFanFlagsCur")
        )
    header = MACM_SHARED_MEMORY_HEADER.from_buffer(region)
    header.dwCommand = MACM_SHARED_MEMORY_COMMAND_FLUSH  # set LAST
    offsets.append(_field_offset(MACM_SHARED_MEMORY_HEADER, "dwCommand"))
    return AppliedOutcome(action="applied", replaced=replaced, word_offsets=tuple(offsets))


def reset_control_region(
    region: bytearray,
    gpu_index: int,
    tolerance: float = TUNING_MATCH_TOLERANCE,
) -> AppliedOutcome:
    """Pure reset: for every supported single-value control group, *_Cur -> *_Def (+ FLUSH)."""
    parsed = validate_control_region(bytes(region))
    entry = _entry_view(region, parsed, gpu_index)
    base = _entry_offset(parsed, gpu_index)
    changed_offsets: List[int] = []
    fan_reset = False
    for spec in _CONTROL_SPECS.values():
        if not (entry.dwFlags & spec.flag):
            continue
        if abs(_to_model_units(int(getattr(entry, spec.cur)), spec) - _to_model_units(
            int(getattr(entry, spec.default)), spec
        )) > tolerance:
            setattr(entry, spec.cur, int(getattr(entry, spec.default)))
            changed_offsets.append(base + _field_offset(MACM_SHARED_MEMORY_GPU_ENTRY, spec.cur))
        if spec.feature is ControlFeature.FAN_PERCENT and (
            entry.dwFanFlagsCur != entry.dwFanFlagsDef
        ):
            entry.dwFanFlagsCur = entry.dwFanFlagsDef
            fan_reset = True
    if not changed_offsets and not fan_reset:
        return AppliedOutcome(action="noop", replaced=None, word_offsets=())
    if fan_reset:
        changed_offsets.append(base + _field_offset(MACM_SHARED_MEMORY_GPU_ENTRY, "dwFanFlagsCur"))
    header = MACM_SHARED_MEMORY_HEADER.from_buffer(region)
    header.dwCommand = MACM_SHARED_MEMORY_COMMAND_FLUSH  # set LAST
    changed_offsets.append(_field_offset(MACM_SHARED_MEMORY_HEADER, "dwCommand"))
    return AppliedOutcome(action="applied", replaced=None, word_offsets=tuple(changed_offsets))


def build_capabilities_from_region(data: bytes, gpu_index: int) -> GpuCapabilities:
    """Typed capabilities from a validated MACM region (flag + reported-range gating)."""
    parsed = validate_control_region(data)
    entry = MACM_SHARED_MEMORY_GPU_ENTRY.from_buffer_copy(
        data[_entry_offset(parsed, gpu_index) : _entry_offset(parsed, gpu_index) + ctypes.sizeof(MACM_SHARED_MEMORY_GPU_ENTRY)]
    )
    supported: set = set()
    limits = GpuLimits()
    for spec in _CONTROL_SPECS.values():
        if not (entry.dwFlags & spec.flag):
            continue
        rng = _range_for(entry, spec)
        if rng is None or rng.inverted:
            continue
        supported.add(spec.feature)
        if spec.feature is ControlFeature.POWER_LIMIT:
            limits = _with(limits, power_limit_pct=rng)
        elif spec.feature is ControlFeature.CORE_OFFSET:
            limits = _with(limits, core_offset_mhz=rng)
        elif spec.feature is ControlFeature.MEMORY_OFFSET:
            limits = _with(limits, memory_offset_mhz=rng)
        elif spec.feature is ControlFeature.VOLTAGE:
            limits = _with(limits, voltage_mv=rng)
        elif spec.feature is ControlFeature.FAN_PERCENT:
            limits = _with(limits, fan_percent=rng)
    # A live MACM interface implies profile apply/reset are writable (resolver re-gates on
    # control_interface_status). Fan curves are NOT expressible as a MACM field and are
    # deliberately never advertised here.
    supported.add(ControlFeature.PROFILE_LOAD)
    supported.add(ControlFeature.PROFILE_RESET)
    return GpuCapabilities(
        gpu_index=gpu_index,
        gpu_name=_cstr(entry.szGpuId),
        supported_controls=frozenset(supported),
        limits=limits,
        control_interface_status=InterfaceStatus.OK,
    )


def _with(limits: GpuLimits, **kwargs) -> GpuLimits:
    from dataclasses import replace

    return replace(limits, **kwargs)


def tuning_state_from_region(data: bytes, gpu_index: int) -> TuningState:
    """Typed applied tuning state from a validated MACM region (live *_Cur values)."""
    parsed = validate_control_region(data)
    entry = MACM_SHARED_MEMORY_GPU_ENTRY.from_buffer_copy(
        data[_entry_offset(parsed, gpu_index) : _entry_offset(parsed, gpu_index) + ctypes.sizeof(MACM_SHARED_MEMORY_GPU_ENTRY)]
    )
    power = _CONTROL_SPECS[ControlFeature.POWER_LIMIT]
    core = _CONTROL_SPECS[ControlFeature.CORE_OFFSET]
    mem = _CONTROL_SPECS[ControlFeature.MEMORY_OFFSET]
    volt = _CONTROL_SPECS[ControlFeature.VOLTAGE]
    fan = _CONTROL_SPECS[ControlFeature.FAN_PERCENT]
    fan_mode = "auto" if entry.dwFanFlagsCur & MACM_SHARED_MEMORY_GPU_ENTRY_FAN_FLAG_AUTO else "manual"
    return TuningState(
        gpu_index=gpu_index,
        power_limit_pct=_to_model_units(int(getattr(entry, power.cur)), power),
        core_offset_mhz=_to_model_units(int(getattr(entry, core.cur)), core),
        memory_offset_mhz=_to_model_units(int(getattr(entry, mem.cur)), mem),
        voltage_mv=_to_model_units(int(getattr(entry, volt.cur)), volt),
        fan_mode=fan_mode,
        fan_percent=_to_model_units(int(getattr(entry, fan.cur)), fan),
    )


def _cstr(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()


def _field_offset(structure, field_name: str) -> int:
    return int(getattr(structure, field_name).offset)


# ---------------------------------------------------------------------------
# OS layer: map + mutex + notify + completion poll
# ---------------------------------------------------------------------------


class _MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("PartitionId", wintypes.WORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


class _ControlMap:
    """A FILE_MAP_ALL_ACCESS view of MACMSharedMemory (control client — writes by design)."""

    def __init__(self) -> None:
        if os.name != "nt":  # pragma: no cover - exercised only off-Windows
            raise PluginError(
                ErrorCode.INTERFACE_UNAVAILABLE,
                "MSI Afterburner only runs on Windows.",
                detail="MACM shared memory requires Windows",
            )
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.OpenFileMappingW.restype = wintypes.HANDLE
        kernel32.MapViewOfFile.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_size_t,
        ]
        kernel32.MapViewOfFile.restype = ctypes.c_void_p
        kernel32.VirtualQuery.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_MEMORY_BASIC_INFORMATION),
            ctypes.c_size_t,
        ]
        kernel32.VirtualQuery.restype = ctypes.c_size_t
        kernel32.CreateMutexW.argtypes = [
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel32.ReleaseMutex.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
        kernel32.UnmapViewOfFile.restype = wintypes.BOOL
        kernel32.RtlMoveMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
        kernel32.RtlMoveMemory.restype = None
        self._kernel32 = kernel32
        self._handle = None
        self._addr = None
        self._region_size = 0
        self._mutex = None

        handle = kernel32.OpenFileMappingW(FILE_MAP_ALL_ACCESS, False, MACM_MAP_NAME)
        if not handle:
            self._raise_open_error()
        self._handle = handle
        try:
            addr = kernel32.MapViewOfFile(handle, FILE_MAP_ALL_ACCESS, 0, 0, 0)
            if not addr:
                self._raise_map_error()
            self._addr = addr
            info = _MEMORY_BASIC_INFORMATION()
            if not kernel32.VirtualQuery(addr, ctypes.byref(info), ctypes.sizeof(info)):
                self._raise_map_error()
            self._region_size = int(info.RegionSize)
            if self._region_size <= 0:
                self._raise_map_error()
        except BaseException:
            self.close()
            raise

    def _raise_open_error(self) -> None:
        err = ctypes.get_last_error()
        if err == _ERROR_FILE_NOT_FOUND:
            raise PluginError(
                ErrorCode.NOT_RUNNING,
                "MSI Afterburner isn't running. Start it and try again.",
                detail=f"MACM map '{MACM_MAP_NAME}' not found (error {err})",
            )
        if err == _ERROR_ACCESS_DENIED:
            raise PluginError(
                ErrorCode.ACCESS_DENIED,
                "Controlling Afterburner needs elevated permissions.",
                detail=f"OpenFileMappingW access denied (error {err})",
            )
        raise PluginError(
            ErrorCode.INTERFACE_UNAVAILABLE,
            "MSI Afterburner's control interface isn't reachable right now.",
            detail=f"OpenFileMappingW failed (error {err})",
        )

    def _raise_map_error(self) -> None:
        err = ctypes.get_last_error()
        if err == _ERROR_ACCESS_DENIED:
            raise PluginError(
                ErrorCode.ACCESS_DENIED,
                "Controlling Afterburner needs elevated permissions.",
                detail=f"MapViewOfFile access denied (error {err})",
            )
        raise PluginError(
            ErrorCode.INTERFACE_UNAVAILABLE,
            "MSI Afterburner's control interface isn't reachable right now.",
            detail=f"MapViewOfFile/VirtualQuery failed (error {err})",
        )

    def read_bytes(self) -> bytes:
        assert self._addr is not None
        return ctypes.string_at(self._addr, self._region_size)

    def write_words(self, offsets: Sequence[int], data: bytes) -> None:
        """Copy the exact 4-byte words the pure layer changed (command word is last)."""
        assert self._addr is not None
        kernel32 = self._kernel32
        for offset in offsets:
            kernel32.RtlMoveMemory(self._addr + offset, data[offset : offset + 4], 4)

    # ------------------------------------------------------------------ mutex
    def mutex(self) -> "_MutexGuard":
        handle = self._kernel32.CreateMutexW(None, False, MACM_MUTEX_NAME)
        if not handle:
            raise PluginError(
                ErrorCode.INTERFACE_UNAVAILABLE,
                "MSI Afterburner's control interface isn't reachable right now.",
                detail=f"CreateMutexW failed (error {ctypes.get_last_error()})",
            )
        result = self._kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)  # INFINITE
        if result not in (0, 0x00000080):  # WAIT_OBJECT_0 / WAIT_ABANDONED
            self._kernel32.CloseHandle(handle)
            raise PluginError(
                ErrorCode.INTERFACE_UNAVAILABLE,
                "MSI Afterburner's control interface is busy right now. Try again.",
                detail=f"mutex wait failed (result 0x{result:08x})",
            )
        return _MutexGuard(self._kernel32, handle)

    def close(self) -> None:
        kernel32 = self._kernel32
        if self._addr:
            kernel32.UnmapViewOfFile(self._addr)
            self._addr = None
        if self._handle:
            kernel32.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self) -> "_ControlMap":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class _MutexGuard:
    def __init__(self, kernel32, handle) -> None:
        self._kernel32 = kernel32
        self._handle = handle

    def __enter__(self) -> "_MutexGuard":
        return self

    def __exit__(self, *exc) -> None:
        if self._handle:
            self._kernel32.ReleaseMutex(self._handle)
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def _notify_afterburner() -> None:
    """Best-effort PostMessage of the sample's registered 'MACMCmdNotification' message."""
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=False)
        user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
        user32.RegisterWindowMessageW.restype = wintypes.UINT
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user32.FindWindowW.restype = wintypes.HANDLE
        user32.PostMessageW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.PostMessageW.restype = wintypes.BOOL
        message = user32.RegisterWindowMessageW(MACM_NOTIFY_MESSAGE)
        if not message:
            return
        hwnd = user32.FindWindowW(None, MACM_NOTIFY_WINDOW)
        if hwnd:
            user32.PostMessageW(hwnd, message, 0, 0)
    except Exception:  # pragma: no cover - notify is best-effort; polling still completes
        return


# ---------------------------------------------------------------------------
# Elevation-mismatch detection (best-effort; None when undeterminable)
# ---------------------------------------------------------------------------


def _current_process_elevated() -> Optional[bool]:
    """Whether this process runs with an elevated (administrator) token."""
    if os.name != "nt":  # pragma: no cover - Windows-only concern
        return None
    try:
        shell32 = ctypes.WinDLL("shell32")
        shell32.IsUserAnAdmin.restype = wintypes.BOOL
        return bool(shell32.IsUserAnAdmin())
    except Exception:  # pragma: no cover - defensive
        return None


def _token_elevation(pid: int) -> Optional[bool]:
    """Whether process ``pid`` carries an elevated token (query-limited access only)."""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.GetTokenInformation.restype = wintypes.BOOL

        class _TOKEN_ELEVATION(ctypes.Structure):
            _fields_ = [("TokenIsElevated", wintypes.DWORD)]

        # PROCESS_QUERY_LIMITED_INFORMATION (0x1000) is enough to read the token.
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            token = wintypes.HANDLE()
            if not advapi32.OpenProcessToken(handle, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
                return None
            try:
                info = _TOKEN_ELEVATION()
                size = wintypes.DWORD()
                ok = advapi32.GetTokenInformation(
                    token, 20, ctypes.byref(info), ctypes.sizeof(info), ctypes.byref(size)
                )  # TokenElevation = 20
                return bool(info.TokenIsElevated) if ok else None
            finally:
                kernel32.CloseHandle(token)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # pragma: no cover - defensive
        return None


def _afterburner_elevation_mismatch() -> Optional[bool]:
    """True when Afterburner runs elevated while this process does not.

    Observed live on Afterburner 4.6.7.17439: an elevated Afterburner never confirms MACM
    FLUSH commands from a non-elevated process within the protocol's completion budget —
    the value words land in the map but `dwCommand` stays set past the 5 s deadline (and
    may only be applied minutes later, if at all), so every apply times out with an
    ambiguous may-or-may-not-have-applied outcome. The MSI sample and this protocol assume
    matching elevation. Returns None when the comparison cannot be made (Afterburner window
    absent, API failure) so callers keep the generic timeout behaviour rather than guessing.
    """
    if os.name != "nt":
        return None
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=False)
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user32.FindWindowW.restype = wintypes.HANDLE
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        hwnd = user32.FindWindowW(None, MACM_NOTIFY_WINDOW)
        if not hwnd:
            return None
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        afterburner_elevated = _token_elevation(int(pid.value))
        own_elevated = _current_process_elevated()
        if afterburner_elevated is None or own_elevated is None:
            return None
        return bool(afterburner_elevated and not own_elevated)
    except Exception:  # pragma: no cover - defensive
        return None


def flush_timeout_error(
    mismatch: Optional[bool],
) -> Tuple[ErrorCode, str, str]:
    """The typed error for a FLUSH completion timeout.

    When an elevation mismatch is detectable (Afterburner elevated, caller not), the failure
    is `ACCESS_DENIED` with a targeted message; otherwise the generic honest timeout error
    stands. Pure so the mapping is unit-testable without Windows APIs.
    """
    if mismatch:
        return (
            ErrorCode.ACCESS_DENIED,
            "Afterburner is running with administrator privileges but this plugin is not, "
            "and Afterburner will not confirm change requests from this process within the "
            "protocol timeout. Restart G-Assist/NVIDIA App elevated (or run Afterburner "
            "without elevation) and try again.",
            "elevation mismatch: elevated Afterburner, non-elevated caller "
            "(FLUSH not confirmed within the completion budget)",
        )
    return (
        ErrorCode.INTERFACE_UNAVAILABLE,
        "MSI Afterburner didn't confirm the change in time. The setting may or may not have "
        "applied — check MSI Afterburner.",
        "FLUSH completion timed out (dwCommand never cleared)",
    )


class AfterburnerControlClient:
    """Capability-gated MACM control adapter (the only privileged write path).

    Implements the control parts of `AfterburnerInterface`: live capabilities/tuning-state
    reads from the MACM map, named-field single-control writes with FLUSH and read-back
    verification, and reset-to-defaults. Monitoring reads are NOT part of this client (they
    belong to the MAHM monitoring client, plan task 18); they raise typed errors here.
    """

    def __init__(
        self,
        install_dir: Optional[os.PathLike] = None,
        *,
        flush_timeout: float = DEFAULT_FLUSH_TIMEOUT_SECONDS,
        poll_interval: float = DEFAULT_FLUSH_POLL_INTERVAL,
        clock=None,
    ) -> None:
        self.install_dir = (
            find_install_dir(str(install_dir)) if install_dir else find_install_dir()
        )
        self.flush_timeout = flush_timeout
        self.poll_interval = poll_interval
        self._clock = clock or time.monotonic
        self._version_cache: Optional[str] = None

    # -------------------------------------------------------------- lifecycle
    def detect(self) -> InterfaceStatus:
        if self.install_dir is None:
            return InterfaceStatus.NOT_INSTALLED
        try:
            with _ControlMap() as view:
                validate_control_region(view.read_bytes())
            return InterfaceStatus.OK
        except PluginError as exc:
            mapping = {
                ErrorCode.NOT_RUNNING: InterfaceStatus.NOT_RUNNING,
                ErrorCode.DISCONNECTED: InterfaceStatus.NOT_RUNNING,
                ErrorCode.UNSUPPORTED_VERSION: InterfaceStatus.UNSUPPORTED_VERSION,
                ErrorCode.ACCESS_DENIED: InterfaceStatus.ACCESS_DENIED,
            }
            return mapping.get(exc.code, InterfaceStatus.UNAVAILABLE)

    def get_version(self) -> Optional[str]:
        if self._version_cache is not None:
            return self._version_cache
        if self.install_dir is None:
            return None
        self._version_cache = _file_version(self.install_dir / "MSIAfterburner.exe")
        return self._version_cache

    # -------------------------------------------------- live control reads
    def _region(self) -> bytes:
        with _ControlMap() as view:
            return view.read_bytes()

    def read_capabilities(self, gpu_index: int) -> GpuCapabilities:
        return build_capabilities_from_region(self._region(), gpu_index)

    def read_tuning_state(self, gpu_index: int) -> TuningState:
        return tuning_state_from_region(self._region(), gpu_index)

    # ------------------------------------------------------------- control
    def apply_control(
        self, gpu_index: int, feature: ControlFeature, value: float
    ) -> ControlResult:
        with _ControlMap() as view:  # map stays open through the completion poll
            outcome = self._transact_apply(view, gpu_index, feature, value)
            if outcome.action == "noop":
                return ControlResult(
                    feature=feature,
                    requested_value=value,
                    applied_value=value,
                    clamped=False,
                    applied=False,
                    replaced_value=None,
                    message=f"{_label(feature)} already set to {value:g} — no change made.",
                )
            replaced = outcome.replaced
            # FLUSH issued: notify Afterburner and wait for the command to clear.
            self._wait_for_completion(view)
            # Verification read only now (completion implies post-apply read-back).
            applied = self._verified_apply_value(view, gpu_index, feature)
        if applied is None or abs(applied - value) > TUNING_MATCH_TOLERANCE:
            actual = applied if applied is not None else "unknown"
            message = (
                f"{_label(feature)} set to {value:g}, but Afterburner reports {actual}. "
                "The change was applied — verify in MSI Afterburner."
            )
            applied_value = applied
        else:
            applied_value = applied
            message = f"{_label(feature)} set to {value:g} and verified."
        return ControlResult(
            feature=feature,
            requested_value=value,
            applied_value=applied_value,
            clamped=False,
            applied=True,
            replaced_value=replaced,
            message=message,
        )

    def _raise_if_write_blocked_by_elevation(self) -> None:
        """Fail fast (ACCESS_DENIED) when Afterburner is elevated and this process is not.

        Elevated Afterburner never confirms MACM commands from non-elevated callers within
        the protocol's completion budget (observed live on 4.6.7.17439), so issuing the
        FLUSH would only burn the budget and leave a may-or-may-not-have-applied ambiguity.
        Called only when a write is about to happen — the Requirement 19.4 no-op path never
        reaches it, because a no-op needs no Afterburner action. Best-effort: when the
        comparison is undeterminable it returns and the completion poll's timeout error
        (INTERFACE_UNAVAILABLE) stands.
        """
        if _afterburner_elevation_mismatch():
            code, message, detail = flush_timeout_error(True)
            raise PluginError(code, message, detail=detail)

    def _transact_apply(
        self, view: _ControlMap, gpu_index: int, feature: ControlFeature, value: float
    ) -> AppliedOutcome:
        """Mutex-held read-modify-write of the named field; FLUSH set last."""
        with view.mutex():
            region = bytearray(view.read_bytes())
            outcome = apply_control_to_region(region, gpu_index, feature, value)
            if outcome.action == "applied":
                self._raise_if_write_blocked_by_elevation()
                view.write_words(outcome.word_offsets, bytes(region))
            return outcome

    def _wait_for_completion(self, view: _ControlMap) -> None:
        _notify_afterburner()
        deadline = self._clock() + self.flush_timeout
        while True:
            command = self._poll_command(view)
            if command == 0:
                return
            if command == 0xFFFFFFFF:
                raise PluginError(
                    ErrorCode.DISCONNECTED,
                    "MSI Afterburner closed its control interface before confirming the "
                    "change. Start it and verify the setting.",
                    detail="MACM map became unavailable during FLUSH completion",
                )
            if self._clock() >= deadline:
                mismatch = _afterburner_elevation_mismatch()
                code, message, detail = flush_timeout_error(mismatch)
                raise PluginError(code, message, detail=detail)
            time.sleep(self.poll_interval)

    def _poll_command(self, view: _ControlMap) -> int:
        """Read dwCommand under the mutex (sample's PollCommand pattern); 0xFFFFFFFF = gone."""
        with view.mutex():
            data = view.read_bytes()
            if len(data) < ctypes.sizeof(MACM_SHARED_MEMORY_HEADER):
                return 0xFFFFFFFF
            header = MACM_SHARED_MEMORY_HEADER.from_buffer_copy(
                data[: ctypes.sizeof(MACM_SHARED_MEMORY_HEADER)]
            )
            if header.dwSignature != MACM_SIGNATURE:
                return 0xFFFFFFFF
            return int(header.dwCommand)

    def _verified_apply_value(
        self, view: _ControlMap, gpu_index: int, feature: ControlFeature
    ) -> Optional[float]:
        with view.mutex():
            region = bytearray(view.read_bytes())
            parsed = validate_control_region(bytes(region))
            spec = _CONTROL_SPECS[feature]
            entry = _entry_view(region, parsed, gpu_index)
            if not (entry.dwFlags & spec.flag):
                return None
            return _to_model_units(int(getattr(entry, spec.cur)), spec)

    def reset_tuning(self, gpu_index: int) -> ControlResult:
        with _ControlMap() as view:  # map stays open through the completion poll
            with view.mutex():
                region = bytearray(view.read_bytes())
                outcome = reset_control_region(region, gpu_index)
                if outcome.action == "applied":
                    self._raise_if_write_blocked_by_elevation()
                    view.write_words(outcome.word_offsets, bytes(region))
            if outcome.action == "noop":
                return ControlResult(
                    feature=ControlFeature.PROFILE_RESET,
                    requested_value=None,
                    applied_value=None,
                    applied=False,
                    message="Tuning already at defaults — no change made.",
                )
            self._wait_for_completion(view)
        return ControlResult(
            feature=ControlFeature.PROFILE_RESET,
            requested_value=None,
            applied_value=None,
            applied=True,
            message="Tuning reset to defaults and verified.",
        )

    # ------------------------------------------- unsupported / out-of-scope
    def _monitoring_unavailable(self, what: str) -> PluginError:
        return PluginError(
            ErrorCode.INTERFACE_UNAVAILABLE,
            f"MSI Afterburner's monitoring data ({what}) is read from the MAHM interface, "
            "not the MACM control client.",
            detail="AfterburnerControlClient implements control only; monitoring is MAHM (plan 18)",
        )

    def read_telemetry(self, gpu_index: int) -> GpuTelemetry:
        raise self._monitoring_unavailable("telemetry")

    def read_all_telemetry(self) -> Sequence[GpuTelemetry]:
        raise self._monitoring_unavailable("telemetry")

    def list_profiles(self) -> Sequence[Profile]:
        raise PluginError(
            ErrorCode.INTERFACE_UNAVAILABLE,
            "Profiles are listed through the profile service's read-only scan of the "
            "Afterburner Profiles directory.",
            detail="MACM control client has no profile enumeration",
        )

    def load_profile(self, profile_id: int) -> ControlResult:
        raise PluginError(
            ErrorCode.INTERFACE_UNAVAILABLE,
            "Profile loading applies stored named controls through the profile service.",
            detail="MACM control client applies profiles at the profile-service layer",
        )

    def apply_fan_curve(self, gpu_index: int, curve: FanCurve) -> ControlResult:
        raise PluginError(
            ErrorCode.UNSUPPORTED_FEATURE,
            "That control isn't available on this hardware/version.",
            detail="a fan curve is not a MACM GPU-entry field; fan speed/auto is the only "
            "named fan control (design § MACM Control Write Sequence caveat)",
        )


def _label(feature: ControlFeature) -> str:
    return {
        ControlFeature.POWER_LIMIT: "Power limit",
        ControlFeature.CORE_OFFSET: "Core clock offset",
        ControlFeature.MEMORY_OFFSET: "Memory clock offset",
        ControlFeature.VOLTAGE: "Core voltage",
        ControlFeature.FAN_PERCENT: "Fan speed",
    }.get(feature, feature.value.replace("_", " ").title())
