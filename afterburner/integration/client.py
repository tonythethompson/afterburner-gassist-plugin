"""`AfterburnerClient` — facade over the integration layer (design component table).

Holds the injected `AfterburnerInterface`, orchestrates detect/read/profile/control calls and
translates every raw adapter failure into a typed `PluginError` (graceful degradation; never an
unhandled exception). Afterburner-dependent operations are rejected with a typed error when no
interface is injected.
"""
from __future__ import annotations

from typing import Callable, Optional, Sequence, TypeVar

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

T = TypeVar("T")


def _no_interface_error() -> PluginError:
    return PluginError(
        ErrorCode.INTERFACE_UNAVAILABLE,
        "Afterburner is installed but its interface is unavailable. "
        "Start MSI Afterburner and try again.",
        detail="no AfterburnerInterface injected",
    )


class AfterburnerClient:
    """Thin, error-translating facade over an `AfterburnerInterface`."""

    def __init__(self, interface: Optional[AfterburnerInterface] = None) -> None:
        self.interface = interface

    # ------------------------------------------------------------------ utils
    def _interface(self) -> AfterburnerInterface:
        if self.interface is None:
            raise _no_interface_error()
        return self.interface

    def _guard(self, fn: Callable[..., T], *args, **kwargs) -> T:
        try:
            return fn(*args, **kwargs)
        except PluginError:
            raise
        except Exception as exc:  # pragma: no cover - defensive: never raise unhandled
            raise PluginError(
                ErrorCode.COMM_FAILURE,
                "I couldn't read from Afterburner just now. Try again in a moment.",
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc

    # -------------------------------------------------------------- lifecycle
    def detect(self) -> InterfaceStatus:
        if self.interface is None:
            return InterfaceStatus.NOT_INSTALLED
        return self._guard(self.interface.detect)

    def get_version(self) -> Optional[str]:
        if self.interface is None:
            return None
        return self._guard(self.interface.get_version)

    # ---------------------------------------------------------------- reads
    def read_telemetry(self, gpu_index: int = 0) -> GpuTelemetry:
        return self._guard(self._interface().read_telemetry, gpu_index)

    def read_all_telemetry(self) -> Sequence[GpuTelemetry]:
        return self._guard(self._interface().read_all_telemetry)

    def read_capabilities(self, gpu_index: int = 0) -> GpuCapabilities:
        return self._guard(self._interface().read_capabilities, gpu_index)

    def read_tuning_state(self, gpu_index: int = 0) -> TuningState:
        return self._guard(self._interface().read_tuning_state, gpu_index)

    def list_profiles(self) -> Sequence[Profile]:
        return self._guard(self._interface().list_profiles)

    # -------------------------------------------------------------- controls
    def apply_control(
        self, gpu_index: int, feature: ControlFeature, value: float
    ) -> ControlResult:
        return self._guard(self._interface().apply_control, gpu_index, feature, value)

    def apply_fan_curve(self, gpu_index: int, curve: FanCurve) -> ControlResult:
        return self._guard(self._interface().apply_fan_curve, gpu_index, curve)

    def apply_fan_auto(self, gpu_index: int) -> ControlResult:
        return self._guard(self._interface().apply_fan_auto, gpu_index)

    def load_profile(self, profile_id: int) -> ControlResult:
        return self._guard(self._interface().load_profile, profile_id)

    def reset_tuning(self, gpu_index: int = 0) -> ControlResult:
        return self._guard(self._interface().reset_tuning, gpu_index)
