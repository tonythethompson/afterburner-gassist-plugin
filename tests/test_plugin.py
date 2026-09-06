"""Plan tasks 15.3 / 15.4 — GAssistPlugin lifecycle, dispatch, confirmation, Property 7."""
from __future__ import annotations

import io
import json
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from afterburner.integration.fake import FakeAfterburner, default_capabilities
from afterburner.models import (
    ControlFeature,
    GpuTelemetry,
    InterfaceStatus,
    TuningState,
)
from afterburner.protocol.plugin import (
    GAssistPlugin,
    build_services,
    run_plugin_loop,
)

PROFILES = Path("tests/fixtures/profiles/startup_enabled")


class _Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def telemetry() -> GpuTelemetry:
    return GpuTelemetry(
        gpu_index=0,
        gpu_name="Fake",
        temperature_c=60.0,
        utilization_pct=80.0,
        core_clock_mhz=1500.0,
        fan_percent=50.0,
    )


def make_plugin(
    *,
    status: InterfaceStatus = InterfaceStatus.OK,
    tuning: TuningState | None = None,
    t: GpuTelemetry | None = None,
    clock: _Clock | None = None,
) -> tuple[FakeAfterburner, GAssistPlugin, _Clock]:
    fake = FakeAfterburner(status=status)
    fake.set_capabilities(default_capabilities())
    fake.set_telemetry(t or telemetry())
    fake.set_tuning(
        tuning
        or TuningState(
            gpu_index=0,
            power_limit_pct=100.0,
            core_offset_mhz=0.0,
            memory_offset_mhz=0.0,
            fan_mode="manual",
            fan_percent=50.0,
        )
    )
    clock = clock or _Clock()
    services = build_services(fake, PROFILES, clock=clock)
    plugin = GAssistPlugin(services, clock=clock)
    return fake, plugin, clock


def execute(plugin: GAssistPlugin, function: str, params=None, request_id: int = 1):
    payload = {"function": function}
    if params is not None:
        payload["params"] = params
    return plugin.process("execute", payload, request_id)


def first_complete(messages) -> dict:
    assert len(messages) == 1
    message = messages[0]
    assert message["method"] == "complete"
    return message["params"]


def first_error(messages) -> dict:
    assert len(messages) == 1
    assert "error" in messages[0]
    return messages[0]


class TestLifecycle:
    def test_initialize_reports_ready_and_registers_all_functions(self) -> None:
        fake, plugin, _ = make_plugin()
        messages = plugin.process("initialize", {}, 1)
        result = messages[0]["result"]
        assert result["status"] == "ready"
        functions = result["functions"]
        for name in (
            "get_gpu_status", "get_gpu_limits", "get_tuning_state", "get_profiles",
            "get_tuning_ownership", "show_configuration", "load_profile",
            "reset_tuning", "set_power_limit", "set_core_offset", "set_memory_offset",
            "set_fan_percent", "set_fan_curve", "optimize_quiet", "optimize_thermal",
            "diagnose_performance",
        ):
            assert name in functions
        assert plugin.initialized is True

    def test_initialize_degrades_when_afterburner_is_down(self) -> None:
        fake, plugin, _ = make_plugin(status=InterfaceStatus.NOT_RUNNING)
        result = plugin.process("initialize", {}, 1)[0]["result"]
        assert result["status"] == "degraded"
        assert "not available" in result["message"]

    def test_ping_returns_pong(self) -> None:
        fake, plugin, _ = make_plugin()
        messages = plugin.process("ping", {}, 1)
        assert messages[0]["result"] == {"result": "pong"}

    def test_shutdown_sets_the_stop_flag(self) -> None:
        fake, plugin, _ = make_plugin()
        messages = plugin.process("shutdown", {}, 1)
        assert messages[0]["result"]["status"] == "shutting_down"
        assert plugin.shutdown_requested is True


class TestDispatch:
    def test_unknown_function_is_error_and_state_is_retained(self) -> None:
        fake, plugin, _ = make_plugin()
        err = first_error(execute(plugin, "no_such_function"))
        assert err["error"]["code"] == -32601
        # State retained: the next request still works.
        complete = first_complete(execute(plugin, "get_gpu_status", {}, 2))
        assert complete["success"] is True

    def test_missing_function_name_is_invalid_params(self) -> None:
        fake, plugin, _ = make_plugin()
        err = first_error(plugin.process("execute", {}, 1))
        assert err["error"]["code"] == -32602

    def test_unknown_protocol_method_is_error_and_loop_survives(self) -> None:
        fake, plugin, _ = make_plugin()
        err = first_error(plugin.process("something_else", {}, 1))
        assert err["error"]["code"] == -32601
        assert first_complete(execute(plugin, "ping" if False else "get_gpu_status"))["success"]

    def test_get_gpu_status_read_requires_no_confirmation(self) -> None:
        fake, plugin, _ = make_plugin()
        complete = first_complete(execute(plugin, "get_gpu_status"))
        assert complete["success"] is True
        assert "60.0" in complete["message"]
        assert "needs_confirmation" not in complete["data"]
        # Data is JSON-serializable (enums/datetimes flattened).
        json.dumps(complete)

    def test_domain_failure_is_a_typed_complete_error(self) -> None:
        fake = FakeAfterburner(status=InterfaceStatus.OK)
        fake.set_capabilities(default_capabilities())
        fake.telemetry_fail_all = True
        services = build_services(fake, PROFILES)
        plugin = GAssistPlugin(services)
        complete = first_complete(execute(plugin, "get_gpu_status"))
        assert complete["success"] is False
        assert complete["data"]["error_code"] == "communication_failure"
        assert complete["message"]

    def test_get_tuning_ownership_dispatches(self) -> None:
        fake, plugin, _ = make_plugin()
        complete = first_complete(execute(plugin, "get_tuning_ownership"))
        assert complete["success"] is True
        report = complete["data"]
        assert report["nvidia_app_auto_tuning"] == "unknown_not_observable"
        assert report["gassist_native_tuning"] == "unknown_not_observable"
        assert report["other_oc_utilities"] == "unknown_not_observable"

    def test_show_configuration_lists_detection_and_controls(self) -> None:
        fake, plugin, _ = make_plugin()
        complete = first_complete(execute(plugin, "show_configuration"))
        assert complete["success"] is True
        assert complete["data"]["interface_status"] == "ok"


class TestLoadProfileAndReset:
    def test_load_profile_accepts_name_or_number_without_confirmation(self) -> None:
        fake, plugin, clock = make_plugin()

        from profiles_util import apply_recorder

        apply_recorder(fake)
        complete = first_complete(
            execute(plugin, "load_profile", {"profile_id": "Profile 1"})
        )
        assert complete["success"] is True
        assert "Profile 1 applied" in complete["message"]
        assert (0, ControlFeature.POWER_LIMIT, 100.0) in {
            (g, f, v) for (g, f, v) in fake.applied
        }

    def test_load_profile_unknown_id_is_typed_error(self) -> None:
        fake, plugin, clock = make_plugin()
        complete = first_complete(execute(plugin, "load_profile", {"profile_id": 9}))
        assert complete["success"] is False
        assert complete["data"]["error_code"] == "invalid_tuning_value"

    def test_reset_tuning_is_low_risk(self) -> None:
        fake, plugin, clock = make_plugin()
        from profiles_util import apply_recorder, slot_tuning

        apply_recorder(fake)
        fake.set_tuning(slot_tuning())  # slot 1 active
        complete = first_complete(execute(plugin, "reset_tuning"))
        assert complete["success"] is True
        assert "restored" in complete["message"]


class TestConfirmationFlow:
    def test_risky_first_run_prompts_with_token_and_never_writes(self) -> None:
        fake, plugin, clock = make_plugin()
        complete = first_complete(execute(plugin, "set_power_limit", {"percent": 110}))
        assert complete["success"] is True
        data = complete["data"]
        assert data["needs_confirmation"] is True
        assert data["confirm_token"]
        assert data["keep_session"] is True
        assert "Afterburner" in data["message"]  # ownership clause present
        assert fake.applied == []  # no write before confirmation

    def test_confirm_token_applies_and_is_single_use(self) -> None:
        fake, plugin, clock = make_plugin()
        prompt = first_complete(execute(plugin, "set_power_limit", {"percent": 110}))
        token = prompt["data"]["confirm_token"]

        complete = first_complete(
            execute(
                plugin,
                "set_power_limit",
                {"percent": 110, "confirm_token": token},
                2,
            )
        )
        assert complete["success"] is True
        assert fake.applied == [(0, ControlFeature.POWER_LIMIT, 110.0)]

        # Replay is rejected and never writes again.
        replay = first_complete(
            execute(
                plugin,
                "set_power_limit",
                {"percent": 110, "confirm_token": token},
                3,
            )
        )
        assert replay["success"] is False
        assert replay["data"]["error_code"] == "confirmation_required"
        assert fake.applied == [(0, ControlFeature.POWER_LIMIT, 110.0)]

    def test_wrong_function_token_is_rejected(self) -> None:
        fake, plugin, clock = make_plugin()
        prompt = first_complete(execute(plugin, "set_power_limit", {"percent": 110}))
        token = prompt["data"]["confirm_token"]
        complete = first_complete(
            execute(plugin, "set_fan_percent", {"percent": 40, "confirm_token": token})
        )
        assert complete["success"] is False
        assert complete["data"]["error_code"] == "confirmation_required"
        assert fake.applied == []

    def test_expired_token_is_rejected(self) -> None:
        clock = _Clock()
        fake, plugin, _ = make_plugin(clock=clock)
        prompt = first_complete(execute(plugin, "set_power_limit", {"percent": 110}))
        token = prompt["data"]["confirm_token"]
        clock.advance(301.0)  # beyond CONFIRM_TOKEN_TTL_SECONDS (300 s)
        complete = first_complete(
            execute(plugin, "set_power_limit", {"percent": 110, "confirm_token": token})
        )
        assert complete["success"] is False
        assert complete["data"]["error_code"] == "confirmation_required"
        assert fake.applied == []

    def test_noop_when_applied_state_already_equals_request(self) -> None:
        # Applied power limit already 118 => requesting 118 is a success with no prompt
        # and no write (Requirement 19.4, decision-time check).
        fake, plugin, clock = make_plugin(
            tuning=TuningState(
                gpu_index=0,
                power_limit_pct=118.0,
                core_offset_mhz=0.0,
                memory_offset_mhz=0.0,
                fan_mode="manual",
                fan_percent=50.0,
            )
        )
        complete = first_complete(execute(plugin, "set_power_limit", {"percent": 118}))
        assert complete["success"] is True
        assert complete["data"]["applied"] is False
        assert "already applied" in complete["message"]
        assert "needs_confirmation" not in complete["data"]
        assert fake.applied == []

    def test_clamped_prompt_names_the_value_that_would_apply(self) -> None:
        fake, plugin, clock = make_plugin()
        complete = first_complete(execute(plugin, "set_power_limit", {"percent": 500}))
        data = complete["data"]
        assert data["needs_confirmation"] is True
        # Prompt previews the clamped value 118 even though 500 was requested.
        assert "118" in data["message"]
        assert fake.applied == []

    def test_input_affirmative_proceeds_with_the_change(self) -> None:
        fake, plugin, clock = make_plugin()
        assert first_complete(execute(plugin, "optimize_quiet"))["data"]["needs_confirmation"]
        complete = first_complete(
            plugin.process(
                "input",
                {"function": "optimize_quiet", "content": "confirm"},
                2,
            )
        )
        assert complete["success"] is True
        assert "Fan lowered" in complete["message"]
        assert (0, ControlFeature.FAN_PERCENT, 40.0) in {
            (g, f, v) for (g, f, v) in fake.applied
        }

    def test_input_negative_cancels_and_never_writes(self) -> None:
        fake, plugin, clock = make_plugin()
        execute(plugin, "set_power_limit", {"percent": 110})
        complete = first_complete(
            plugin.process("input", {"function": "set_power_limit", "content": "cancel"}, 2)
        )
        assert complete["success"] is True
        assert complete["data"]["cancelled"] is True
        assert fake.applied == []

    def test_input_after_passthrough_timeout_reports_timeout(self) -> None:
        clock = _Clock()
        fake, plugin, _ = make_plugin(clock=clock)
        execute(plugin, "set_power_limit", {"percent": 110})
        clock.advance(61.0)  # beyond the 60 s passthrough window
        complete = first_complete(
            plugin.process("input", {"function": "set_power_limit", "content": "confirm"}, 2)
        )
        assert complete["success"] is True
        assert complete["data"]["timed_out"] is True
        assert fake.applied == []

    def test_input_without_pending_change_is_a_noop(self) -> None:
        fake, plugin, clock = make_plugin()
        complete = first_complete(
            plugin.process("input", {"function": "set_power_limit", "content": "confirm"}, 1)
        )
        assert complete["success"] is True
        assert "No 'set_power_limit'" in complete["message"]
        assert fake.applied == []


class TestFullLoop:
    def test_framed_loop_end_to_end(self) -> None:
        fake, plugin, clock = make_plugin()

        def frame(payload: object) -> bytes:
            raw = json.dumps(payload).encode("utf-8")
            return len(raw).to_bytes(4, "big") + raw

        inbound = (
            frame({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            + frame({"jsonrpc": "2.0", "id": 2, "method": "execute",
                     "params": {"function": "get_gpu_status", "params": {}}})
            + frame({"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {}})
            + frame({"jsonrpc": "2.0", "id": 4, "method": "shutdown", "params": {}})
        )
        reader = io.BytesIO(inbound)
        writer = io.BytesIO()
        assert run_plugin_loop(plugin, reader=reader, writer=writer) == 0

        data = writer.getvalue()
        messages = []
        offset = 0
        while offset + 4 <= len(data):
            length = int.from_bytes(data[offset:offset + 4], "big")
            offset += 4
            messages.append(json.loads(data[offset:offset + length].decode("utf-8")))
            offset += length
        assert messages[0]["id"] == 1 and "result" in messages[0]
        assert messages[1]["method"] == "complete"  # execute -> complete notification
        assert messages[2]["id"] == 3 and messages[2]["result"]["result"] == "pong"
        assert messages[3]["id"] == 4
        assert messages[3]["result"]["status"] == "shutting_down"


# ---------------------------------------------------------------------------
# Property 7 (task 15.4): graceful degradation — never an unhandled exception.
# ---------------------------------------------------------------------------

READ_FUNCTIONS = (
    "get_gpu_status",
    "get_gpu_limits",
    "get_tuning_state",
    "get_profiles",
    "get_tuning_ownership",
    "show_configuration",
    "diagnose_performance",
)
CONTROL_FUNCTIONS = ("set_power_limit", "load_profile", "reset_tuning")


class TestProperty7GracefulDegradation:
    @given(
        status=st.sampled_from(list(InterfaceStatus)),
        function=st.sampled_from(READ_FUNCTIONS + CONTROL_FUNCTIONS),
    )
    def test_every_call_returns_typed_outcome_never_raises(self, status, function) -> None:
        fake, plugin, clock = make_plugin(status=status)
        try:
            messages = execute(plugin, function, {"percent": 110})
        except Exception as exc:  # pragma: no cover - Property 7 forbids this
            raise AssertionError(f"unhandled exception for {function}: {exc}")
        assert len(messages) == 1
        message = messages[0]
        if "error" in message:  # JSON-RPC error response (unknown function path)
            assert message["error"]["code"] in (-32601, -32602)
        else:
            assert message["method"] == "complete"
            params = message["params"]
            assert "message" in params and isinstance(params["message"], str)
            assert "success" in params

    @given(status=st.sampled_from(list(InterfaceStatus)))
    def test_domain_calls_never_crash_the_process(self, status) -> None:
        fake, plugin, clock = make_plugin(status=status)
        for function in READ_FUNCTIONS + CONTROL_FUNCTIONS:
            try:
                execute(plugin, function)
            except Exception as exc:  # pragma: no cover
                raise AssertionError(f"unhandled exception in {function}: {exc}")
        # A follow-up initialize still works after all the failures.
        result = plugin.process("initialize", {}, 99)[0]
        assert result.get("result", {}).get("status") in ("ready", "degraded")
