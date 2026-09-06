"""Capability detection & gating (design "Capability Resolution").

Supported controls derive ONLY from Afterburner-reported capability flags + reported min/max
limits — never hardcoded per-GPU assumptions. Write-capable features require a functional
control interface (MACM). When Afterburner or the control interface is unavailable the resolver
degrades to read-only capabilities (and, for profiles, adds PROFILE_LOAD / PROFILE_RESET only
when the control interface is OK).
"""
from __future__ import annotations

import time
from dataclasses import replace
from typing import Callable, Optional

from ..integration.interface import AfterburnerInterface
from ..models import (
    ControlFeature,
    GpuCapabilities,
    GpuLimits,
    InterfaceStatus,
    PluginError,
)

DEFAULT_FAN_CURVE_MAX_POINTS = 2
FAN_CURVE_ABSOLUTE_MAX_POINTS = 32
DEFAULT_RESOLUTION_TIMEOUT_SECONDS = 5.0


def _status_from_error(code: "ErrorCode") -> InterfaceStatus:
    """Map an adapter error code onto an interface status for degraded capabilities."""
    from ..models import ErrorCode as _EC

    mapping = {
        _EC.NOT_INSTALLED: InterfaceStatus.NOT_INSTALLED,
        _EC.NOT_RUNNING: InterfaceStatus.NOT_RUNNING,
        _EC.UNSUPPORTED_VERSION: InterfaceStatus.UNSUPPORTED_VERSION,
        _EC.ACCESS_DENIED: InterfaceStatus.ACCESS_DENIED,
        _EC.DISCONNECTED: InterfaceStatus.NOT_RUNNING,
    }
    return mapping.get(code, InterfaceStatus.UNAVAILABLE)

_WRITE_SINGLE_VALUE_FEATURES = (
    ControlFeature.POWER_LIMIT,
    ControlFeature.CORE_OFFSET,
    ControlFeature.MEMORY_OFFSET,
    ControlFeature.VOLTAGE,
    ControlFeature.FAN_PERCENT,
)


class HardwareCapabilityResolver:
    """Resolves the supported control set for one GPU, degrading gracefully on timeout."""

    def __init__(
        self,
        interface: AfterburnerInterface,
        timeout: float = DEFAULT_RESOLUTION_TIMEOUT_SECONDS,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._interface = interface
        self.timeout = timeout
        self._clock = clock or time.monotonic
        self.last_timed_out = False

    # ----------------------------------------------------------------------
    def resolve(self, gpu_index: int) -> GpuCapabilities:
        """Return Afterburner-reported capabilities with the gated supported set."""
        start = self._clock()

        status = self._interface.detect()
        if self._elapsed(start) >= self.timeout:
            return self._read_only(gpu_index, "", InterfaceStatus.UNAVAILABLE, timed_out=True)
        if status is not InterfaceStatus.OK:
            return self._read_only(gpu_index, "", status, timed_out=False)

        try:
            caps = self._interface.read_capabilities(gpu_index)
        except PluginError as exc:
            # Adapter says the interface is broken; degrade read-only rather than raise.
            return self._read_only(gpu_index, "", _status_from_error(exc.code), timed_out=False)
        except Exception:
            return self._read_only(
                gpu_index, "", InterfaceStatus.UNAVAILABLE, timed_out=False
            )

        if self._elapsed(start) >= self.timeout:
            return self._read_only(gpu_index, caps.gpu_name, InterfaceStatus.UNAVAILABLE,
                                   timed_out=True)

        supported = self._gated_supported(caps)
        self.last_timed_out = False
        return replace(
            caps,
            supported_controls=frozenset(supported),
            control_interface_status=caps.control_interface_status,
        )

    # ----------------------------------------------------------------------
    def _elapsed(self, start: float) -> float:
        return self._clock() - start

    def _gated_supported(self, caps: GpuCapabilities) -> set[ControlFeature]:
        limits = caps.limits
        supported: set[ControlFeature] = set()

        # Single-value features: advertised AND reported with a sane (non-inverted) range.
        for feature in _WRITE_SINGLE_VALUE_FEATURES:
            rng = limits.range_for(feature)
            if feature in caps.supported_controls and rng is not None and not rng.inverted:
                supported.add(feature)

        # Fan curves additionally need the reported max point count and temp/fan axes.
        max_points = limits.fan_curve_max_points
        if max_points is None:
            max_points = DEFAULT_FAN_CURVE_MAX_POINTS
        max_points = min(max_points, FAN_CURVE_ABSOLUTE_MAX_POINTS)
        if (
            ControlFeature.FAN_CURVE in caps.supported_controls
            and max_points >= 2
            and limits.fan_temp_c is not None
            and not limits.fan_temp_c.inverted
            and limits.fan_percent is not None
            and not limits.fan_percent.inverted
        ):
            supported.add(ControlFeature.FAN_CURVE)

        # Control writes (single-value + fan curve) require a functional control interface.
        macm_ok = caps.control_interface_status is InterfaceStatus.OK
        if not macm_ok:
            supported = supported - {
                ControlFeature.POWER_LIMIT,
                ControlFeature.CORE_OFFSET,
                ControlFeature.MEMORY_OFFSET,
                ControlFeature.VOLTAGE,
                ControlFeature.FAN_PERCENT,
                ControlFeature.FAN_CURVE,
            }
        else:
            # Profile apply/reset are writes and additionally require MACM.
            if ControlFeature.PROFILE_LOAD in caps.supported_controls:
                supported.add(ControlFeature.PROFILE_LOAD)
            if ControlFeature.PROFILE_RESET in caps.supported_controls:
                supported.add(ControlFeature.PROFILE_RESET)

        return supported

    def _read_only(
        self,
        gpu_index: int,
        gpu_name: str,
        status: InterfaceStatus,
        *,
        timed_out: bool = False,
    ) -> GpuCapabilities:
        self.last_timed_out = timed_out
        return GpuCapabilities(
            gpu_index=gpu_index,
            gpu_name=gpu_name,
            supported_controls=frozenset(),
            limits=GpuLimits(),
            control_interface_status=status,
        )
