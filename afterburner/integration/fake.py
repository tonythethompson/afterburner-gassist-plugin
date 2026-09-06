"""`FakeAfterburner` — in-memory `AfterburnerInterface` for tests.

Configurable detect status, telemetry (per GPU, including missing/null fields), capability
sets/limits, profiles, tuning state and control results. Every control call is recorded so
tests can assert whether a write occurred (never via a generic memory-writer).

Simulates unavailability modes:
- not installed / not running / unsupported version / access denied: change `status`; read
  operations then raise a matching typed `PluginError`.
- disconnected mid-op: raise inside `read_telemetry` / `apply_control` via `read_errors` /
  `apply_errors`.
- a stalled MAHM update counter: a frozen `telemetry` instance reused across polls (values
  never change) — services detect the stall.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ..models import (
    ControlFeature,
    ControlResult,
    FanCurve,
    GpuCapabilities,
    GpuLimits,
    GpuTelemetry,
    InterfaceStatus,
    PluginError,
    Profile,
    Range,
    TuningState,
    ErrorCode,
)
from .interface import AfterburnerInterface

DEFAULT_LIMITS = GpuLimits(
    power_limit_pct=Range(50.0, 118.0),
    core_offset_mhz=Range(-300.0, 300.0),
    memory_offset_mhz=Range(-500.0, 1000.0),
    voltage_mv=Range(0.0, 1100.0),
    fan_percent=Range(0.0, 100.0),
    fan_temp_c=Range(30.0, 90.0),
    fan_curve_max_points=2,
)


def default_capabilities(
    gpu_index: int = 0,
    gpu_name: str = "Fake GPU",
    *,
    supported: Tuple[ControlFeature, ...] = (
        ControlFeature.POWER_LIMIT,
        ControlFeature.CORE_OFFSET,
        ControlFeature.MEMORY_OFFSET,
        ControlFeature.VOLTAGE,
        ControlFeature.FAN_PERCENT,
        ControlFeature.FAN_CURVE,
        ControlFeature.PROFILE_LOAD,
        ControlFeature.PROFILE_RESET,
    ),
    limits: Optional[GpuLimits] = None,
    control_interface_status: InterfaceStatus = InterfaceStatus.OK,
) -> GpuCapabilities:
    return GpuCapabilities(
        gpu_index=gpu_index,
        gpu_name=gpu_name,
        supported_controls=frozenset(supported),
        limits=limits if limits is not None else DEFAULT_LIMITS,
        control_interface_status=control_interface_status,
    )


@dataclass
class FakeAfterburner:
    """Fully in-memory AfterburnerInterface with call recording."""

    status: InterfaceStatus = InterfaceStatus.OK
    version: Optional[str] = "4.6.7.17439"

    telemetry_by_gpu: Dict[int, GpuTelemetry] = field(default_factory=dict)
    caps_by_gpu: Dict[int, GpuCapabilities] = field(default_factory=dict)
    tuning_by_gpu: Dict[int, TuningState] = field(default_factory=dict)
    profiles: List[Profile] = field(default_factory=list)

    # Unavailability-mode simulation
    read_errors: Dict[int, Exception] = field(default_factory=dict)  # gpu -> raise on telemetry
    apply_errors: Dict[int, Exception] = field(default_factory=dict)  # gpu -> raise on control
    telemetry_fail_all: bool = False

    # Call recording — tests assert whether a write occurred
    detect_calls: int = field(default=0)
    telemetry_calls: Dict[int, int] = field(default_factory=dict)
    applied: List[Tuple[int, ControlFeature, float]] = field(default_factory=list)
    applied_fan_curves: List[Tuple[int, FanCurve]] = field(default_factory=list)
    applied_fan_auto: List[int] = field(default_factory=list)
    profile_loads: List[int] = field(default_factory=list)
    resets: List[int] = field(default_factory=list)

    # Optional overrides for control outcomes
    on_apply_control: Optional[
        Callable[[int, ControlFeature, float], ControlResult]
    ] = None
    on_apply_fan_curve: Optional[Callable[[int, FanCurve], ControlResult]] = None

    # ------------------------------------------------------------------ helpers
    def set_telemetry(self, telemetry: GpuTelemetry, gpu_index: Optional[int] = None) -> None:
        idx = telemetry.gpu_index if gpu_index is None else gpu_index
        self.telemetry_by_gpu[idx] = telemetry

    def set_capabilities(self, caps: GpuCapabilities, gpu_index: Optional[int] = None) -> None:
        idx = caps.gpu_index if gpu_index is None else gpu_index
        self.caps_by_gpu[idx] = caps

    def set_tuning(self, state: TuningState, gpu_index: Optional[int] = None) -> None:
        idx = state.gpu_index if gpu_index is None else gpu_index
        self.tuning_by_gpu[idx] = state

    def add_profile(self, profile_id: int, *, is_active: bool = False) -> Profile:
        profile = Profile(id=profile_id, name=f"Profile {profile_id}", is_active=is_active)
        self.profiles.append(profile)
        return profile

    # ------------------------------------------------------------- interface
    def detect(self) -> InterfaceStatus:
        self.detect_calls += 1
        return self.status

    def get_version(self) -> Optional[str]:
        return self.version

    def _ensure_ok(self) -> None:
        if self.status is not InterfaceStatus.OK:
            if self.status is InterfaceStatus.NOT_INSTALLED:
                raise PluginError(
                    ErrorCode.NOT_INSTALLED, "MSI Afterburner doesn't appear to be installed."
                )
            if self.status is InterfaceStatus.NOT_RUNNING:
                raise PluginError(
                    ErrorCode.NOT_RUNNING,
                    "MSI Afterburner isn't running. Start it and try again.",
                )
            if self.status is InterfaceStatus.UNSUPPORTED_VERSION:
                raise PluginError(
                    ErrorCode.UNSUPPORTED_VERSION,
                    "Your Afterburner version doesn't expose the needed interface. Please update.",
                )
            if self.status is InterfaceStatus.ACCESS_DENIED:
                raise PluginError(
                    ErrorCode.ACCESS_DENIED,
                    "Controlling Afterburner needs elevated permissions.",
                )
            raise PluginError(
                ErrorCode.INTERFACE_UNAVAILABLE,
                "Afterburner is installed but its interface is unavailable.",
            )

    def read_telemetry(self, gpu_index: int) -> GpuTelemetry:
        self._ensure_ok()
        if self.telemetry_fail_all:
            raise PluginError(
                ErrorCode.COMM_FAILURE,
                "I couldn't read from Afterburner just now. Try again in a moment.",
            )
        err = self.read_errors.get(gpu_index)
        if err is not None:
            raise err
        self.telemetry_calls[gpu_index] = self.telemetry_calls.get(gpu_index, 0) + 1
        if gpu_index not in self.telemetry_by_gpu:
            raise PluginError(
                ErrorCode.UNSUPPORTED_GPU,
                f"GPU index {gpu_index} isn't available.",
            )
        return self.telemetry_by_gpu[gpu_index]

    def read_all_telemetry(self) -> Sequence[GpuTelemetry]:
        self._ensure_ok()
        if self.telemetry_fail_all:
            raise PluginError(
                ErrorCode.COMM_FAILURE,
                "I couldn't read from Afterburner just now. Try again in a moment.",
            )
        return tuple(self.telemetry_by_gpu.values())

    def read_capabilities(self, gpu_index: int) -> GpuCapabilities:
        self._ensure_ok()
        caps = self.caps_by_gpu.get(gpu_index)
        if caps is None:
            raise PluginError(
                ErrorCode.UNSUPPORTED_GPU,
                f"GPU index {gpu_index} isn't available.",
            )
        return caps

    def read_tuning_state(self, gpu_index: int) -> TuningState:
        self._ensure_ok()
        state = self.tuning_by_gpu.get(gpu_index)
        if state is None:
            raise PluginError(
                ErrorCode.UNSUPPORTED_GPU,
                f"GPU index {gpu_index} isn't available.",
            )
        return state

    # ----------------------------------------------------------------- profiles
    def list_profiles(self) -> Sequence[Profile]:
        self._ensure_ok()
        return tuple(self.profiles)

    def load_profile(self, profile_id: int) -> ControlResult:
        self._ensure_ok()
        err = self.apply_errors.get(0, None)
        if err is not None:
            raise err
        self.profile_loads.append(profile_id)
        if not any(p.id == profile_id for p in self.profiles):
            raise PluginError(ErrorCode.INVALID_VALUE, f"No profile {profile_id} exists.")
        return ControlResult(
            feature=ControlFeature.PROFILE_LOAD,
            requested_value=None,
            applied_value=None,
            applied=True,
            message=f"Profile {profile_id} applied.",
        )

    def reset_tuning(self, gpu_index: int) -> ControlResult:
        self._ensure_ok()
        err = self.apply_errors.get(gpu_index, None)
        if err is not None:
            raise err
        self.resets.append(gpu_index)
        return ControlResult(
            feature=ControlFeature.PROFILE_RESET,
            requested_value=None,
            applied_value=None,
            applied=True,
            message="Tuning reset to defaults.",
        )

    # ------------------------------------------------------------------ control
    def apply_control(
        self, gpu_index: int, feature: ControlFeature, value: float
    ) -> ControlResult:
        self._ensure_ok()
        err = self.apply_errors.get(gpu_index, None)
        if err is not None:
            raise err
        self.applied.append((gpu_index, feature, value))
        if self.on_apply_control is not None:
            return self.on_apply_control(gpu_index, feature, value)
        return ControlResult(
            feature=feature,
            requested_value=value,
            applied_value=value,
            applied=True,
            message=f"{feature.value} set to {value}.",
        )

    def apply_fan_curve(self, gpu_index: int, curve: FanCurve) -> ControlResult:
        self._ensure_ok()
        err = self.apply_errors.get(gpu_index, None)
        if err is not None:
            raise err
        self.applied_fan_curves.append((gpu_index, curve))
        if self.on_apply_fan_curve is not None:
            return self.on_apply_fan_curve(gpu_index, curve)
        return ControlResult(
            feature=ControlFeature.FAN_CURVE,
            requested_value=None,
            applied_value=None,
            applied=True,
            message=f"Fan curve with {len(curve.points)} points applied.",
        )

    def apply_fan_auto(self, gpu_index: int) -> ControlResult:
        self._ensure_ok()
        err = self.apply_errors.get(gpu_index, None)
        if err is not None:
            raise err
        caps = self.caps_by_gpu.get(gpu_index)
        if caps is not None and not caps.supports(ControlFeature.FAN_PERCENT):
            raise PluginError(
                ErrorCode.UNSUPPORTED_FEATURE,
                "That control isn't available on this hardware/version.",
            )
        state = self.tuning_by_gpu.get(gpu_index)
        if state is not None and state.fan_mode == "auto":
            return ControlResult(
                feature=ControlFeature.FAN_PERCENT,
                requested_value=None,
                applied_value=state.fan_percent,
                applied=False,
                message="Fan is already on automatic control, no change made.",
            )
        self.applied_fan_auto.append(gpu_index)
        if state is not None:
            self.tuning_by_gpu[gpu_index] = replace(state, fan_mode="auto")
        return ControlResult(
            feature=ControlFeature.FAN_PERCENT,
            requested_value=None,
            applied_value=None if state is None else state.fan_percent,
            applied=True,
            message="Fan returned to automatic control.",
        )


def make_fake(
    *,
    status: InterfaceStatus = InterfaceStatus.OK,
    gpu_name: str = "Fake GPU",
    telemetry: Optional[Mapping[int, GpuTelemetry]] = None,
    caps: Optional[Mapping[int, GpuCapabilities]] = None,
    tuning: Optional[Mapping[int, TuningState]] = None,
    profiles: Sequence[int] = (),
    active_profile: Optional[int] = None,
) -> FakeAfterburner:
    """Build a populated FakeAfterburner in one call (tests)."""
    fake = FakeAfterburner(status=status)
    fake.telemetry_by_gpu = dict(telemetry or {})
    fake.caps_by_gpu = dict(caps or {0: default_capabilities(0, gpu_name=gpu_name)})
    fake.tuning_by_gpu = dict(tuning or {0: TuningState(gpu_index=0)})
    for pid in profiles:
        fake.add_profile(pid, is_active=(pid == active_profile))
    return fake
