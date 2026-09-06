"""`ReconnectingAfterburner` — recover when Afterburner starts after the plugin.

The engine spawns the plugin once and keeps the process for the session (manifest
``persistent: true``). If Afterburner is not exposing shared memory at spawn time, a
one-shot adapter would answer "not running" forever — even after Afterburner starts (the
live finding: the plugin kept reporting "MSI Afterburner isn't running" while the app
was up). This wrapper holds a *factory* for the real interface and, whenever the current
one is unavailable, retries detection (cooldown-gated at 2 s so it never hammers the
map) and swaps in a fresh live client the moment detection succeeds. A call that just
hit a recoverable failure triggers one ungated refresh immediately, so a user retry
right after starting Afterburner recovers at once. While down, calls degrade to the same
typed errors as before — nothing is fabricated (Property 7 / Requirement 16.3).
"""
from __future__ import annotations

import time as _time
from typing import Callable, Optional, Sequence

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

_RETRY_COOLDOWN_S = 2.0  # design timing: at most one gated probe per 2 s while down

# Transient conditions worth re-detecting for. NOT_INSTALLED / UNSUPPORTED_VERSION are
# not transient, but a cooldown-gated probe still runs (cheap) and the typed message
# stays accurate until the situation changes.
_RECOVERABLE = {
    ErrorCode.NOT_RUNNING,
    ErrorCode.INTERFACE_UNAVAILABLE,
    ErrorCode.ACCESS_DENIED,
    ErrorCode.DISCONNECTED,
}


class ReconnectingAfterburner:
    """AfterburnerInterface that re-detects and swaps to a live client when available."""

    def __init__(
        self,
        factory: Callable[[], AfterburnerInterface],
        initial: AfterburnerInterface,
        *,
        cooldown_s: float = _RETRY_COOLDOWN_S,
        clock: Optional[Callable[[], float]] = None,
        on_swapped=None,
    ) -> None:
        self._factory = factory
        self._cooldown = cooldown_s
        self._clock = clock or _time.monotonic
        self._on_swapped = on_swapped
        self._current = initial
        self._last_attempt = float("-inf")  # first probe may run immediately

    # ------------------------------------------------------------------ internals
    @property
    def status(self) -> InterfaceStatus:
        return self._current.detect()

    def _probe(self) -> None:
        """Cooldown-gated: try a fresh client; swap in if it reports OK."""
        if self._clock() - self._last_attempt < self._cooldown:
            return
        self._last_attempt = self._clock()
        try:
            candidate = self._factory()
            if candidate.detect() is InterfaceStatus.OK:
                previous = self._current
                self._current = candidate
                if self._on_swapped is not None:
                    self._on_swapped(previous)
        except Exception:
            # Factory/detection failure: keep the current (honest) interface.
            pass

    def _route(self, name: str, *args):
        """Delegate one call; after a recoverable failure, probe and retry once.

        All probes are cooldown-gated, so repeated calls while Afterburner is down cost
        at most one attempt per 2 s, and recovery happens on the first call at least 2 s
        after Afterburner appears.
        """
        if self.status is not InterfaceStatus.OK:
            self._probe()
        try:
            return getattr(self._current, name)(*args)
        except PluginError as exc:
            if exc.code in _RECOVERABLE:
                self._probe()
                if self.status is InterfaceStatus.OK:
                    return getattr(self._current, name)(*args)
            raise

    # ------------------------------------------------------------------ interface
    def detect(self) -> InterfaceStatus:
        if self.status is not InterfaceStatus.OK:
            self._probe()
        return self.status

    def get_version(self) -> Optional[str]:
        return self._route("get_version")

    def read_telemetry(self, gpu_index: int) -> GpuTelemetry:
        return self._route("read_telemetry", gpu_index)

    def read_all_telemetry(self) -> Sequence[GpuTelemetry]:
        return self._route("read_all_telemetry")

    def read_capabilities(self, gpu_index: int) -> GpuCapabilities:
        return self._route("read_capabilities", gpu_index)

    def read_tuning_state(self, gpu_index: int) -> TuningState:
        return self._route("read_tuning_state", gpu_index)

    def list_profiles(self) -> Sequence[Profile]:
        return self._route("list_profiles")

    def load_profile(self, profile_id: int) -> ControlResult:
        return self._route("load_profile", profile_id)

    def reset_tuning(self, gpu_index: int) -> ControlResult:
        return self._route("reset_tuning", gpu_index)

    def apply_control(
        self, gpu_index: int, feature: ControlFeature, value: float
    ) -> ControlResult:
        return self._route("apply_control", gpu_index, feature, value)

    def apply_fan_curve(self, gpu_index: int, curve: FanCurve) -> ControlResult:
        return self._route("apply_fan_curve", gpu_index, curve)

    def apply_fan_auto(self, gpu_index: int) -> ControlResult:
        return self._route("apply_fan_auto", gpu_index)
