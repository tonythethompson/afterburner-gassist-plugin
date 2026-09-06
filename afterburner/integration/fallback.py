"""`UnavailableAfterburner` — honest AfterburnerInterface when no real client is present.

Used by the plugin entry point until the real MAHM monitoring / MACM control adapters land
(plan tasks 18-21): the process must start, degrade gracefully (Property 7 / Requirement
16.3), and answer every monitoring/control call with a typed error instead of fabricating
values. The concrete status is chosen from what can be observed without shared memory:
NOT_INSTALLED when no install is detected, otherwise UNAVAILABLE (installed but the shared
memory interface cannot be reached yet — elevation/version checks arrive with the real
clients).
"""

from __future__ import annotations

from typing import Optional, Sequence

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

_STATUS_MESSAGE = {
    InterfaceStatus.NOT_INSTALLED: (
        ErrorCode.NOT_INSTALLED,
        "MSI Afterburner doesn't appear to be installed.",
    ),
    InterfaceStatus.UNAVAILABLE: (
        ErrorCode.INTERFACE_UNAVAILABLE,
        "MSI Afterburner is installed but its interface is unavailable. "
        "Start MSI Afterburner and try again.",
    ),
    InterfaceStatus.NOT_RUNNING: (
        ErrorCode.NOT_RUNNING,
        "MSI Afterburner isn't running. Start it and try again.",
    ),
    InterfaceStatus.UNSUPPORTED_VERSION: (
        ErrorCode.UNSUPPORTED_VERSION,
        "Your Afterburner version doesn't expose the needed interface. Please update.",
    ),
    InterfaceStatus.ACCESS_DENIED: (
        ErrorCode.ACCESS_DENIED,
        "Controlling Afterburner needs elevated permissions.",
    ),
}


class UnavailableAfterburner:
    """Minimal AfterburnerInterface that always degrades to a typed error."""

    def __init__(
        self,
        status: InterfaceStatus = InterfaceStatus.NOT_INSTALLED,
        detail: str = "",
    ) -> None:
        self.status = status
        self.detail = detail

    def _raise(self) -> None:
        code, message = _STATUS_MESSAGE.get(
            self.status, (ErrorCode.INTERFACE_UNAVAILABLE, "unavailable")
        )
        raise PluginError(code, message, detail=self.detail)

    # ------------------------------------------------------------------ reads
    def detect(self) -> InterfaceStatus:
        return self.status

    def get_version(self) -> Optional[str]:
        return None

    def read_telemetry(self, gpu_index: int) -> GpuTelemetry:
        self._raise()

    def read_all_telemetry(self) -> Sequence[GpuTelemetry]:
        self._raise()

    def read_capabilities(self, gpu_index: int) -> GpuCapabilities:
        self._raise()

    def read_tuning_state(self, gpu_index: int) -> TuningState:
        self._raise()

    def list_profiles(self) -> Sequence[Profile]:
        self._raise()

    def load_profile(self, profile_id: int) -> ControlResult:
        self._raise()

    def reset_tuning(self, gpu_index: int) -> ControlResult:
        self._raise()

    # ------------------------------------------------------------------ control
    def apply_control(
        self, gpu_index: int, feature: ControlFeature, value: float
    ) -> ControlResult:
        self._raise()

    def apply_fan_curve(self, gpu_index: int, curve: FanCurve) -> ControlResult:
        self._raise()

    def apply_fan_auto(self, gpu_index: int) -> ControlResult:
        self._raise()
