"""Protocol V2 SDK patterns: @plugin.command, stream, on_input, set_keep_session.

The afterburner package stays import-free of gassist_sdk; bind_sdk_plugin is duck-typed
so these tests bind a fake SDK object.
"""
from __future__ import annotations

from pathlib import Path

from afterburner.integration.fake import FakeAfterburner, default_capabilities
from afterburner.models import ControlFeature, GpuTelemetry, InterfaceStatus, TuningState
from afterburner.protocol.plugin import (
    CommandFailed,
    GAssistPlugin,
    bind_sdk_plugin,
    build_services,
)

PROFILES = Path("tests/fixtures/profiles/startup_enabled")

MANIFEST_FUNCTIONS = (
    "get_gpu_status",
    "list_gpus",
    "get_gpu_limits",
    "get_tuning_state",
    "get_profiles",
    "get_tuning_ownership",
    "show_configuration",
    "load_profile",
    "reset_tuning",
    "set_power_limit",
    "set_core_offset",
    "set_memory_offset",
    "set_fan_percent",
    "set_fan_auto",
    "set_fan_curve",
    "optimize_quiet",
    "optimize_thermal",
    "diagnose_performance",
)


class FakeSdk:
    def __init__(self) -> None:
        self.commands: dict[str, object] = {}
        self.streamed: list[str] = []
        self.keep_session = False

    def command(self, name: str | None = None, description: str | None = None):
        def decorator(func):
            self.commands[name or func.__name__] = func
            return func

        return decorator

    def stream(self, data: str) -> None:
        self.streamed.append(data)

    def set_keep_session(self, keep: bool) -> None:
        self.keep_session = keep


def make_bound():
    fake = FakeAfterburner(status=InterfaceStatus.OK)
    fake.set_capabilities(default_capabilities())
    fake.set_telemetry(
        GpuTelemetry(
            gpu_index=0,
            gpu_name="Fake",
            temperature_c=60.0,
            utilization_pct=80.0,
            core_clock_mhz=1500.0,
            fan_percent=50.0,
        )
    )
    fake.set_tuning(
        TuningState(
            gpu_index=0,
            power_limit_pct=100.0,
            core_offset_mhz=0.0,
            memory_offset_mhz=0.0,
            fan_mode="manual",
            fan_percent=50.0,
        )
    )
    services = build_services(fake, PROFILES)
    core = GAssistPlugin(services)
    sdk = FakeSdk()
    bind_sdk_plugin(sdk, core)
    return fake, core, sdk


class TestBindSdkPlugin:
    def test_registers_manifest_commands_and_on_input(self) -> None:
        _fake, _core, sdk = make_bound()
        for name in MANIFEST_FUNCTIONS:
            assert name in sdk.commands
        assert "on_input" in sdk.commands
        assert set(MANIFEST_FUNCTIONS).isdisjoint({"on_input"})

    def test_read_command_returns_text_and_does_not_keep_session(self) -> None:
        _fake, _core, sdk = make_bound()
        text = sdk.commands["get_gpu_status"](gpu_index=0)
        assert isinstance(text, str)
        assert "60.0" in text
        assert sdk.keep_session is False

    def test_risky_command_keeps_session_and_streams_prompt(self) -> None:
        fake, _core, sdk = make_bound()
        text = sdk.commands["set_power_limit"](percent=110)
        assert sdk.keep_session is True
        assert "Reply 'confirm'" in text
        assert sdk.streamed
        assert fake.applied == []

    def test_on_input_confirm_applies_and_exits_passthrough(self) -> None:
        fake, _core, sdk = make_bound()
        sdk.commands["optimize_quiet"]()
        assert sdk.keep_session is True
        text = sdk.commands["on_input"](content="confirm")
        assert "Fan lowered" in text
        assert sdk.keep_session is False
        assert "Looking for a quieter fan setting..." in sdk.streamed
        assert (0, ControlFeature.FAN_PERCENT, 40.0) in {
            (g, f, v) for (g, f, v) in fake.applied
        }

    def test_on_input_cancel_never_writes(self) -> None:
        fake, _core, sdk = make_bound()
        sdk.commands["set_power_limit"](percent=110)
        text = sdk.commands["on_input"](content="cancel")
        assert "cancelled" in text.lower()
        assert sdk.keep_session is False
        assert fake.applied == []

    def test_domain_failure_is_command_failed(self) -> None:
        fake = FakeAfterburner(status=InterfaceStatus.OK)
        fake.set_capabilities(default_capabilities())
        fake.telemetry_fail_all = True
        services = build_services(fake, PROFILES)
        core = GAssistPlugin(services)
        sdk = FakeSdk()
        bind_sdk_plugin(sdk, core)
        try:
            sdk.commands["get_gpu_status"](gpu_index=0)
        except CommandFailed as exc:
            assert exc.message
            assert isinstance(exc.code, int)
        else:  # pragma: no cover
            raise AssertionError("expected CommandFailed")

    def test_diagnose_streams_progress(self) -> None:
        _fake, _core, sdk = make_bound()
        text = sdk.commands["diagnose_performance"]()
        assert sdk.streamed == ["Checking live GPU telemetry..."]
        assert "classified:" in text
        assert sdk.keep_session is False
