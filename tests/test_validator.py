"""Task 5.3/5.4/5.5 — clamping, fan curves; Properties 1 & 3."""
from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from afterburner.integration.fake import FakeAfterburner, default_capabilities
from afterburner.models import (
    ControlFeature,
    ErrorCode,
    FanCurve,
    FanCurvePoint,
    GpuLimits,
    PluginError,
    Range,
)
from afterburner.safety.capabilities import HardwareCapabilityResolver
from afterburner.safety.validator import TuningValidator


def validator_for(limits: GpuLimits, supported=...) -> TuningValidator:
    fake = FakeAfterburner()
    if supported is ...:
        supported = (
            ControlFeature.POWER_LIMIT,
            ControlFeature.CORE_OFFSET,
            ControlFeature.MEMORY_OFFSET,
            ControlFeature.VOLTAGE,
            ControlFeature.FAN_PERCENT,
            ControlFeature.FAN_CURVE,
        )
    fake.set_capabilities(default_capabilities(supported=supported, limits=limits))
    return TuningValidator(HardwareCapabilityResolver(fake)), fake


FULL_LIMITS = GpuLimits(
    power_limit_pct=Range(50.0, 118.0),
    core_offset_mhz=Range(-300.0, 300.0),
    memory_offset_mhz=Range(-500.0, 1000.0),
    voltage_mv=Range(0.0, 1100.0),
    fan_percent=Range(0.0, 100.0),
    fan_temp_c=Range(30.0, 90.0),
    fan_curve_max_points=2,
)


class TestClampBoundaries:
    def setup_method(self) -> None:
        self.validator, self.fake = validator_for(FULL_LIMITS)

    def test_in_range_value_passes_through(self) -> None:
        safe, clamped = self.validator.validate_and_clamp(0, ControlFeature.POWER_LIMIT, 100.0)
        assert safe == 100.0
        assert clamped is False

    def test_below_min_clamps_to_min(self) -> None:
        safe, clamped = self.validator.validate_and_clamp(0, ControlFeature.POWER_LIMIT, 1.0)
        assert safe == 50.0
        assert clamped is True

    def test_above_max_clamps_to_max(self) -> None:
        safe, clamped = self.validator.validate_and_clamp(0, ControlFeature.CORE_OFFSET, 900.0)
        assert safe == 300.0
        assert clamped is True

    def test_at_bounds_not_clamped(self) -> None:
        for value in (50.0, 118.0):
            safe, clamped = self.validator.validate_and_clamp(
                0, ControlFeature.POWER_LIMIT, value
            )
            assert safe == value
            assert clamped is False

    def test_negative_offsets_allowed(self) -> None:
        safe, clamped = self.validator.validate_and_clamp(0, ControlFeature.CORE_OFFSET, -350.0)
        assert safe == -300.0
        assert clamped is True

    def test_rejects_nan_and_inf(self) -> None:
        for bad in (math.nan, math.inf, -math.inf):
            with pytest.raises(PluginError) as excinfo:
                self.validator.validate_and_clamp(0, ControlFeature.POWER_LIMIT, bad)
            assert excinfo.value.code is ErrorCode.INVALID_VALUE

    def test_unsupported_feature_error(self) -> None:
        validator, _ = validator_for(
            FULL_LIMITS, supported=(ControlFeature.FAN_PERCENT,)
        )
        with pytest.raises(PluginError) as excinfo:
            validator.validate_and_clamp(0, ControlFeature.POWER_LIMIT, 100.0)
        assert excinfo.value.code is ErrorCode.UNSUPPORTED_FEATURE

    def test_missing_range_is_gated_off_as_unsupported(self) -> None:
        # The capability layer never offers a feature without a reported range (design
        # Capability Resolution), so the validator sees it as UNSUPPORTED_FEATURE — the
        # INTERFACE_UNAVAILABLE branch covers a reported-but-unreadable range changing
        # between resolution and validation.
        validator, _ = validator_for(
            GpuLimits(), supported=(ControlFeature.POWER_LIMIT,)
        )
        with pytest.raises(PluginError) as excinfo:
            validator.validate_and_clamp(0, ControlFeature.POWER_LIMIT, 100.0)
        assert excinfo.value.code is ErrorCode.UNSUPPORTED_FEATURE


class TestFanCurveValidation:
    def setup_method(self) -> None:
        self.validator, self.fake = validator_for(FULL_LIMITS)

    def _curve(self, *points) -> FanCurve:
        return FanCurve(points=tuple(FanCurvePoint(*p) for p in points))

    def test_valid_two_point_curve_accepted(self) -> None:
        curve = self.validator.validate_fan_curve(0, self._curve((40, 30), (80, 90)))
        assert len(curve.points) == 2

    def test_curve_points_clamped_to_axes(self) -> None:
        curve = self.validator.validate_fan_curve(
            0, self._curve((10, 0), (95, 200))  # out of temp/fan axes
        )
        assert curve.points[0] == FanCurvePoint(30.0, 0.0)  # temp clamped to min 30
        assert curve.points[1] == FanCurvePoint(90.0, 100.0)  # both clamped to max

    def test_too_few_points_rejected(self) -> None:
        with pytest.raises(PluginError) as excinfo:
            self.validator.validate_fan_curve(0, self._curve((40, 30)))
        assert excinfo.value.code is ErrorCode.INVALID_VALUE

    def test_too_many_points_rejected(self) -> None:
        points = [(40 + i, 30 + i) for i in range(3)]  # max_points default 2
        with pytest.raises(PluginError) as excinfo:
            self.validator.validate_fan_curve(0, self._curve(*points))
        assert excinfo.value.code is ErrorCode.INVALID_VALUE

    def test_non_monotonic_temperatures_rejected(self) -> None:
        with pytest.raises(PluginError) as excinfo:
            self.validator.validate_fan_curve(0, self._curve((80, 90), (40, 30)))
        assert excinfo.value.code is ErrorCode.INVALID_VALUE

    def test_fan_curve_unsupported(self) -> None:
        validator, _ = validator_for(
            FULL_LIMITS, supported=(ControlFeature.FAN_PERCENT,)
        )
        with pytest.raises(PluginError) as excinfo:
            validator.validate_fan_curve(0, self._curve((40, 30), (80, 90)))
        assert excinfo.value.code is ErrorCode.UNSUPPORTED_FEATURE

    def test_failure_leaves_no_write(self) -> None:
        with pytest.raises(PluginError):
            self.validator.validate_fan_curve(0, self._curve((80, 90), (40, 30)))
        assert self.fake.applied_fan_curves == []
        assert self.fake.applied == []


# ---------------------------------------------------------------------------
# Property 1: clamping invariant — no raw LLM value reaches hardware.
# ---------------------------------------------------------------------------

NON_FINITE = st.one_of(
    st.just(float("nan")),
    st.just(float("inf")),
    st.just(float("-inf")),
)

FINITE_FLOATS = st.floats(
    min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False
)


@st.composite
def valid_range(draw):
    lo = draw(st.floats(min_value=-1000.0, max_value=500.0, allow_nan=False,
                        allow_infinity=False))
    hi = draw(st.floats(min_value=lo, max_value=1000.0, allow_nan=False,
                        allow_infinity=False))
    return Range(lo, hi)


@st.composite
def full_limits_and_feature(draw):
    feature = draw(st.sampled_from(
        [
            ControlFeature.POWER_LIMIT,
            ControlFeature.CORE_OFFSET,
            ControlFeature.MEMORY_OFFSET,
            ControlFeature.VOLTAGE,
            ControlFeature.FAN_PERCENT,
        ]
    ))
    rng = draw(valid_range())
    limits = GpuLimits(
        power_limit_pct=rng if feature is ControlFeature.POWER_LIMIT else Range(0, 100),
        core_offset_mhz=rng if feature is ControlFeature.CORE_OFFSET else Range(-300, 300),
        memory_offset_mhz=rng if feature is ControlFeature.MEMORY_OFFSET else Range(0, 1000),
        voltage_mv=rng if feature is ControlFeature.VOLTAGE else Range(0, 1000),
        fan_percent=rng if feature is ControlFeature.FAN_PERCENT else Range(0, 100),
    )
    return feature, rng, limits


class TestProperty1ClampingInvariant:
    @given(feature_and_limits=full_limits_and_feature(), requested=st.floats(
        min_value=-1e9, max_value=1e9, allow_nan=False, allow_infinity=False))
    def test_value_handed_to_adapter_always_in_range(
        self, feature_and_limits, requested
    ) -> None:
        feature, rng, limits = feature_and_limits
        validator, fake = validator_for(limits)
        safe, clamped = validator.validate_and_clamp(0, feature, requested)
        assert rng.contains(safe)
        if rng.contains(requested):
            assert safe == requested
            assert clamped is False
        else:
            assert clamped is True

    @given(feature_and_limits=full_limits_and_feature(), bad=NON_FINITE)
    def test_non_finite_values_rejected(self, feature_and_limits, bad) -> None:
        feature, _, limits = feature_and_limits
        validator, fake = validator_for(limits)
        with pytest.raises(PluginError) as excinfo:
            validator.validate_and_clamp(0, feature, bad)
        assert excinfo.value.code is ErrorCode.INVALID_VALUE


# ---------------------------------------------------------------------------
# Property 3: fan-curve monotonicity and bounds.
# ---------------------------------------------------------------------------


@st.composite
def candidate_curves(draw):
    """Random candidate curves: length 0..5, arbitrary ordered or unordered points."""
    length = draw(st.integers(min_value=0, max_value=5))
    temps = draw(st.lists(
        st.floats(min_value=-50.0, max_value=200.0, allow_nan=False, allow_infinity=False),
        min_size=length,
        max_size=length,
    ))
    fans = draw(st.lists(
        st.floats(min_value=-50.0, max_value=200.0, allow_nan=False, allow_infinity=False),
        min_size=length,
        max_size=length,
    ))
    return FanCurve(
        points=tuple(FanCurvePoint(t, f) for t, f in zip(temps, fans))
    )


class TestProperty3FanCurveBounds:
    @given(curve=candidate_curves())
    def test_accepted_curves_are_monotonic_and_in_bounds(self, curve) -> None:
        validator, fake = validator_for(FULL_LIMITS)
        try:
            safe = validator.validate_fan_curve(0, curve)
        except PluginError as exc:
            assert exc.code is ErrorCode.INVALID_VALUE
            assert fake.applied_fan_curves == []  # nothing applied on rejection
            return
        points = safe.points
        assert len(points) >= 2
        temps = [p.temp_c for p in points]
        assert all(b >= a for a, b in zip(temps, temps[1:]))  # non-decreasing
        for p in points:
            assert FULL_LIMITS.fan_temp_c.contains(p.temp_c)
            assert FULL_LIMITS.fan_percent.contains(p.fan_percent)

    @given(curve=candidate_curves())
    def test_rejected_when_too_few_or_non_monotonic(self, curve) -> None:
        validator, _ = validator_for(FULL_LIMITS)
        try:
            safe = validator.validate_fan_curve(0, curve)
        except PluginError:
            return
        assert len(safe.points) >= 2
