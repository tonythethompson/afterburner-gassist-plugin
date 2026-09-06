"""`AfterburnerMonitoringClient` — official read-only MAHM monitoring interface.

One of the three OS-touching modules (MAHM monitoring, MACM control, and the read-only
profile-file reader; plan task 18.1). Reads MSI Afterburner's official hardware-monitoring
shared memory (`MAHMSharedMemory`, v2.0 `0x00020000`, `'MAHM'` signature — layout verified
against the shipped `SDK\\Include\\MAHMSharedMemory.h`) via `ctypes`/`kernel32`.

Hard rules:

- **Read-only.** The map is opened with `FILE_MAP_READ` (0x4) only; the module never
  requests write or all-access mapping rights. Nothing in this module writes to the map.
- **Validate before use.** Every read validates the live header: a deallocated map
  (`0xDEAD` marker), a wrong signature, an unsupported interface version, zero/oversized
  entry sizes, and a declared region that exceeds the mapped size are all rejected with a
  typed `PluginError` — never a crash, never a fabricated value.
- **Pure parsing.** `parse_mahm(bytes)` performs all validation/layout against an injected
  byte buffer so unit tests drive every error path without a real Afterburner install; the
  thin OS layer only maps the file read-only and copies bytes.

The bindings are checked against the committed layout fixtures
(`tests/fixtures/sdk_layouts/mahm_layout.json`, plan tasks 18.3/18.4) and the live installed
header via `tools/generate_sdk_layout_fixtures.py --diff`.

This client implements the *monitoring* parts of `AfterburnerInterface`; control reads/writes
(MACM) are out of scope here and are answered with an honest typed `INTERFACE_UNAVAILABLE`
so the plugin degrades gracefully instead of fabricating tuning state.
"""
from __future__ import annotations

import ctypes
import math
import os
import struct
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
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
    TuningState,
)
from .interface import AfterburnerInterface

# ---------------------------------------------------------------------------
# Shared-memory contract (verified against the shipped v2.0 header)
# ---------------------------------------------------------------------------

MAHM_MAP_NAME = "MAHMSharedMemory"
MAHM_SIGNATURE = 0x4D41484D  # 'MAHM' as a little-endian DWORD
MAHM_DEAD_SIGNATURE = 0x0000DEAD  # map marked for deallocation
MAHM_VERSION = 0x00020000  # interface version (major << 16) + minor
MAHM_SOURCE_ID_GLOBAL = 0xFFFFFFFF  # dwGpu value for global data sources

FILE_MAP_READ = 0x0004
_ERROR_FILE_NOT_FOUND = 2
_ERROR_ACCESS_DENIED = 5

# Hard sanity ceilings so a corrupted header can never cause a huge allocation/copy.
_MAX_ENTRIES = 65536
_MAX_GPU_ENTRIES = 1024
_FLT_UNAVAILABLE_THRESHOLD = 1.0e30  # header: data == FLT_MAX when not available


class MAHM_SHARED_MEMORY_HEADER(ctypes.Structure):
    """v2.0 header (size 32): 4-byte fields; `time` is the documented 32-bit time."""

    _fields_ = [
        ("dwSignature", ctypes.c_uint32),
        ("dwVersion", ctypes.c_uint32),
        ("dwHeaderSize", ctypes.c_uint32),
        ("dwNumEntries", ctypes.c_uint32),
        ("dwEntrySize", ctypes.c_uint32),
        ("time", ctypes.c_int32),
        ("dwNumGpuEntries", ctypes.c_uint32),
        ("dwGpuEntrySize", ctypes.c_uint32),
    ]


class MAHM_SHARED_MEMORY_ENTRY(ctypes.Structure):
    """v2.0 monitoring entry (size 1324): five 260-byte char arrays then float/DWORD tail."""

    _fields_ = [
        ("szSrcName", ctypes.c_char * 260),
        ("szSrcUnits", ctypes.c_char * 260),
        ("szLocalizedSrcName", ctypes.c_char * 260),
        ("szLocalizedSrcUnits", ctypes.c_char * 260),
        ("szRecommendedFormat", ctypes.c_char * 260),
        ("data", ctypes.c_float),
        ("minLimit", ctypes.c_float),
        ("maxLimit", ctypes.c_float),
        ("dwFlags", ctypes.c_uint32),
        ("dwGpu", ctypes.c_uint32),
        ("dwSrcId", ctypes.c_uint32),
    ]


class MAHM_SHARED_MEMORY_GPU_ENTRY(ctypes.Structure):
    """v2.0 per-GPU entry (size 1304): five 260-byte char arrays then a DWORD tail."""

    _fields_ = [
        ("szGpuId", ctypes.c_char * 260),
        ("szFamily", ctypes.c_char * 260),
        ("szDevice", ctypes.c_char * 260),
        ("szDriver", ctypes.c_char * 260),
        ("szBIOS", ctypes.c_char * 260),
        ("dwMemAmount", ctypes.c_uint32),
    ]


def _cstr(raw: bytes) -> str:
    """Decode a NUL-terminated char buffer, replacing undecodable bytes."""
    return raw.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()


# ---------------------------------------------------------------------------
# Parsed snapshot models (pure; built from an injected byte buffer)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MahmEntry:
    """One MAHM_SHARED_MEMORY_ENTRY (a single monitored data source sample)."""

    name: str
    units: str
    data: float
    flags: int
    gpu: int  # dwGpu; MAHM_SOURCE_ID_GLOBAL for global sources
    src_id: int


@dataclass(frozen=True)
class MahmGpu:
    """One MAHM_SHARED_MEMORY_GPU_ENTRY plus the sources sampled for it."""

    index: int
    gpu_id: str  # VEN_…&FN_%d encoding that names the per-GPU profile files
    family: str
    device: str
    driver: str
    bios: str
    mem_amount_kb: int
    entries: Tuple[MahmEntry, ...] = ()


@dataclass(frozen=True)
class MahmSnapshot:
    """Validated, parsed contents of the MAHM shared-memory region."""

    version: int
    header_size: int
    last_poll_unix: int
    gpus: Tuple[MahmGpu, ...]
    sources: Tuple[MahmEntry, ...]  # every entry (per-GPU and global) in map order


def _unavailable(message: str, detail: str) -> PluginError:
    return PluginError(ErrorCode.INTERFACE_UNAVAILABLE, message, detail=detail)


def parse_mahm(data: bytes) -> MahmSnapshot:
    """Validate and parse a MAHM shared-memory region from an injected byte buffer.

    Raises typed `PluginError`s for every invalid-header case — deallocated map, wrong
    signature, unsupported version, zero/oversized entry sizes, and a declared region
    exceeding the buffer — and never crashes on malformed input.
    """
    if len(data) < ctypes.sizeof(MAHM_SHARED_MEMORY_HEADER):
        raise _unavailable(
            "MSI Afterburner's monitoring data is incomplete right now. Try again shortly.",
            f"region smaller than MAHM header ({len(data)} bytes)",
        )
    header = MAHM_SHARED_MEMORY_HEADER.from_buffer_copy(data[: ctypes.sizeof(MAHM_SHARED_MEMORY_HEADER)])

    if header.dwSignature == MAHM_DEAD_SIGNATURE:
        raise PluginError(
            ErrorCode.NOT_RUNNING,
            "MSI Afterburner isn't running. Start it and try again.",
            detail="MAHM map marked for deallocation (0xDEAD)",
        )
    if header.dwSignature != MAHM_SIGNATURE:
        raise _unavailable(
            "MSI Afterburner's monitoring interface isn't initialized yet. Start it and try again.",
            f"bad MAHM signature 0x{header.dwSignature:08x}",
        )
    if header.dwVersion != MAHM_VERSION:
        raise PluginError(
            ErrorCode.UNSUPPORTED_VERSION,
            "Your Afterburner version doesn't expose the needed interface. Please update.",
            detail=f"MAHM version 0x{header.dwVersion:08x}, expected 0x{MAHM_VERSION:08x}",
        )
    if header.dwHeaderSize < ctypes.sizeof(MAHM_SHARED_MEMORY_HEADER):
        raise _unavailable(
            "MSI Afterburner's monitoring data is malformed right now. Try again shortly.",
            f"dwHeaderSize {header.dwHeaderSize} < header struct {ctypes.sizeof(MAHM_SHARED_MEMORY_HEADER)}",
        )
    if header.dwNumEntries > _MAX_ENTRIES or header.dwNumGpuEntries > _MAX_GPU_ENTRIES:
        raise _unavailable(
            "MSI Afterburner's monitoring data is malformed right now. Try again shortly.",
            f"implausible entry counts ({header.dwNumEntries}/{header.dwNumGpuEntries})",
        )

    entry_size = ctypes.sizeof(MAHM_SHARED_MEMORY_ENTRY)
    gpu_entry_size = ctypes.sizeof(MAHM_SHARED_MEMORY_GPU_ENTRY)
    if header.dwNumEntries > 0:
        if header.dwEntrySize == 0:
            raise _unavailable(
                "MSI Afterburner's monitoring data is malformed right now. Try again shortly.",
                "dwEntrySize is zero with entries present",
            )
        if header.dwEntrySize != entry_size:
            raise PluginError(
                ErrorCode.UNSUPPORTED_VERSION,
                "Your Afterburner version doesn't expose the needed interface. Please update.",
                detail=f"dwEntrySize {header.dwEntrySize} != binding size {entry_size}",
            )
    if header.dwNumGpuEntries > 0:
        if header.dwGpuEntrySize == 0:
            raise _unavailable(
                "MSI Afterburner's monitoring data is malformed right now. Try again shortly.",
                "dwGpuEntrySize is zero with GPU entries present",
            )
        if header.dwGpuEntrySize != gpu_entry_size:
            raise PluginError(
                ErrorCode.UNSUPPORTED_VERSION,
                "Your Afterburner version doesn't expose the needed interface. Please update.",
                detail=f"dwGpuEntrySize {header.dwGpuEntrySize} != binding size {gpu_entry_size}",
            )

    main_offset = header.dwHeaderSize
    gpu_offset = main_offset + header.dwNumEntries * header.dwEntrySize
    region_end = gpu_offset + header.dwNumGpuEntries * header.dwGpuEntrySize
    if region_end > len(data):
        raise _unavailable(
            "MSI Afterburner's monitoring data is incomplete right now. Try again shortly.",
            f"declared region {region_end} exceeds mapped size {len(data)}",
        )

    entries: List[MahmEntry] = []
    for i in range(header.dwNumEntries):
        start = main_offset + i * header.dwEntrySize
        raw = MAHM_SHARED_MEMORY_ENTRY.from_buffer_copy(
            data[start : start + header.dwEntrySize]
        )
        entries.append(
            MahmEntry(
                name=_cstr(raw.szSrcName),
                units=_cstr(raw.szSrcUnits),
                data=float(raw.data),
                flags=int(raw.dwFlags),
                gpu=int(raw.dwGpu),
                src_id=int(raw.dwSrcId),
            )
        )
    all_entries = tuple(entries)

    gpus: List[MahmGpu] = []
    for i in range(header.dwNumGpuEntries):
        start = gpu_offset + i * header.dwGpuEntrySize
        raw = MAHM_SHARED_MEMORY_GPU_ENTRY.from_buffer_copy(
            data[start : start + header.dwGpuEntrySize]
        )
        per_gpu = tuple(e for e in all_entries if e.gpu == i)
        gpus.append(
            MahmGpu(
                index=i,
                gpu_id=_cstr(raw.szGpuId),
                family=_cstr(raw.szFamily),
                device=_cstr(raw.szDevice),
                driver=_cstr(raw.szDriver),
                bios=_cstr(raw.szBIOS),
                mem_amount_kb=int(raw.dwMemAmount),
                entries=per_gpu,
            )
        )
    return MahmSnapshot(
        version=header.dwVersion,
        header_size=header.dwHeaderSize,
        last_poll_unix=header.time,
        gpus=tuple(gpus),
        sources=all_entries,
    )


# ---------------------------------------------------------------------------
# Telemetry mapping: monitoring source ID -> model field (primary + fallbacks).
# Units are Afterburner-reported (clocks MHz, voltage mV, power W/%); the source IDs are the
# header-documented MONITORING_SOURCE_ID_* constants recorded in the layout fixture.
# ---------------------------------------------------------------------------

_SOURCE_TO_FIELD: Tuple[Tuple[str, Tuple[int, ...]], ...] = (
    ("temperature_c", (0x00000000,)),  # MONITORING_SOURCE_ID_GPU_TEMPERATURE
    ("utilization_pct", (0x00000030,)),  # MONITORING_SOURCE_ID_GPU_USAGE
    ("core_clock_mhz", (0x00000020,)),  # MONITORING_SOURCE_ID_CORE_CLOCK
    ("memory_clock_mhz", (0x00000022,)),  # MONITORING_SOURCE_ID_MEMORY_CLOCK
    ("voltage_mv", (0x00000040,)),  # MONITORING_SOURCE_ID_GPU_VOLTAGE
    ("power_watts", (0x00000061,)),  # MONITORING_SOURCE_ID_GPU_ABS_POWER
    ("power_limit_pct", (0x00000071,)),  # MONITORING_SOURCE_ID_GPU_POWER_LIMIT
    ("fan_percent", (0x00000010, 0x00000012, 0x00000014)),  # FAN_SPEED[_2|_3]
    ("fan_rpm", (0x00000011, 0x00000013, 0x00000015)),  # FAN_TACHOMETER[_2|_3]
    ("memory_used_mb", (0x00000031,)),  # MONITORING_SOURCE_ID_MEMORY_USAGE
)


def _available(data: float) -> Optional[float]:
    if not math.isfinite(data) or abs(data) >= _FLT_UNAVAILABLE_THRESHOLD:
        return None
    return float(data)


def telemetry_from_snapshot(
    snapshot: MahmSnapshot, gpu_index: int
) -> GpuTelemetry:
    """Build one typed `GpuTelemetry` sample for a GPU from a parsed snapshot."""
    if gpu_index < 0 or gpu_index >= len(snapshot.gpus):
        raise PluginError(
            ErrorCode.UNSUPPORTED_GPU,
            f"GPU index {gpu_index} isn't available.",
            detail=f"MAHM reports {len(snapshot.gpus)} GPU(s)",
        )
    gpu = snapshot.gpus[gpu_index]
    values: Dict[str, Optional[float]] = {}
    for field_name, candidate_ids in _SOURCE_TO_FIELD:
        for src_id in candidate_ids:
            match = next((e for e in gpu.entries if e.src_id == src_id), None)
            if match is not None:
                values[field_name] = _available(match.data)
                break

    memory_total_mb = None
    if gpu.mem_amount_kb > 0:
        memory_total_mb = gpu.mem_amount_kb / 1024.0
    return GpuTelemetry(
        gpu_index=gpu_index,
        gpu_name=gpu.device or gpu.family or gpu.gpu_id or f"GPU {gpu_index}",
        driver_version=gpu.driver or None,
        temperature_c=values.get("temperature_c"),
        utilization_pct=values.get("utilization_pct"),
        core_clock_mhz=values.get("core_clock_mhz"),
        memory_clock_mhz=values.get("memory_clock_mhz"),
        voltage_mv=values.get("voltage_mv"),
        power_watts=values.get("power_watts"),
        power_limit_pct=values.get("power_limit_pct"),
        fan_percent=values.get("fan_percent"),
        fan_rpm=values.get("fan_rpm"),
        memory_used_mb=values.get("memory_used_mb"),
        memory_total_mb=memory_total_mb,
        sampled_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Install detection (shared well-known paths + env override)
# ---------------------------------------------------------------------------

_STANDARD_INSTALL_DIRS = (
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "MSI Afterburner",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "MSI Afterburner",
)


def find_install_dir(override: Optional[str] = None) -> Optional[Path]:
    """Locate the Afterburner install (env override first, then well-known paths)."""
    candidates = []
    if override:
        candidates.append(Path(override))
    env = os.environ.get("MSI_AFTERBURNER_INSTALL_DIR")
    if env:
        candidates.append(Path(env))
    candidates.extend(_STANDARD_INSTALL_DIRS)
    for candidate in candidates:
        if (candidate / "MSIAfterburner.exe").is_file():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Read-only OS mapping
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


class _ReadOnlyView:
    """A FILE_MAP_READ view of the MAHM mapping, closed on context exit."""

    def __init__(self) -> None:
        if os.name != "nt":  # pragma: no cover - exercised only off-Windows
            raise PluginError(
                ErrorCode.INTERFACE_UNAVAILABLE,
                "MSI Afterburner only runs on Windows.",
                detail="MAHM shared memory requires Windows",
            )
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # Explicit prototypes: without these, 64-bit HANDLE/PVOID results are truncated to
        # 32 bits by ctypes' default c_int restype and pointer args are misread.
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
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
        kernel32.UnmapViewOfFile.restype = wintypes.BOOL

        self._kernel32 = kernel32
        self._handle = None
        self._addr = None
        self._region_size = 0

        handle = kernel32.OpenFileMappingW(FILE_MAP_READ, False, MAHM_MAP_NAME)
        if not handle:
            self._raise_open_error()
        self._handle = handle
        try:
            addr = kernel32.MapViewOfFile(handle, FILE_MAP_READ, 0, 0, 0)
            if not addr:
                self._raise_map_error()
            self._addr = addr
            info = _MEMORY_BASIC_INFORMATION()
            queried = kernel32.VirtualQuery(
                addr, ctypes.byref(info), ctypes.sizeof(info)
            )
            if not queried:
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
                detail=f"MAHM map '{MAHM_MAP_NAME}' not found (error {err})",
            )
        if err == _ERROR_ACCESS_DENIED:
            raise PluginError(
                ErrorCode.ACCESS_DENIED,
                "Reading Afterburner's monitoring data needs elevated permissions.",
                detail=f"OpenFileMappingW access denied (error {err})",
            )
        raise PluginError(
            ErrorCode.INTERFACE_UNAVAILABLE,
            "MSI Afterburner's monitoring interface isn't reachable right now.",
            detail=f"OpenFileMappingW failed (error {err})",
        )

    def _raise_map_error(self) -> None:
        err = ctypes.get_last_error()
        if err == _ERROR_ACCESS_DENIED:
            raise PluginError(
                ErrorCode.ACCESS_DENIED,
                "Reading Afterburner's monitoring data needs elevated permissions.",
                detail=f"MapViewOfFile access denied (error {err})",
            )
        raise PluginError(
            ErrorCode.INTERFACE_UNAVAILABLE,
            "MSI Afterburner's monitoring interface isn't reachable right now.",
            detail=f"MapViewOfFile/VirtualQuery failed (error {err})",
        )

    def read_region(self) -> bytes:
        """Copy the mapped region into Python bytes (bounded by the view's region size)."""
        assert self._addr is not None
        size = self._region_size
        return ctypes.string_at(self._addr, size)

    def close(self) -> None:
        kernel32 = self._kernel32
        if self._addr:
            kernel32.UnmapViewOfFile(self._addr)
            self._addr = None
        if self._handle:
            kernel32.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self) -> "_ReadOnlyView":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _snapshot_from_map(view: _ReadOnlyView) -> MahmSnapshot:
    """Read the view header, then copy exactly the declared region and parse it."""
    region = view.read_region()
    if len(region) < ctypes.sizeof(MAHM_SHARED_MEMORY_HEADER):
        raise _unavailable(
            "MSI Afterburner's monitoring data is incomplete right now. Try again shortly.",
            f"mapped view smaller than the MAHM header ({len(region)} bytes)",
        )
    header = MAHM_SHARED_MEMORY_HEADER.from_buffer_copy(region[: ctypes.sizeof(MAHM_SHARED_MEMORY_HEADER)])
    if header.dwSignature == MAHM_DEAD_SIGNATURE:
        raise PluginError(
            ErrorCode.NOT_RUNNING,
            "MSI Afterburner isn't running. Start it and try again.",
            detail="MAHM map marked for deallocation (0xDEAD)",
        )
    if header.dwSignature != MAHM_SIGNATURE:
        raise _unavailable(
            "MSI Afterburner's monitoring interface isn't initialized yet. Start it and try again.",
            f"bad MAHM signature 0x{header.dwSignature:08x}",
        )
    if header.dwNumEntries > _MAX_ENTRIES or header.dwNumGpuEntries > _MAX_GPU_ENTRIES:
        raise _unavailable(
            "MSI Afterburner's monitoring data is malformed right now. Try again shortly.",
            f"implausible entry counts ({header.dwNumEntries}/{header.dwNumGpuEntries})",
        )
    try:
        region_end = (
            header.dwHeaderSize
            + header.dwNumEntries * header.dwEntrySize
            + header.dwNumGpuEntries * header.dwGpuEntrySize
        )
    except (OverflowError, ValueError):  # pragma: no cover - defensive
        raise _unavailable(
            "MSI Afterburner's monitoring data is malformed right now. Try again shortly.",
            "declared region size overflow",
        )
    if region_end > len(region):
        raise _unavailable(
            "MSI Afterburner's monitoring data is incomplete right now. Try again shortly.",
            f"declared region {region_end} exceeds mapped size {len(region)}",
        )
    if region_end < ctypes.sizeof(MAHM_SHARED_MEMORY_HEADER):
        region_end = ctypes.sizeof(MAHM_SHARED_MEMORY_HEADER)
    return parse_mahm(region[:region_end])


# ---------------------------------------------------------------------------
# The AfterburnerInterface implementation (monitoring parts)
# ---------------------------------------------------------------------------


class AfterburnerMonitoringClient:
    """Read-only MAHM monitoring adapter implementing the monitoring parts of the interface.

    The control/state parts (`read_capabilities`, `read_tuning_state`, profile application,
    and every control write) require the MACM control interface (plan task 20) and are
    answered here with an honest typed `INTERFACE_UNAVAILABLE` — never fabricated values.
    """

    def __init__(self, install_dir: Optional[os.PathLike] = None) -> None:
        self.install_dir = find_install_dir(str(install_dir)) if install_dir else find_install_dir()
        self._version_cache: Optional[str] = None

    # -------------------------------------------------------------- lifecycle
    def detect(self) -> InterfaceStatus:
        if self.install_dir is None:
            return InterfaceStatus.NOT_INSTALLED
        try:
            with _ReadOnlyView() as view:
                _snapshot_from_map(view)
            return InterfaceStatus.OK
        except PluginError as exc:
            mapping = {
                ErrorCode.NOT_RUNNING: InterfaceStatus.NOT_RUNNING,
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

    # ---------------------------------------------------------------- reads
    def _snapshot(self) -> MahmSnapshot:
        with _ReadOnlyView() as view:
            return _snapshot_from_map(view)

    def read_telemetry(self, gpu_index: int) -> GpuTelemetry:
        return telemetry_from_snapshot(self._snapshot(), gpu_index)

    def read_all_telemetry(self) -> Sequence[GpuTelemetry]:
        snapshot = self._snapshot()
        return tuple(telemetry_from_snapshot(snapshot, i) for i in range(len(snapshot.gpus)))

    # -------------------------------------------------- control (out of scope)
    def _control_unavailable(self, what: str) -> PluginError:
        return PluginError(
            ErrorCode.INTERFACE_UNAVAILABLE,
            "MSI Afterburner's control interface isn't available right now — "
            f"{what} can't be applied. Start MSI Afterburner and try again.",
            detail="AfterburnerMonitoringClient is read-only (MAHM); control needs MACM (plan task 20)",
        )

    def read_capabilities(self, gpu_index: int) -> GpuCapabilities:
        raise self._control_unavailable("tuning controls")

    def read_tuning_state(self, gpu_index: int) -> TuningState:
        raise self._control_unavailable("tuning state")

    def list_profiles(self) -> Sequence[Profile]:
        raise self._control_unavailable("profiles")

    def load_profile(self, profile_id: int) -> ControlResult:
        raise self._control_unavailable("profiles")

    def reset_tuning(self, gpu_index: int) -> ControlResult:
        raise self._control_unavailable("tuning resets")

    def apply_control(
        self, gpu_index: int, feature: ControlFeature, value: float
    ) -> ControlResult:
        raise self._control_unavailable("tuning controls")

    def apply_fan_curve(self, gpu_index: int, curve: FanCurve) -> ControlResult:
        raise self._control_unavailable("fan curves")


def _file_version(exe: Path) -> Optional[str]:
    """FileVersion of an exe via the Win32 version resource; None when unavailable."""
    if os.name != "nt" or not exe.is_file():
        return None
    try:
        kernel32 = ctypes.WinDLL("version", use_last_error=False)
        size_fn = kernel32.GetFileVersionInfoSizeW
        size_fn.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        size_fn.restype = wintypes.DWORD
        info_fn = kernel32.GetFileVersionInfoW
        info_fn.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
        info_fn.restype = wintypes.BOOL
        query_fn = kernel32.VerQueryValueW
        query_fn.argtypes = [
            ctypes.c_void_p,
            wintypes.LPCWSTR,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.UINT),
        ]
        query_fn.restype = wintypes.BOOL
        _reserved = wintypes.DWORD()
        size = size_fn(str(exe), ctypes.byref(_reserved))
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not info_fn(str(exe), 0, size, buf):
            return None
        ptr = ctypes.c_void_p()
        length = wintypes.UINT()
        if not query_fn(buf, "\\", ctypes.byref(ptr), ctypes.byref(length)):
            return None
        hi, lo = struct.unpack_from("<II", ctypes.string_at(ptr.value, 16), 8)
        return f"{hi >> 16}.{hi & 0xFFFF}.{lo >> 16}.{lo & 0xFFFF}"
    except Exception:  # pragma: no cover - defensive: version display is best-effort
        return None
