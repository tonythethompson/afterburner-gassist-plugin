"""Plan task 11.2 — optimize_quiet / optimize_thermal unit tests.

Asserts the fan/thermal-only rule (Req 11.5): clock/voltage/power/memory tuning parameters
are never modified — every write recorded by the fake is a FAN_PERCENT write.
"""
from __future__ import annotations

import pytest

from afterburner.integration.client import AfterburnerClient
from afterburner.integration.fake import FakeAfterburner, default_capabilities
from afterburner.models import (
    ControlFeature,
    ErrorCode,
    GpuLimits,
    GpuTelemetry,
    PluginError,
    Range,
    TuningState,
)
from afterburner.services.optimize import OptimizeService


def telemetry(*, temperature_c: float, fan_percent: float) -> GpuTelemetry:
    return GpuTelemetry(
        gpu_index=0,
        gpu_name="Fake",
        temperature_c=temperature_c,
        fan_percent=fan_percent,
        utilization_pct=60.0,
        core_clock_mhz=1500.0,
    )


def fake_for(temperature_c: float, fan_percent: float) -> FakeAfterburner:
    fake = FakeAfterburner()
    fake.set_capabilities(default_capabilities())
    fake.set_telemetry(telemetry(temperature_c=temperature_c, fan_percent=fan_percent))
    fake.set_tuning(
        TuningState(gpu_index=0, fan_mode="manual", fan_percent=fan_percent)
    )
    return fake


def make_service(fake: FakeAfterburner, **kw) -> OptimizeService:
    return OptimizeService(
        AfterburnerClient(interface=fake), sleep=lambda _: None, **kw
    )


def applied_features(fake: FakeAfterburner):
    return [feature for (_g, feature, _v) in fake.applied]


class TestOptimizeQuiet:
    def test_reduces_fan_by_at_least_ten_points(self) -> None:
        fake = fake_for(temperature_c=60.0, fan_percent=50.0)
        svc = make_service(fake)
        result = svc.optimize_quiet()

        assert result.applied is True
        assert result.feature is ControlFeature.FAN_PERCENT
        assert result.applied_value == 40.0  # 50 - 10
        assert fake.applied == [(0, ControlFeature.FAN_PERCENT, 40.0)]
        assert "Fan lowered from 50% to 40%" in result.message
        assert "60.0" in result.message  # verified measured temperature

    def test_refuses_when_temperature_already_above_the_limit(self) -> None:
        fake = fake_for(temperature_c=90.0, fan_percent=80.0)
        svc = make_service(fake)
        with pytest.raises(PluginError) as excinfo:
            svc.optimize_quiet()
        assert excinfo.value.code is ErrorCode.UNSUPPORTED_FEATURE
        assert fake.applied == []  # never reduced

    def test_refuses_when_fan_is_already_too_low_to_reduce(self) -> None:
        fake = fake_for(temperature_c=60.0, fan_percent=5.0)
        svc = make_service(fake)
        with pytest.raises(PluginError) as excinfo:
            svc.optimize_quiet()
        assert excinfo.value.code is ErrorCode.UNSUPPORTED_FEATURE
        assert fake.applied == []

    def test_restores_fan_when_reduction_pushes_temperature_over_the_limit(
        self,
    ) -> None:
        fake = fake_for(temperature_c=82.0, fan_percent=50.0)

        def _overheat_on_apply(gpu_index: int, feature: ControlFeature, value: float):
            # Lowering the fan lets the GPU heat up past the 83 C limit.
            fake.telemetry_by_gpu[gpu_index] = telemetry(
                temperature_c=85.0, fan_percent=value
            )
            return None

        fake.on_apply_control = _overheat_on_apply
        svc = make_service(fake, read_interval_s=0.0)

        with pytest.raises(PluginError) as excinfo:
            svc.optimize_quiet()
        assert excinfo.value.code is ErrorCode.UNSUPPORTED_FEATURE
        assert "restored" in excinfo.value.user_message
        # reduce to 40, then restore to 50 — never left overheated
        assert fake.applied == [
            (0, ControlFeature.FAN_PERCENT, 40.0),
            (0, ControlFeature.FAN_PERCENT, 50.0),
        ]

    def test_missing_temperature_is_a_typed_error_not_a_change(self) -> None:
        fake = fake_for(temperature_c=60.0, fan_percent=50.0)
        fake.telemetry_by_gpu[0] = GpuTelemetry(gpu_index=0, gpu_name="Fake",
                                                fan_percent=50.0)
        svc = make_service(fake)
        with pytest.raises(PluginError) as excinfo:
            svc.optimize_quiet()
        assert excinfo.value.code is ErrorCode.INTERFACE_UNAVAILABLE
        assert fake.applied == []


class TestOptimizeThermal:
    def test_rejects_out_of_range_and_non_numeric_targets(self) -> None:
        fake = fake_for(temperature_c=60.0, fan_percent=40.0)
        svc = make_service(fake)
        for bad in (39.0, 96.0, 0, 200, None, "70", True, [], float("nan")):
            with pytest.raises(PluginError) as excinfo:
                svc.optimize_thermal(bad)
            assert excinfo.value.code is ErrorCode.INVALID_VALUE
            assert "40" in excinfo.value.user_message and "95" in excinfo.value.user_message
        assert fake.applied == []  # Req 11.4: existing fan/thermal settings unchanged

    def test_in_range_targets_are_accepted(self) -> None:
        fake = fake_for(temperature_c=30.0, fan_percent=40.0)
        svc = make_service(fake)
        for target in (40.0, 70.0, 95.0):
            result = svc.optimize_thermal(target)
            assert result.applied is False  # already at/below target => no change needed
            assert fake.applied == []

    def test_raises_fan_stepwise_and_verifies_target_met(self) -> None:
        fake = fake_for(temperature_c=88.0, fan_percent=30.0)

        def _cool_with_fan(gpu_index: int, feature: ControlFeature, value: float):
            # Simple simulated response: each +20 fan point drops temperature by 20 C.
            fake.telemetry_by_gpu[gpu_index] = telemetry(
                temperature_c=88.0 - (value - 30.0), fan_percent=value
            )
            return None

        fake.on_apply_control = _cool_with_fan
        svc = make_service(fake, read_interval_s=0.0)

        result = svc.optimize_thermal(70.0)
        assert result.applied is True
        assert fake.applied == [(0, ControlFeature.FAN_PERCENT, 50.0)]
        assert "68.0" in result.message  # measured temperature reported (Req 11.3)

    def test_honest_failure_when_target_not_reachable_even_at_max_fan(self) -> None:
        fake = fake_for(temperature_c=90.0, fan_percent=20.0)  # telemetry never changes
        svc = make_service(fake, read_interval_s=0.0)
        with pytest.raises(PluginError) as excinfo:
            svc.optimize_thermal(40.0)
        assert excinfo.value.code is ErrorCode.UNSUPPORTED_FEATURE
        assert "reachable" in excinfo.value.user_message
        # Fans escalated 20 -> 40 -> 60 -> 80 -> 100, never a false success.
        assert applied_features(fake) == [ControlFeature.FAN_PERCENT] * 4
        assert fake.applied[-1] == (0, ControlFeature.FAN_PERCENT, 100.0)

    def test_missing_temperature_is_typed_error(self) -> None:
        fake = fake_for(temperature_c=60.0, fan_percent=40.0)
        fake.telemetry_by_gpu[0] = GpuTelemetry(gpu_index=0, gpu_name="Fake",
                                                fan_percent=40.0)
        svc = make_service(fake)
        with pytest.raises(PluginError) as excinfo:
            svc.optimize_thermal(70.0)
        assert excinfo.value.code is ErrorCode.INTERFACE_UNAVAILABLE
        assert fake.applied == []


class TestFanAndThermalOnly:
    def test_optimizations_never_touch_clock_voltage_power_or_memory(self) -> None:
        fake = fake_for(temperature_c=75.0, fan_percent=60.0)
        svc = make_service(fake, read_interval_s=0.0)

        # Quiet: fan 60 -> 50 (temperature static, fine).
        quiet = svc.optimize_quiet()
        assert quiet.applied is True
        # Thermal: already at/under an easy target -> no writes at all.
        thermal = svc.optimize_thermal(90.0)
        assert thermal.applied is False

        for (_g, feature, _v) in fake.applied:
            assert feature is ControlFeature.FAN_PERCENT  # Req 11.5
        assert fake.applied == [(0, ControlFeature.FAN_PERCENT, 50.0)]

    def test_fan_control_unsupported_is_a_typed_error_with_no_write(self) -> None:
        fake = FakeAfterburner()
        fake.set_capabilities(
            default_capabilities(
                supported=(ControlFeature.POWER_LIMIT,),
                limits=GpuLimits(power_limit_pct=Range(50.0, 118.0)),
            )
        )
        fake.set_telemetry(telemetry(temperature_c=60.0, fan_percent=50.0))
        fake.set_tuning(
            TuningState(gpu_index=0, fan_mode="manual", fan_percent=50.0)
        )
        svc = make_service(fake)
        with pytest.raises(PluginError) as excinfo:
            svc.optimize_quiet()
        assert excinfo.value.code is ErrorCode.UNSUPPORTED_FEATURE
        assert fake.applied == []
