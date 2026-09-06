"""Validation & clamping (design "Tuning Validation & Clamping", "Fan Curve Validation").

Clamping invariant (Property 1): no raw LLM value reaches hardware — every value is clamped to
Afterburner-reported [min, max] first. Non-finite / non-numeric values are INVALID_VALUE errors;
finite out-of-range values are clamped and reported back as (safe_value, clamped=True).
Fan curves must be supported, use 2..fan_curve_max_points points (adapter-reported max, default
2, never over 32), be non-decreasing in temperature, and every point clamped into the reported
temp/fan axes. On failure a typed error is raised and no configuration is changed.
"""
from __future__ import annotations

import math
from typing import Tuple

from ..models import (
    ControlFeature,
    ErrorCode,
    FanCurve,
    FanCurvePoint,
    GpuCapabilities,
    PluginError,
)
from .capabilities import FAN_CURVE_ABSOLUTE_MAX_POINTS, HardwareCapabilityResolver


class TuningValidator:
    """Validates and clamps requested tuning values against Afterburner-reported limits."""

    def __init__(self, resolver: HardwareCapabilityResolver) -> None:
        self._resolver = resolver

    # ----------------------------------------------------------------------
    def validate_and_clamp(
        self, gpu_index: int, feature: ControlFeature, requested_value: float
    ) -> Tuple[float, bool]:
        """Validate + clamp a single-value control request.

        Returns (safe_value, clamped). Raises PluginError:
        UNSUPPORTED_FEATURE (not supported), INTERFACE_UNAVAILABLE (no reported range),
        INVALID_VALUE (NaN / ±inf).
        """
        caps = self._resolver.resolve(gpu_index)
        if not caps.supports(feature):
            raise PluginError(
                ErrorCode.UNSUPPORTED_FEATURE,
                "That control isn't available on this hardware/version.",
                detail=f"feature={feature.value}",
            )

        rng = caps.limits.range_for(feature)
        if rng is None or rng.inverted:
            raise PluginError(
                ErrorCode.INTERFACE_UNAVAILABLE,
                "Afterburner is installed but its control interface is unavailable. "
                "Start MSI Afterburner and try again.",
                detail=f"no reported range for {feature.value}",
            )

        if not _is_finite_number(requested_value):
            raise PluginError(
                ErrorCode.INVALID_VALUE,
                "That value isn't valid for this control.",
                detail=f"non-finite requested value {requested_value!r}",
            )

        safe_value = rng.clamp(requested_value)
        return safe_value, safe_value != requested_value

    # ----------------------------------------------------------------------
    def validate_fan_curve(self, gpu_index: int, curve: FanCurve) -> FanCurve:
        """Validate + clamp a fan curve; returns a new safe curve or raises PluginError."""
        caps = self._resolver.resolve(gpu_index)
        if not caps.supports(ControlFeature.FAN_CURVE):
            raise PluginError(
                ErrorCode.UNSUPPORTED_FEATURE,
                "Fan curve control isn't available on this setup.",
            )
        return self._validate_fan_curve_against_caps(caps, curve)

    def _validate_fan_curve_against_caps(
        self, caps: GpuCapabilities, curve: FanCurve
    ) -> FanCurve:
        limits = caps.limits
        max_points = limits.fan_curve_max_points
        if max_points is None:
            max_points = 2
        max_points = min(max_points, FAN_CURVE_ABSOLUTE_MAX_POINTS)

        points = list(curve.points)
        if len(points) < 2 or len(points) > max_points:
            raise PluginError(
                ErrorCode.INVALID_VALUE,
                f"A fan curve needs between 2 and {max_points} points on this setup.",
                detail=f"got {len(points)} points",
            )

        temp_range = limits.fan_temp_c
        fan_range = limits.fan_percent
        if temp_range is None or fan_range is None or temp_range.inverted or fan_range.inverted:
            raise PluginError(
                ErrorCode.INTERFACE_UNAVAILABLE,
                "Afterburner is installed but its control interface is unavailable. "
                "Start MSI Afterburner and try again.",
                detail="missing fan temp/percent axes",
            )

        safe_points = []
        prev_temp = -math.inf
        for p in points:
            if not _is_finite_number(p.temp_c) or not _is_finite_number(p.fan_percent):
                raise PluginError(
                    ErrorCode.INVALID_VALUE,
                    "That value isn't valid for this control.",
                    detail="non-finite fan-curve point",
                )
            if p.temp_c < prev_temp:
                raise PluginError(
                    ErrorCode.INVALID_VALUE,
                    "Fan curve temperatures must go up, not down.",
                    detail=f"{p.temp_c} < previous {prev_temp}",
                )
            safe_points.append(
                FanCurvePoint(
                    temp_c=temp_range.clamp(p.temp_c),
                    fan_percent=fan_range.clamp(p.fan_percent),
                )
            )
            prev_temp = p.temp_c

        return FanCurve(points=tuple(safe_points))


def _is_finite_number(value: float) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False
