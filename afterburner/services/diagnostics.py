"""DiagnosticsService — evidence-based performance-limiter classification.

Design "Diagnostics Classification" + Requirement 12. Classification is deliberately
conservative (Property 5): it never overclaims on stale or missing data, every *inferred*
cause carries confidence strictly inside (0.0, 1.0), and every inferred cause includes
non-empty evidence items that name the telemetry field and its observed value.

Limits of a single-snapshot heuristic (documented honestly): power / voltage limiting are
inferred from coarse signals (utilization pinned near 100%, reported power-limit % at/under
the stock ceiling, core voltage near the high end) — these are heuristics to be re-verified
against real MAHM telemetry (plan task 18) and Afterburner-reported control-map ranges; the
confidence values and the UNKNOWN preference keep them honest meanwhile. No CPU utilization
is observable through Afterburner, so a CPU-limited vs application-behavior split is
inferred from GPU-side evidence only (VRAM pressure distinguishes app-behavior).

Returned values are exactly one of: thermal | power | voltage | utilization-bottleneck |
cpu-limited | application-behavior | unknown-insufficient-data (Req 12.1).
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence, Tuple

from ..models import Diagnosis, DiagnosisCause, GpuTelemetry, PluginError
from .telemetry import STALE_THRESHOLD_SECONDS, TelemetryService

# Essential fields: when any is missing/null the classifier must say UNKNOWN (Req 12.4).
ESSENTIAL_FIELDS: Tuple[str, ...] = (
    "temperature_c",
    "utilization_pct",
    "core_clock_mhz",
)

# Heuristic thresholds (configurable; see module docstring).
DEFAULT_THERMAL_LIMIT_C = 85.0  # near/over typical NVIDIA thermal throttle
HIGH_UTIL_PCT = 95.0  # "pinned at the limit"
LOW_UTIL_PCT = 60.0  # below this the GPU itself is clearly not the bottleneck
VOLTAGE_CEILING_HINT_MV = 1100.0  # core voltage near the top of typical v/f curves
VRAM_PRESSURE_RATIO = 0.90  # >= 90% VRAM used => application-behavior signal

# Confidence constants: inferred causes are never 0.0 or 1.0 (Req 12.5); UNKNOWN is low.
_CONF_UNKNOWN_NO_DATA = 0.2
_CONF_UNKNOWN_NO_LIMITER = 0.3
_CONF_THERMAL = 0.8
_CONF_POWER = 0.55
_CONF_VOLTAGE = 0.5
_CONF_UTIL_BOTTLENECK = 0.6
_CONF_CPU = 0.45
_CONF_APP = 0.5


def _fmt(value: Optional[float], digits: int = 1) -> str:
    if value is None:
        return "unavailable"
    return f"{value:.{digits}f}"


def _missing_or_non_finite(value: Optional[float]) -> bool:
    """Missing or null *or* non-finite — either way the sample can't be trusted."""
    return value is None or not math.isfinite(float(value))


class DiagnosticsService:
    """Classifies the likely GPU performance limiter from timestamped telemetry."""

    def __init__(
        self,
        telemetry: TelemetryService,
        *,
        thermal_limit_c: float = DEFAULT_THERMAL_LIMIT_C,
        stale_after_s: float = STALE_THRESHOLD_SECONDS,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._telemetry = telemetry
        self.thermal_limit_c = thermal_limit_c
        self.stale_after_s = stale_after_s
        self._now = now or (lambda: datetime.now(timezone.utc))

    # ----------------------------------------------------------------------
    def diagnose_performance(self, gpu_index: int = 0) -> Diagnosis:
        """Return an evidence-based Diagnosis (never raises for interface problems)."""
        try:
            telemetry = self._telemetry.get_status(gpu_index)
        except PluginError as exc:
            return self._unknown(
                ("telemetry read failed ({}): {}".format(exc.code.value, exc.detail or ""),)
                if exc.detail
                else ("telemetry read failed ({})".format(exc.code.value),)
            )

        fresh, missing = self._freshness(telemetry)
        if not fresh:
            if missing:
                return self._unknown(
                    (
                        "essential field(s) missing: " + ", ".join(missing),
                        f"sampled_at={telemetry.sampled_at.isoformat()}",
                    )
                )
            if telemetry.is_stale:
                return self._unknown(
                    ("sample flagged stale (Afterburner may be restarting)",)
                )
            return self._unknown(
                (
                    f"sample older than {self.stale_after_s:g}s: "
                    f"sampled_at={telemetry.sampled_at.isoformat()}",
                )
            )

        return self._classify(telemetry)

    # ------------------------------------------------------------------ internals
    def _freshness(self, telemetry: GpuTelemetry) -> Tuple[bool, Sequence[str]]:
        """(fresh?, missing-essential-fields). Age check uses sampled_at vs now."""
        missing = [
            name
            for name in ESSENTIAL_FIELDS
            if _missing_or_non_finite(getattr(telemetry, name))
        ]
        if missing:
            return False, missing
        age = (self._now() - telemetry.sampled_at).total_seconds()
        if telemetry.is_stale or age > self.stale_after_s:
            return False, ()
        return True, ()

    def _classify(self, t: GpuTelemetry) -> Diagnosis:
        evidence: list[str] = []

        # --- thermal ---------------------------------------------------------
        temp = t.temperature_c
        if temp is not None and temp >= self.thermal_limit_c:
            evidence.append(
                f"temperature_c={_fmt(temp)}°C >= thermal limit {self.thermal_limit_c:g}°C"
            )
            if t.fan_percent is not None:
                evidence.append(f"fan_percent={_fmt(t.fan_percent)}% (max effort)")
            return self._result(
                DiagnosisCause.THERMAL,
                _CONF_THERMAL,
                evidence,
                f"The GPU is at {_fmt(temp)}°C — right at/over the thermal limit, so "
                "clocks are being cut back to protect it.",
            )

        util = t.utilization_pct
        # --- power -----------------------------------------------------------
        # Signal: GPU pinned near 100% while the reported power limit sits at/under the
        # stock ceiling => throttling at the power cap is the likely cause.
        if (
            util is not None
            and util >= HIGH_UTIL_PCT
            and t.power_limit_pct is not None
            and t.power_limit_pct <= 100.0
            and t.power_watts is not None
        ):
            evidence.append(f"utilization_pct={_fmt(util)}% (pinned)")
            evidence.append(f"power_limit_pct={_fmt(t.power_limit_pct)}% (at/under stock)")
            evidence.append(f"power_watts={_fmt(t.power_watts)}W")
            return self._result(
                DiagnosisCause.POWER,
                _CONF_POWER,
                evidence,
                f"The GPU is maxed out ({_fmt(util)}% utilized) with its power limit at "
                f"{_fmt(t.power_limit_pct)}% — it looks power-limited.",
            )

        # --- voltage ---------------------------------------------------------
        if (
            util is not None
            and util >= HIGH_UTIL_PCT
            and t.voltage_mv is not None
            and t.voltage_mv >= VOLTAGE_CEILING_HINT_MV
        ):
            evidence.append(f"utilization_pct={_fmt(util)}% (pinned)")
            evidence.append(
                f"voltage_mv={_fmt(t.voltage_mv)}mV (near the top of the v/f curve)"
            )
            return self._result(
                DiagnosisCause.VOLTAGE,
                _CONF_VOLTAGE,
                evidence,
                f"The GPU is maxed out and its core voltage is pinned near "
                f"{_fmt(t.voltage_mv)}mV — voltage is the likely ceiling.",
            )

        # --- low utilization: CPU-limited vs application-behavior ------------
        if util is not None and util < LOW_UTIL_PCT:
            evidence.append(f"utilization_pct={_fmt(util)}% (well below max)")
            mem_ratio = None
            if t.memory_used_mb is not None and t.memory_total_mb:
                mem_ratio = t.memory_used_mb / t.memory_total_mb
            if mem_ratio is not None and mem_ratio >= VRAM_PRESSURE_RATIO:
                evidence.append(
                    f"memory_used_mb={_fmt(t.memory_used_mb, 0)}MB of "
                    f"{_fmt(t.memory_total_mb, 0)}MB ({mem_ratio * 100:.0f}%)"
                )
                return self._result(
                    DiagnosisCause.APP_BEHAVIOR,
                    _CONF_APP,
                    evidence,
                    f"GPU utilization is only {_fmt(util)}% while VRAM is nearly full "
                    f"({mem_ratio * 100:.0f}%) — the application/game itself is the "
                    "likely limiter.",
                )
            if t.core_clock_mhz is not None:
                evidence.append(f"core_clock_mhz={_fmt(t.core_clock_mhz, 0)}")
            return self._result(
                DiagnosisCause.CPU_LIMITED,
                _CONF_CPU,
                evidence,
                f"GPU utilization is only {_fmt(util)}% with no thermal/power/voltage "
                "limit in sight — the CPU (not observable through Afterburner) is the "
                "likely limiter.",
            )

        # --- fully utilized, nothing else visible -----------------------------
        if util is not None and util >= HIGH_UTIL_PCT:
            evidence.append(f"utilization_pct={_fmt(util)}% (pinned)")
            if t.core_clock_mhz is not None:
                evidence.append(f"core_clock_mhz={_fmt(t.core_clock_mhz, 0)}MHz")
            return self._result(
                DiagnosisCause.UTILIZATION_BOTTLENECK,
                _CONF_UTIL_BOTTLENECK,
                evidence,
                f"The GPU is fully busy ({_fmt(util)}% utilized) with no thermal, power, "
                "or voltage limit visible — the GPU itself is the bottleneck.",
            )

        # --- mid utilization, no limiter evident ------------------------------
        if temp is not None:
            evidence.append(f"temperature_c={_fmt(temp)}°C")
        if util is not None:
            evidence.append(f"utilization_pct={_fmt(util)}%")
        evidence.append(
            "no single limiter is evident from this sample; "
            "utilization is neither pinned nor idle"
        )
        return self._unknown(evidence)

    # ----------------------------------------------------------------------
    @staticmethod
    def _unknown(evidence: Sequence[str]) -> Diagnosis:
        return Diagnosis(
            cause=DiagnosisCause.UNKNOWN_INSUFFICIENT_DATA,
            confidence=_CONF_UNKNOWN_NO_DATA,
            evidence=tuple(evidence),
            summary="I don't have enough live data to be sure what's limiting the GPU.",
        )

    @staticmethod
    def _result(
        cause: DiagnosisCause,
        confidence: float,
        evidence: Sequence[str],
        summary: str,
    ) -> Diagnosis:
        return Diagnosis(
            cause=cause,
            confidence=confidence,
            evidence=tuple(evidence),
            summary=summary,
        )
