"""Optimization intents — ``optimize_quiet`` / ``optimize_thermal`` (Requirement 11).

Focused fan/thermal optimization: applied changes are **restricted to fan controls only**
(FAN_PERCENT — the sole fan/thermal actuator exposed by the control interface); clock,
voltage, power-limit, and memory tuning parameters are never touched (Req 11.5). Every fan
value goes through the same validation/clamping as LLM-supplied values, and every outcome is
verified by telemetry read-back before success is claimed (Req 11.1/11.3) — never a
fabricated success.

Confirmation gating (Req 11.6 / design Property 4): ``optimize_quiet`` and
``optimize_thermal`` are HIGH-risk operations per the design's function table, so the plugin
layer (plan task 15.2, ``executeRisky``) requires a valid confirmation token *before* these
methods are invoked; prior settings are preserved until then. This service implements the
confirmed apply + verification path.

Semantics implemented here:

- ``optimize_quiet`` — lower the current fan setpoint by >= 10 percentage points and verify
  by telemetry read-back that temperature stays at/below 83 C. When any reduction would push
  the GPU over that limit (measured after the change) the fan is restored to its prior
  setpoint and a typed error is returned (Req 11.1/11.2).
- ``optimize_thermal(target_c)`` — target 40..95 C inclusive (outside/missing/non-numeric
  rejected with the valid range, fan left unchanged — Req 11.4). While temperature exceeds
  the target the fan is raised in steps (fan-only) up to 100%; once the target is met a
  success result with the measured temperature is returned; if even max fan cannot reach it,
  an honest failure is returned (Req 11.3) — never a false success.

Feasibility failures (target not reachable / reduction unsafe) use ``UNSUPPORTED_FEATURE``
with a precise user message; missing required readings use ``INTERFACE_UNAVAILABLE`` with a
precise user message — both map to the protocol-level ``error`` result (V2 -1).
"""

from __future__ import annotations

import math
import time as _time
from typing import Callable, Optional

from ..integration.client import AfterburnerClient
from ..models import (
    ControlFeature,
    ControlResult,
    ErrorCode,
    PluginError,
)
from ..safety.capabilities import HardwareCapabilityResolver
from ..safety.validator import TuningValidator

QUIET_TEMP_LIMIT_C = 83.0
QUIET_REDUCTION_PTS = 10.0
THERMAL_FAN_STEP_PTS = 20.0
THERMAL_TARGET_MIN_C = 40.0
THERMAL_TARGET_MAX_C = 95.0
VERIFY_TIMEOUT_S = 30.0  # Req 11.3: read-back verification window
VERIFY_READ_INTERVAL_S = 1.0


def _fan_only_result(fan_value: float, message: str, *, applied: bool) -> ControlResult:
    return ControlResult(
        feature=ControlFeature.FAN_PERCENT,
        requested_value=fan_value if applied else None,
        applied_value=fan_value if applied else None,
        applied=applied,
        message=message,
    )


class OptimizeService:
    """Focused fan/thermal optimization over the Afterburner client (fan-only writes)."""

    def __init__(
        self,
        client: AfterburnerClient,
        *,
        gpu_index: int = 0,
        quiet_temp_limit_c: float = QUIET_TEMP_LIMIT_C,
        reduction_pts: float = QUIET_REDUCTION_PTS,
        fan_step_pts: float = THERMAL_FAN_STEP_PTS,
        target_min_c: float = THERMAL_TARGET_MIN_C,
        target_max_c: float = THERMAL_TARGET_MAX_C,
        verify_timeout_s: float = VERIFY_TIMEOUT_S,
        read_interval_s: float = VERIFY_READ_INTERVAL_S,
        sleep: Callable[[float], None] = _time.sleep,
    ) -> None:
        self._client = client
        self.gpu_index = gpu_index
        self.quiet_temp_limit_c = quiet_temp_limit_c
        self.reduction_pts = reduction_pts
        self.fan_step_pts = fan_step_pts
        self.target_min_c = target_min_c
        self.target_max_c = target_max_c
        self.verify_timeout_s = verify_timeout_s
        self.read_interval_s = read_interval_s
        self._sleep = sleep
        if client.interface is not None:
            self._resolver = HardwareCapabilityResolver(client.interface)
            self._validator = TuningValidator(self._resolver)
        else:
            self._resolver = None
            self._validator = None

    # ------------------------------------------------------------------ quiet
    def optimize_quiet(self, gpu_index: Optional[int] = None) -> ControlResult:
        """Lower the fan setpoint >= 10 pts while keeping the GPU at/below the temp limit."""
        return self._typed(
            lambda: self._optimize_quiet(self._gpu(gpu_index))
        )

    # ------------------------------------------------------------------ thermal
    def optimize_thermal(
        self, target_c: object, gpu_index: Optional[int] = None
    ) -> ControlResult:
        """Keep the GPU at/below ``target_c`` (40-95 inclusive) using fan control only."""
        return self._typed(
            lambda: self._optimize_thermal(self._gpu(gpu_index), target_c)
        )

    # ------------------------------------------------------------------ helpers
    def _gpu(self, gpu_index: Optional[int]) -> int:
        return self.gpu_index if gpu_index is None else gpu_index

    @staticmethod
    def _typed(fn):
        try:
            return fn()
        except PluginError:
            raise
        except Exception as exc:  # pragma: no cover - never an unhandled exception
            raise PluginError(
                ErrorCode.COMM_FAILURE,
                "I couldn't complete that optimization just now. Try again.",
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc

    # ------------------------------------------------------------------ impl
    def _optimize_quiet(self, gpu_index: int) -> ControlResult:
        temp = self._read_temp(gpu_index)
        if temp is None:
            raise PluginError(
                ErrorCode.INTERFACE_UNAVAILABLE,
                "I don't have a current GPU temperature, so I can't safely lower the fan.",
            )
        if temp > self.quiet_temp_limit_c:
            raise PluginError(
                ErrorCode.UNSUPPORTED_FEATURE,
                f"The GPU is already at {temp:.1f}°C (over {self.quiet_temp_limit_c:g}°C), "
                "so I can't lower the fan without overheating it. I left it unchanged.",
            )

        current_fan = self._current_fan_setpoint(gpu_index)
        if current_fan is None:
            raise PluginError(
                ErrorCode.INTERFACE_UNAVAILABLE,
                "I can't read the current fan speed, so I can't lower it safely.",
            )
        if current_fan < self.reduction_pts:
            raise PluginError(
                ErrorCode.UNSUPPORTED_FEATURE,
                f"The fan is already very low ({current_fan:g}%), so I can't reduce it by "
                f"{self.reduction_pts:g} points. I left it unchanged.",
            )

        target_fan = current_fan - self.reduction_pts
        safe_fan = self._validate_fan(gpu_index, target_fan)
        self._client.apply_control(gpu_index, ControlFeature.FAN_PERCENT, safe_fan)

        # Verify with a fresh read-back (the fan change may have heated the GPU);
        # initial=None forces the first live read rather than trusting the pre-apply temp.
        measured = self._poll_temp_at_or_below(
            gpu_index, self.quiet_temp_limit_c, initial=None
        )
        if measured is None or measured > self.quiet_temp_limit_c:
            # Honest revert: lowering the fan pushed the GPU past the limit.
            self._restore_fan(gpu_index, current_fan)
            raise PluginError(
                ErrorCode.UNSUPPORTED_FEATURE,
                f"Lowering the fan let the GPU reach {measured if measured is not None else '?'}"
                f"°C (over {self.quiet_temp_limit_c:g}°C), so I restored the fan to "
                f"{current_fan:g}%.",
            )

        return _fan_only_result(
            safe_fan,
            f"Fan lowered from {current_fan:g}% to {safe_fan:g}%; "
            f"GPU temperature is {measured:.1f}°C (limit {self.quiet_temp_limit_c:g}°C).",
            applied=True,
        )

    def _optimize_thermal(self, gpu_index: int, target_c: object) -> ControlResult:
        target = self._validate_target(target_c)
        temp = self._read_temp(gpu_index)
        if temp is None:
            raise PluginError(
                ErrorCode.INTERFACE_UNAVAILABLE,
                "I don't have a current GPU temperature to optimize against.",
            )
        if temp <= target:
            return _fan_only_result(
                target,
                f"The GPU is already at {temp:.1f}°C — at or below your target of "
                f"{target:g}°C, so no change was needed.",
                applied=False,
            )

        current_fan = self._current_fan_setpoint(gpu_index) or 0.0
        while temp is not None and temp > target:
            next_fan = min(current_fan + self.fan_step_pts, 100.0)
            if next_fan <= current_fan:  # already at 100% and still over target
                break
            safe_fan = self._validate_fan(gpu_index, next_fan)
            self._client.apply_control(gpu_index, ControlFeature.FAN_PERCENT, safe_fan)
            current_fan = safe_fan
            temp = self._poll_temp_at_or_below(gpu_index, target, initial=temp)

        if temp is not None and temp <= target:
            return _fan_only_result(
                current_fan,
                f"Fan raised to {current_fan:g}%; GPU is now {temp:.1f}°C "
                f"(target {target:g}°C).",
                applied=True,
            )

        # Honest failure: even max fan couldn't reach the target within the budget.
        raise PluginError(
            ErrorCode.UNSUPPORTED_FEATURE,
            f"I couldn't get the GPU down to {target:g}°C — even at {current_fan:g}% fan "
            f"it reads {temp if temp is not None else 'unavailable'}°C. "
            "That target may not be reachable with this GPU/cooling.",
        )

    # ------------------------------------------------------------------ reads/writes
    def _read_temp(self, gpu_index: int) -> Optional[float]:
        telemetry = self._client.read_telemetry(gpu_index)
        temp = telemetry.temperature_c
        if temp is None:
            return None
        return temp if math.isfinite(temp) else None

    def _current_fan_setpoint(self, gpu_index: int) -> Optional[float]:
        """Current fan: the applied setpoint when manual, else the measured speed."""
        try:
            state = self._client.read_tuning_state(gpu_index)
            if state.fan_percent is not None and math.isfinite(state.fan_percent):
                return state.fan_percent
        except PluginError:
            pass  # fall back to measured telemetry fan
        telemetry = self._client.read_telemetry(gpu_index)
        fan = telemetry.fan_percent
        if fan is None or not math.isfinite(fan):
            return None
        return fan

    def _validate_fan(self, gpu_index: int, value: float) -> float:
        """Clamp a fan value through the same validator as LLM-supplied values."""
        if self._validator is None:
            raise PluginError(
                ErrorCode.INTERFACE_UNAVAILABLE,
                "Afterburner is installed but its control interface is unavailable. "
                "Start MSI Afterburner and try again.",
            )
        safe_value, _clamped = self._validator.validate_and_clamp(
            gpu_index, ControlFeature.FAN_PERCENT, value
        )
        return safe_value

    def _poll_temp_at_or_below(
        self,
        gpu_index: int,
        target_c: float,
        *,
        initial: Optional[float],
    ) -> Optional[float]:
        """Read back temperature until <= target or the verification budget is spent."""
        if initial is not None and initial <= target_c:
            return initial
        if self.read_interval_s > 0:
            attempts = max(1, int(self.verify_timeout_s / self.read_interval_s))
        else:
            attempts = 1  # no waiting configured (tests) => one live read-back
        measured = initial
        for _ in range(attempts):
            if self.read_interval_s > 0:
                self._sleep(self.read_interval_s)
            measured = self._read_temp(gpu_index)
            if measured is not None and measured <= target_c:
                return measured
        return measured

    def _restore_fan(self, gpu_index: int, fan: float) -> None:
        try:
            safe = self._validate_fan(gpu_index, fan)
            self._client.apply_control(gpu_index, ControlFeature.FAN_PERCENT, safe)
        except PluginError:
            pass  # best-effort restore; the error already tells the user what happened

    def _validate_target(self, target_c: object) -> float:
        """Reject out-of-range / missing / non-numeric targets (Req 11.4)."""
        if isinstance(target_c, bool) or not isinstance(target_c, (int, float)):
            raise PluginError(
                ErrorCode.INVALID_VALUE,
                f"A target temperature must be between {self.target_min_c:g}°C and "
                f"{self.target_max_c:g}°C.",
                detail=f"non-numeric target {target_c!r}",
            )
        value = float(target_c)
        if not math.isfinite(value):
            raise PluginError(
                ErrorCode.INVALID_VALUE,
                f"A target temperature must be between {self.target_min_c:g}°C and "
                f"{self.target_max_c:g}°C.",
                detail=f"non-finite target {target_c!r}",
            )
        if not self.target_min_c <= value <= self.target_max_c:
            raise PluginError(
                ErrorCode.INVALID_VALUE,
                f"Target temperature must be between {self.target_min_c:g}°C and "
                f"{self.target_max_c:g}°C.",
                detail=f"target {value} out of range",
            )
        return value
