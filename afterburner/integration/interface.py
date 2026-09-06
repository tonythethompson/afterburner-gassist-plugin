"""Afterburner adapter contract — the mock boundary (design "Afterburner Adapter Interface").

The entire stack depends only on this abstraction. Real implementations wrap MAHM (monitoring)
and MACM (control); unit tests inject `FakeAfterburner`. No real GPU or Afterburner is needed
for unit tests.

Deliberately absent: any generic `write(offset, bytes)` primitive. Callers can only request
named, validated controls — never arbitrary memory writes (design Property 6).
"""
from __future__ import annotations

from typing import Optional, Protocol, Sequence, runtime_checkable

from ..models import (
    ControlFeature,
    ControlResult,
    FanCurve,
    GpuCapabilities,
    GpuTelemetry,
    InterfaceStatus,
    Profile,
    TuningState,
)


@runtime_checkable
class AfterburnerInterface(Protocol):
    def detect(self) -> InterfaceStatus:
        """Is Afterburner installed & running; is shared memory readable?"""
        ...

    def get_version(self) -> Optional[str]:
        ...

    # --- Monitoring (MAHM — official, read-only) ---
    def read_telemetry(self, gpu_index: int) -> GpuTelemetry:
        ...

    def read_all_telemetry(self) -> Sequence[GpuTelemetry]:
        ...

    # --- State & capability reads (read-only; CONTROL shared memory) ---
    # Implementations read these LIVE from the MACM map (entry *_Cur / *_Min..*_Max fields),
    # never from the telemetry cache. Control interface unreadable => None / empty capabilities.
    def read_capabilities(self, gpu_index: int) -> GpuCapabilities:
        ...

    def read_tuning_state(self, gpu_index: int) -> TuningState:
        ...

    # --- Profiles (Afterburner is source of truth) ---
    # list_profiles: read-only scan of the detected Afterburner installation's Profiles
    #   directory (Requirement 14.3 carve-out) — never writes there.
    # load_profile: read the stored profile (read-only) and apply its settings through named,
    #   validated control operations; requires the control interface. Never writes to the
    #   Profiles directory.
    def list_profiles(self) -> Sequence[Profile]:
        ...

    def load_profile(self, profile_id: int) -> ControlResult:
        ...

    def reset_tuning(self, gpu_index: int) -> ControlResult:
        ...

    # --- Control (MACM — official SDK header, capability-gated) ---
    # Implementations MUST write only known control fields with validated, clamped values.
    # There is NO generic write(offset, value) method by design.
    def apply_control(
        self, gpu_index: int, feature: ControlFeature, value: float
    ) -> ControlResult:
        ...

    def apply_fan_curve(self, gpu_index: int, curve: FanCurve) -> ControlResult:
        ...
