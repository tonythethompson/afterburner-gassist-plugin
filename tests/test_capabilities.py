"""Task 4.2 / 4.3 — capability detection & gating (design Property 2)."""
from __future__ import annotations

from dataclasses import replace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from afterburner.integration.fake import FakeAfterburner, default_capabilities
from afterburner.models import (
    ControlFeature,
    ErrorCode,
    GpuCapabilities,
    GpuLimits,
    InterfaceStatus,
    PluginError,
    Range,
)
from afterburner.safety.capabilities import HardwareCapabilityResolver
from afterburner.safety.validator import TuningValidator

FEATURES = [
    ControlFeature.POWER_LIMIT,
    ControlFeature.CORE_OFFSET,
    ControlFeature.MEMORY_OFFSET,
    ControlFeature.VOLTAGE,
    ControlFeature.FAN_PERCENT,
    ControlFeature.FAN_CURVE,
    ControlFeature.PROFILE_LOAD,
    ControlFeature.PROFILE_RESET,
]

WRITE_FEATURES = {
    ControlFeature.POWER_LIMIT,
    ControlFeature.CORE_OFFSET,
    ControlFeature.MEMORY_OFFSET,
    ControlFeature.VOLTAGE,
    ControlFeature.FAN_PERCENT,
    ControlFeature.FAN_CURVE,
}


def make_resolver(fake: FakeAfterburner, **kw) -> HardwareCapabilityResolver:
    return HardwareCapabilityResolver(fake, **kw)


class TestCapabilityResolution:
    def test_supported_features_require_reported_ranges(self) -> None:
        caps = default_capabilities(
            supported=(
                ControlFeature.POWER_LIMIT,
                ControlFeature.CORE_OFFSET,
                ControlFeature.MEMORY_OFFSET,
                ControlFeature.VOLTAGE,
                ControlFeature.FAN_PERCENT,
                ControlFeature.FAN_CURVE,
            ),
            limits=GpuLimits(
                power_limit_pct=Range(50, 118),
                # core_offset intentionally missing -> must NOT be offered
                memory_offset_mhz=Range(-500, 1000),
                fan_percent=Range(0, 100),
                # fan_temp_c intentionally missing -> advertised FAN_CURVE not offered
                fan_curve_max_points=2,
            ),
        )
        fake = FakeAfterburner()
        fake.set_capabilities(caps)
        resolved = make_resolver(fake).resolve(0)
        assert resolved.supports(ControlFeature.POWER_LIMIT)
        assert not resolved.supports(ControlFeature.CORE_OFFSET)
        assert resolved.supports(ControlFeature.MEMORY_OFFSET)
        # FAN_CURVE needs both fan axes even though advertised:
        assert not resolved.supports(ControlFeature.FAN_CURVE)

    def test_inverted_range_is_unavailable(self) -> None:
        caps = default_capabilities(
            supported=(ControlFeature.POWER_LIMIT,),
            limits=GpuLimits(power_limit_pct=Range(118, 50)),  # inverted
        )
        fake = FakeAfterburner()
        fake.set_capabilities(caps)
        resolved = make_resolver(fake).resolve(0)
        assert not resolved.supports(ControlFeature.POWER_LIMIT)

    def test_macm_unavailable_degrades_to_read_only(self) -> None:
        caps = default_capabilities(
            supported=(
                ControlFeature.POWER_LIMIT,
                ControlFeature.CORE_OFFSET,
                ControlFeature.FAN_PERCENT,
                ControlFeature.FAN_CURVE,
                ControlFeature.PROFILE_LOAD,
                ControlFeature.PROFILE_RESET,
            ),
            control_interface_status=InterfaceStatus.NOT_RUNNING,
        )
        fake = FakeAfterburner()
        fake.set_capabilities(caps)
        resolved = make_resolver(fake).resolve(0)
        assert resolved.supported_controls == frozenset()  # read-only
        assert resolved.control_interface_status is InterfaceStatus.NOT_RUNNING

    def test_profiles_gated_on_control_interface(self) -> None:
        caps = default_capabilities(
            supported=(ControlFeature.PROFILE_LOAD, ControlFeature.PROFILE_RESET),
            control_interface_status=InterfaceStatus.OK,
        )
        fake = FakeAfterburner()
        fake.set_capabilities(caps)
        resolved = make_resolver(fake).resolve(0)
        assert resolved.supports(ControlFeature.PROFILE_LOAD)
        assert resolved.supports(ControlFeature.PROFILE_RESET)

    def test_detect_not_ok_yields_read_only_with_status(self) -> None:
        for status in (
            InterfaceStatus.NOT_INSTALLED,
            InterfaceStatus.NOT_RUNNING,
            InterfaceStatus.UNSUPPORTED_VERSION,
            InterfaceStatus.ACCESS_DENIED,
        ):
            fake = FakeAfterburner(status=status)
            resolved = make_resolver(fake).resolve(0)
            assert resolved.supported_controls == frozenset()
            assert resolved.control_interface_status is status

    def test_resolution_timeout_degrades_to_read_only(self) -> None:
        fake = FakeAfterburner()
        fake.set_capabilities(default_capabilities(supported=tuple(FEATURES)))
        # clock jumps past the budget after the first call
        times = iter([0.0, 10.0])
        resolver = HardwareCapabilityResolver(fake, timeout=0.5, clock=lambda: next(times))
        resolved = resolver.resolve(0)
        assert resolver.last_timed_out is True
        assert resolved.supported_controls == frozenset()
        assert resolved.control_interface_status is InterfaceStatus.UNAVAILABLE

    def test_adapter_error_degrades_read_only(self) -> None:
        fake = FakeAfterburner(status=InterfaceStatus.OK)
        fake.caps_by_gpu.pop(0, None)  # read_capabilities raises UNSUPPORTED_GPU
        resolved = make_resolver(fake).resolve(0)
        assert resolved.supported_controls == frozenset()
        assert resolved.control_interface_status is InterfaceStatus.UNAVAILABLE

    def test_fan_curve_max_points_capped_at_32(self) -> None:
        caps = default_capabilities(
            supported=(ControlFeature.FAN_CURVE,),
            limits=GpuLimits(
                fan_percent=Range(0, 100),
                fan_temp_c=Range(30, 90),
                fan_curve_max_points=64,  # reported beyond the absolute cap
            ),
        )
        fake = FakeAfterburner()
        fake.set_capabilities(caps)
        assert make_resolver(fake).resolve(0).supports(ControlFeature.FAN_CURVE)


# ---------------------------------------------------------------------------
# Property 2: unsupported features are never actuated.
# ---------------------------------------------------------------------------


@st.composite
def caps_and_resolved(draw):
    advertised = frozenset(draw(st.sets(st.sampled_from(FEATURES), max_size=len(FEATURES))))
    # Random sane-or-missing ranges per single-value feature.
    def maybe_range():
        if draw(st.booleans()):
            lo = draw(st.integers(min_value=-1000, max_value=1000))
            hi = lo + draw(st.integers(min_value=0, max_value=2000))
            return Range(float(lo), float(hi))
        return None

    limits = GpuLimits(
        power_limit_pct=maybe_range(),
        core_offset_mhz=maybe_range(),
        memory_offset_mhz=maybe_range(),
        voltage_mv=maybe_range(),
        fan_percent=maybe_range(),
        fan_temp_c=maybe_range(),
        fan_curve_max_points=draw(st.sampled_from([None, 1, 2, 3, 32, 64])),
    )
    control_ok = draw(st.booleans())
    caps = GpuCapabilities(
        gpu_index=0,
        gpu_name="random gpu",
        supported_controls=frozenset(advertised),
        limits=limits,
        control_interface_status=(
            InterfaceStatus.OK if control_ok else InterfaceStatus.UNAVAILABLE
        ),
    )
    fake = FakeAfterburner()
    fake.set_capabilities(caps)
    resolved = HardwareCapabilityResolver(fake).resolve(0)
    return fake, resolved


class TestProperty2CapabilityGating:
    @given(st.data())
    def test_unsupported_feature_never_writes_and_raises_typed_error(self, data) -> None:
        fake, resolved = data.draw(caps_and_resolved())
        validator = TuningValidator(HardwareCapabilityResolver(fake))
        for feature in FEATURES:
            if not resolved.supports(feature):
                with pytest.raises(PluginError) as excinfo:
                    validator.validate_and_clamp(0, feature, 50.0)
                assert excinfo.value.code is ErrorCode.UNSUPPORTED_FEATURE
        assert fake.applied == []  # no adapter write ever occurred

    @given(st.data())
    def test_supported_single_value_features_have_a_range(self, data) -> None:
        _, resolved = data.draw(caps_and_resolved())
        for feature in WRITE_FEATURES - {ControlFeature.FAN_CURVE}:
            if resolved.supports(feature):
                rng = resolved.limits.range_for(feature)
                assert rng is not None and not rng.inverted
