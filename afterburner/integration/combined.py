"""`CombinedAfterburner` — the AfterburnerInterface the plugin process injects.

Composes the two OS adapters into one protocol-complete interface (plan tasks 18/20 wiring):
monitoring reads (`detect`/`get_version`/telemetry) come from the read-only MAHM client;
capabilities, applied tuning state, and every control write come from the MACM control
client. `detect()` reflects whether Afterburner is running (MAHM readable); the granular
control-interface status travels inside each `GpuCapabilities.control_interface_status`, so
the capability resolver degrades writes to read-only when MACM is down while monitoring still
works — matching the interface contract ("control interface unreadable => None / empty
capabilities").

Profile *listing/application* is handled at the profile-service layer (read-only Profiles
scan + per-feature named applies through this same interface); the interface-level profile
methods are therefore answered with typed errors here, exactly like the monitoring-only and
control-only adapters, so no path fabricates data.
"""
from __future__ import annotations

from typing import Optional, Sequence

from ..models import (
    ControlFeature,
    ControlResult,
    ErrorCode,
    FanCurve,
    GpuCapabilities,
    GpuTelemetry,
    InterfaceStatus,
    PluginError,
    Profile,
    TuningState,
)
from .interface import AfterburnerInterface
from .macm import AfterburnerControlClient
from .mahm import AfterburnerMonitoringClient


class CombinedAfterburner:
    """AfterburnerInterface = MAHM monitoring reads + MACM control writes."""

    def __init__(
        self,
        monitoring: AfterburnerMonitoringClient,
        control: AfterburnerControlClient,
    ) -> None:
        self.monitoring = monitoring
        self.control = control

    # ------------------------------------------------------------ lifecycle
    def detect(self) -> InterfaceStatus:
        return self.monitoring.detect()

    def get_version(self) -> Optional[str]:
        return self.monitoring.get_version()

    # ---------------------------------------------------------- monitoring
    def read_telemetry(self, gpu_index: int) -> GpuTelemetry:
        return self.monitoring.read_telemetry(gpu_index)

    def read_all_telemetry(self) -> Sequence[GpuTelemetry]:
        return self.monitoring.read_all_telemetry()

    # -------------------------------------------------------------- control
    def read_capabilities(self, gpu_index: int) -> GpuCapabilities:
        return self.control.read_capabilities(gpu_index)

    def read_tuning_state(self, gpu_index: int) -> TuningState:
        return self.control.read_tuning_state(gpu_index)

    def apply_control(
        self, gpu_index: int, feature: ControlFeature, value: float
    ) -> ControlResult:
        return self.control.apply_control(gpu_index, feature, value)

    def apply_fan_curve(self, gpu_index: int, curve: FanCurve) -> ControlResult:
        return self.control.apply_fan_curve(gpu_index, curve)

    def reset_tuning(self, gpu_index: int) -> ControlResult:
        return self.control.reset_tuning(gpu_index)

    # -------------------------------------------------- profile layer (N/A)
    def list_profiles(self) -> Sequence[Profile]:
        raise PluginError(
            ErrorCode.INTERFACE_UNAVAILABLE,
            "Profiles are listed through the profile service's read-only scan of the "
            "Afterburner Profiles directory.",
            detail="CombinedAfterburner: profile enumeration lives in ProfileManager",
        )

    def load_profile(self, profile_id: int) -> ControlResult:
        raise PluginError(
            ErrorCode.INTERFACE_UNAVAILABLE,
            "Profile loading applies stored named controls through the profile service.",
            detail="CombinedAfterburner: profile loading lives in ProfileManager",
        )
