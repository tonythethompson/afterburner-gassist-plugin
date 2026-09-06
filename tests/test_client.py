"""Task 7 — AfterburnerClient facade: typed errors, graceful degradation."""
from __future__ import annotations

import pytest

from afterburner.integration.client import AfterburnerClient
from afterburner.integration.fake import FakeAfterburner, default_capabilities
from afterburner.models import (
    ControlFeature,
    ErrorCode,
    FanCurve,
    FanCurvePoint,
    GpuTelemetry,
    InterfaceStatus,
    PluginError,
    TuningState,
)


class TestNoInterfaceInjected:
    def test_detect_reports_not_installed(self) -> None:
        client = AfterburnerClient()
        assert client.detect() is InterfaceStatus.NOT_INSTALLED

    def test_afterburner_dependent_ops_rejected(self) -> None:
        client = AfterburnerClient()
        with pytest.raises(PluginError) as excinfo:
            client.read_telemetry(0)
        assert excinfo.value.code is ErrorCode.INTERFACE_UNAVAILABLE

        with pytest.raises(PluginError) as excinfo:
            client.apply_control(0, ControlFeature.FAN_PERCENT, 50.0)
        assert excinfo.value.code is ErrorCode.INTERFACE_UNAVAILABLE

        with pytest.raises(PluginError) as excinfo:
            client.list_profiles()
        assert excinfo.value.code is ErrorCode.INTERFACE_UNAVAILABLE


class TestDelegation:
    def setup_method(self) -> None:
        self.fake = FakeAfterburner()
        telemetry = GpuTelemetry(
            gpu_index=0, gpu_name="Fake", temperature_c=61.0, driver_version="552.22"
        )
        self.fake.set_telemetry(telemetry)
        self.fake.set_capabilities(default_capabilities(0))
        self.fake.set_tuning(TuningState(gpu_index=0, power_limit_pct=100.0))
        self.fake.add_profile(1)
        self.client = AfterburnerClient(self.fake)

    def test_detect_and_version(self) -> None:
        assert self.client.detect() is InterfaceStatus.OK
        assert self.client.get_version() == "4.6.7.17439"

    def test_reads_delegate(self) -> None:
        t = self.client.read_telemetry(0)
        assert t.temperature_c == 61.0
        caps = self.client.read_capabilities(0)
        assert caps.supports(ControlFeature.POWER_LIMIT)
        state = self.client.read_tuning_state(0)
        assert state.power_limit_pct == 100.0
        assert [p.id for p in self.client.list_profiles()] == [1]

    def test_apply_control_delegates_and_records(self) -> None:
        result = self.client.apply_control(0, ControlFeature.FAN_PERCENT, 40.0)
        assert result.applied is True
        assert self.fake.applied == [(0, ControlFeature.FAN_PERCENT, 40.0)]

    def test_apply_fan_curve_delegates(self) -> None:
        curve = FanCurve(points=(FanCurvePoint(40.0, 30.0), FanCurvePoint(80.0, 90.0)))
        result = self.client.apply_fan_curve(0, curve)
        assert result.applied is True
        assert len(self.fake.applied_fan_curves) == 1

    def test_apply_fan_auto_delegates(self) -> None:
        self.fake.set_tuning(TuningState(gpu_index=0, fan_mode="manual", fan_percent=40.0))
        result = self.client.apply_fan_auto(0)
        assert result.applied is True
        assert self.fake.applied_fan_auto == [0]

    def test_plugin_errors_pass_through(self) -> None:
        self.fake.status = InterfaceStatus.NOT_RUNNING
        with pytest.raises(PluginError) as excinfo:
            self.client.read_telemetry(0)
        assert excinfo.value.code is ErrorCode.NOT_RUNNING

    def test_unexpected_exception_becomes_typed_comm_failure(self) -> None:
        self.fake.read_errors[0] = ValueError("boom")  # raw, unexpected failure
        with pytest.raises(PluginError) as excinfo:
            self.client.read_telemetry(0)
        assert excinfo.value.code is ErrorCode.COMM_FAILURE
        assert "boom" in excinfo.value.detail
