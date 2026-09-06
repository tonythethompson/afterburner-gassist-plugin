"""Task 2.3 — unit tests for models and `Range`."""
from __future__ import annotations

import math

import pytest

from afterburner.models import (
    AuthorityState,
    ControlFeature,
    ErrorCode,
    FanCurve,
    FanCurvePoint,
    GpuCapabilities,
    GpuLimits,
    GpuTelemetry,
    InterfaceStatus,
    Profile,
    Range,
    TuningOwnershipReport,
    AfterburnerAuthoritySnapshot,
    TuningState,
)


class TestRange:
    def test_clamp_inside(self) -> None:
        r = Range(10.0, 20.0)
        assert r.clamp(15.0) == 15.0

    def test_clamp_below_and_above(self) -> None:
        r = Range(10.0, 20.0)
        assert r.clamp(5.0) == 10.0
        assert r.clamp(25.0) == 20.0

    def test_clamp_at_bounds(self) -> None:
        r = Range(10.0, 20.0)
        assert r.clamp(10.0) == 10.0
        assert r.clamp(20.0) == 20.0

    def test_contains(self) -> None:
        r = Range(10.0, 20.0)
        assert r.contains(10.0)
        assert r.contains(20.0)
        assert r.contains(15.0)
        assert not r.contains(9.999)
        assert not r.contains(20.001)

    def test_inverted_range(self) -> None:
        r = Range(20.0, 10.0)
        assert r.inverted
        # clamp on an inverted range is meaningless; callers treat it as unavailable
        assert not Range(1.0, 1.0).inverted


class TestGpuLimitsRangeFor:
    def test_known_features_map_to_ranges(self) -> None:
        limits = GpuLimits(
            power_limit_pct=Range(50, 118),
            core_offset_mhz=Range(-300, 300),
            memory_offset_mhz=Range(-500, 1000),
            voltage_mv=Range(0, 1100),
            fan_percent=Range(0, 100),
        )
        assert limits.range_for(ControlFeature.POWER_LIMIT) == Range(50, 118)
        assert limits.range_for(ControlFeature.CORE_OFFSET) == Range(-300, 300)
        assert limits.range_for(ControlFeature.MEMORY_OFFSET) == Range(-500, 1000)
        assert limits.range_for(ControlFeature.VOLTAGE) == Range(0, 1100)
        assert limits.range_for(ControlFeature.FAN_PERCENT) == Range(0, 100)

    def test_non_single_value_features_have_no_range(self) -> None:
        limits = GpuLimits()
        assert limits.range_for(ControlFeature.FAN_CURVE) is None
        assert limits.range_for(ControlFeature.PROFILE_LOAD) is None
        assert limits.range_for(ControlFeature.PROFILE_RESET) is None

    def test_missing_range_is_none(self) -> None:
        assert GpuLimits().range_for(ControlFeature.POWER_LIMIT) is None


class TestGpuCapabilitiesSupports:
    def test_supports(self) -> None:
        caps = GpuCapabilities(
            gpu_index=0,
            gpu_name="RTX",
            supported_controls=frozenset({ControlFeature.POWER_LIMIT}),
            limits=GpuLimits(),
            control_interface_status=InterfaceStatus.OK,
        )
        assert caps.supports(ControlFeature.POWER_LIMIT)
        assert not caps.supports(ControlFeature.CORE_OFFSET)


class TestOwnershipModels:
    def test_external_authorities_default_to_unknown_not_observable(self) -> None:
        report = TuningOwnershipReport(
            gpu_index=0,
            afterburner=AfterburnerAuthoritySnapshot(
                interface_status=InterfaceStatus.OK, applied_state=TuningState(gpu_index=0)
            ),
        )
        assert report.nvidia_app_auto_tuning is AuthorityState.UNKNOWN_NOT_OBSERVABLE
        assert report.gassist_native_tuning is AuthorityState.UNKNOWN_NOT_OBSERVABLE
        assert report.other_oc_utilities is AuthorityState.UNKNOWN_NOT_OBSERVABLE


class TestValueSanity:
    def test_enum_values_match_spec(self) -> None:
        assert ErrorCode.INVALID_VALUE.value == "invalid_tuning_value"
        assert ErrorCode.UNSUPPORTED_FEATURE.value == "unsupported_feature"
        assert InterfaceStatus.OK.value == "ok"
        assert AuthorityState.UNKNOWN_NOT_OBSERVABLE.value == "unknown_not_observable"

    def test_nan_is_rejected_upstream_not_in_range(self) -> None:
        # Range.clamp is pure comparison math (Python min/max with NaN return the first
        # operand, so the result is meaningless). NaN handling belongs to the validator,
        # which rejects non-finite values before clamp is ever called.
        value = Range(0.0, 100.0).clamp(math.nan)
        assert isinstance(value, float)  # no crash; meaningless under NaN — see validator
        # The validator is the guard rail:
        from afterburner.integration.fake import default_capabilities
        from afterburner.integration.fake import FakeAfterburner
        from afterburner.models import ErrorCode, PluginError
        from afterburner.safety.capabilities import HardwareCapabilityResolver
        from afterburner.safety.validator import TuningValidator

        fake = FakeAfterburner()
        fake.set_capabilities(default_capabilities(0))
        validator = TuningValidator(HardwareCapabilityResolver(fake))
        with pytest.raises(PluginError) as excinfo:
            validator.validate_and_clamp(0, ControlFeature.POWER_LIMIT, math.nan)
        assert excinfo.value.code is ErrorCode.INVALID_VALUE

    def test_fan_curve_and_profile_shapes(self) -> None:
        curve = FanCurve(points=(FanCurvePoint(40.0, 30.0), FanCurvePoint(80.0, 90.0)))
        assert len(curve.points) == 2
        profile = Profile(id=1, name="Profile 1", is_active=True)
        assert profile.kind == "hardware"

    def test_telemetry_has_driver_version_and_stale_flag(self) -> None:
        t = GpuTelemetry(gpu_index=0, gpu_name="GPU", driver_version="552.22")
        assert t.driver_version == "552.22"
        assert t.is_stale is False
        assert t.sampled_at is not None
