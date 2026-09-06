"""Strongly-typed domain models and the error taxonomy.

Mirrors design.md "Data Models" and "Error Handling": every value that crosses the LLM boundary
is validated/clamped before it reaches the Afterburner interface; values are Afterburner-REPORTED
(never hardcoded per-GPU assumptions).

Tuning ownership: the GPU exposes ONE shared set of driver tuning parameters. External
authorities (NVIDIA App Automatic Tuning, G-Assist native tuning, other OC utilities) are never
observable through Afterburner interfaces and are always marked UNKNOWN_NOT_OBSERVABLE.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import FrozenSet, Optional, Tuple

# ---------------------------------------------------------------------------
# Availability & capabilities
# ---------------------------------------------------------------------------


class InterfaceStatus(str, Enum):
    OK = "ok"
    NOT_INSTALLED = "not_installed"  # Afterburner not found on disk
    NOT_RUNNING = "not_running"  # installed but process/shared-mem absent
    UNSUPPORTED_VERSION = "unsupported_version"
    ACCESS_DENIED = "access_denied"  # needs elevation
    UNAVAILABLE = "unavailable"  # interface present but non-functional


class ControlFeature(str, Enum):
    POWER_LIMIT = "power_limit"
    CORE_OFFSET = "core_offset"
    MEMORY_OFFSET = "memory_offset"
    VOLTAGE = "voltage"
    FAN_PERCENT = "fan_percent"
    FAN_CURVE = "fan_curve"
    PROFILE_LOAD = "profile_load"
    PROFILE_RESET = "profile_reset"


@dataclass(frozen=True)
class Range:
    min: float
    max: float

    def clamp(self, value: float) -> float:
        return max(self.min, min(self.max, value))

    def contains(self, value: float) -> bool:
        return self.min <= value <= self.max

    @property
    def inverted(self) -> bool:
        return self.max < self.min


@dataclass(frozen=True)
class GpuLimits:
    """Afterburner-REPORTED limits (never hardcoded per-GPU)."""

    power_limit_pct: Optional[Range] = None  # e.g. 50..120 (%)
    core_offset_mhz: Optional[Range] = None
    memory_offset_mhz: Optional[Range] = None
    voltage_mv: Optional[Range] = None
    fan_percent: Optional[Range] = None  # usually 0..100
    fan_temp_c: Optional[Range] = None  # valid temp axis for curve points
    fan_curve_max_points: Optional[int] = None  # 2 for the documented two-point model; >2 only
    # where the control interface reports wider support (max 32)

    def range_for(self, feature: ControlFeature) -> Optional[Range]:
        """Range for a single-value control feature (None for non-single-value features)."""
        mapping = {
            ControlFeature.POWER_LIMIT: self.power_limit_pct,
            ControlFeature.CORE_OFFSET: self.core_offset_mhz,
            ControlFeature.MEMORY_OFFSET: self.memory_offset_mhz,
            ControlFeature.VOLTAGE: self.voltage_mv,
            ControlFeature.FAN_PERCENT: self.fan_percent,
        }
        return mapping.get(feature)


@dataclass(frozen=True)
class GpuCapabilities:
    gpu_index: int
    gpu_name: str
    supported_controls: FrozenSet[ControlFeature]
    limits: GpuLimits
    control_interface_status: InterfaceStatus  # MACM availability for this GPU

    def supports(self, f: ControlFeature) -> bool:
        return f in self.supported_controls


# ---------------------------------------------------------------------------
# Telemetry & tuning state
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    """Timezone-aware UTC timestamp for sampled telemetry."""
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class GpuTelemetry:
    gpu_index: int
    gpu_name: str
    driver_version: Optional[str] = None
    temperature_c: Optional[float] = None
    hotspot_c: Optional[float] = None  # not exposed by Afterburner for NVIDIA GPUs
    utilization_pct: Optional[float] = None
    core_clock_mhz: Optional[float] = None
    memory_clock_mhz: Optional[float] = None
    voltage_mv: Optional[float] = None
    power_watts: Optional[float] = None
    power_limit_pct: Optional[float] = None
    fan_percent: Optional[float] = None
    fan_rpm: Optional[float] = None
    memory_used_mb: Optional[float] = None
    memory_total_mb: Optional[float] = None
    sampled_at: datetime = field(default_factory=_utcnow)
    is_stale: bool = False


@dataclass(frozen=True)
class TuningState:
    gpu_index: int
    power_limit_pct: Optional[float] = None
    core_offset_mhz: Optional[float] = None
    memory_offset_mhz: Optional[float] = None
    voltage_mv: Optional[float] = None
    fan_mode: str = "auto"  # "auto" | "manual" | "curve"
    fan_percent: Optional[float] = None


# ---------------------------------------------------------------------------
# Tuning ownership (single shared GPU tuning state)
# ---------------------------------------------------------------------------


class AuthorityState(str, Enum):
    OBSERVED = "observed"  # state actually read back via Afterburner interfaces
    UNKNOWN_NOT_OBSERVABLE = "unknown_not_observable"  # no Afterburner interface exposes it
    NOT_APPLICABLE = "not_applicable"  # Afterburner absent / interfaces down


@dataclass(frozen=True)
class AfterburnerAuthoritySnapshot:
    interface_status: InterfaceStatus
    applied_state: Optional[TuningState] = None
    active_profile_id: Optional[int] = None  # matched via read-back compare — never guessed
    startup_auto_apply_present: bool = False  # populated [Startup] section (read-only)


@dataclass(frozen=True)
class TuningOwnershipReport:
    """One shared tuning state; only Afterburner is observable through the plugin's interfaces."""

    gpu_index: int
    afterburner: AfterburnerAuthoritySnapshot
    nvidia_app_auto_tuning: AuthorityState = AuthorityState.UNKNOWN_NOT_OBSERVABLE
    gassist_native_tuning: AuthorityState = AuthorityState.UNKNOWN_NOT_OBSERVABLE
    other_oc_utilities: AuthorityState = AuthorityState.UNKNOWN_NOT_OBSERVABLE
    summary: str = ""  # NL text warning external authorities cannot be ruled out


# ---------------------------------------------------------------------------
# Profiles & fan curves
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Profile:
    id: int  # hardware profile slot 1..5 (per-GPU)
    name: str  # derived label "Profile N" (Afterburner stores no names)
    is_active: bool = False  # set only via control-state read-back match — never guessed
    kind: str = "hardware"  # "hardware" | "user" ("user" reserved)
    summary: str = ""  # stored OC/fan/voltage settings, NL; never the VF hex blob
    nickname: Optional[str] = None  # plugin-local label; never stored in Afterburner


@dataclass(frozen=True)
class FanCurvePoint:
    temp_c: float
    fan_percent: float


@dataclass(frozen=True)
class FanCurve:
    points: Tuple[FanCurvePoint, ...]


# ---------------------------------------------------------------------------
# Commands, results, errors
# ---------------------------------------------------------------------------


class RiskLevel(str, Enum):
    LOW = "low"  # no confirmation (reads, reset_tuning, load_profile)
    HIGH = "high"  # requires explicit confirmation


@dataclass(frozen=True)
class ControlResult:
    feature: ControlFeature
    requested_value: Optional[float]
    applied_value: Optional[float]  # after clamping; None if not applied
    clamped: bool = False
    applied: bool = False
    replaced_value: Optional[float] = None  # applied value this change replaced (write-time)
    message: str = ""


class ErrorCode(str, Enum):
    NOT_INSTALLED = "afterburner_not_installed"
    NOT_RUNNING = "afterburner_not_running"
    UNSUPPORTED_VERSION = "unsupported_version"
    UNSUPPORTED_GPU = "unsupported_gpu"
    INTERFACE_UNAVAILABLE = "interface_unavailable"
    ACCESS_DENIED = "access_denied"
    INVALID_VALUE = "invalid_tuning_value"
    COMM_FAILURE = "communication_failure"
    STALE_TELEMETRY = "stale_telemetry"
    UNSUPPORTED_FEATURE = "unsupported_feature"
    DISCONNECTED = "afterburner_disconnecting"
    CONFIRMATION_REQUIRED = "confirmation_required"


@dataclass(frozen=True)
class PluginError(Exception):
    code: ErrorCode
    user_message: str  # NL-friendly, returned to G-Assist
    detail: str = ""  # internal / log only


class DiagnosisCause(str, Enum):
    THERMAL = "thermal"
    POWER = "power"
    VOLTAGE = "voltage"
    UTILIZATION_BOTTLENECK = "utilization_bottleneck"
    CPU_LIMITED = "cpu_limited"
    APP_BEHAVIOR = "app_behavior"
    UNKNOWN_INSUFFICIENT_DATA = "unknown_insufficient_data"


@dataclass(frozen=True)
class Diagnosis:
    cause: DiagnosisCause
    confidence: float  # 0.0..1.0 — never overclaim
    evidence: Tuple[str, ...]  # human-readable supporting facts (field name + value)
    summary: str
