"""Plan tasks 15.3 / 15.4 — GAssistPlugin lifecycle, dispatch, confirmation, Property 7.

Wire shapes follow the engine-verified Protocol V2 contract (NVIDIA PROTOCOL_V2.md +
migration guide + gassist_sdk + the official plugin_emulator): execute -> ``complete``
notification ``{request_id, success, data, keep_session}`` where ``data`` IS the NL
message string (Protocol V2 has no structured output channel — dict payloads made the
live engine report "Could not parse JSON-RPC message", 2026-09-06); failures -> ``error``
notification ``{request_id, code, message}``; ping echoes ``timestamp``; initialize
result carries ``name/version/protocol_version/commands``; input is acknowledged first
and resolved against the plugin's internal pending state; shutdown is a notification
with no response.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from afterburner.errors import protocol_code_for, user_message_for
from afterburner.integration.fake import FakeAfterburner, default_capabilities
from afterburner.models import (
    ControlFeature,
    ErrorCode,
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
    """Engine-shaped execute: function + arguments (+ optional confirm_token)."""
    payload = {"function": function}
    if params is not None:
        payload["arguments"] = params
    return plugin.process("execute", payload, request_id)


def one(messages) -> dict:
    assert len(messages) == 1
    return messages[0]


def complete_params(messages) -> dict:
    """Params of the single ``complete`` notification."""
    message = one(messages)
    assert message["method"] == "complete"
    return message["params"]


def error_notification(messages) -> dict:
    """Params of the single ``error`` notification (engine failure path)."""
    message = one(messages)
    assert message["method"] == "error"
    return message["params"]


def input_messages(messages) -> tuple[dict, dict]:
    """(ack response, complete params) for a passthrough ``input`` exchange."""
    assert len(messages) == 2
    ack, complete = messages
    assert ack["result"] == {"acknowledged": True}
    assert complete["method"] == "complete"
    return ack, complete["params"]


def msg_text(complete: dict) -> str:
    """NL user text carried by a complete notification (``data`` is the string)."""
    return complete["data"]


class TestLifecycle:
    def test_initialize_reports_ready_and_registers_all_functions(self) -> None:
        fake, plugin, _ = make_plugin()
        result = one(plugin.process("initialize", {}, 1))["result"]
        assert result["status"] == "ready"
        assert result["name"] == "afterburner"
        assert result["protocol_version"] == "2.0"
        command_names = [c["name"] for c in result["commands"]]
        for name in (
            "get_gpu_status", "get_gpu_limits", "get_tuning_state", "get_profiles",
            "get_tuning_ownership", "show_configuration", "load_profile",
            "reset_tuning", "set_power_limit", "set_core_offset", "set_memory_offset",
            "set_fan_percent", "set_fan_curve", "optimize_quiet", "optimize_thermal",
            "diagnose_performance",
        ):
            assert name in command_names
        assert plugin.initialized is True

    def test_initialize_carries_manifest_metadata_and_command_descriptions(self) -> None:
        fake, plugin, _ = make_plugin()
        fake2 = FakeAfterburner(status=InterfaceStatus.OK)
        fake2.set_capabilities(default_capabilities())
        fake2.set_telemetry(telemetry())
        fake2.set_tuning(TuningState(
            gpu_index=0, power_limit_pct=100.0, core_offset_mhz=0.0,
            memory_offset_mhz=0.0, fan_mode="manual", fan_percent=50.0,
        ))
        services = build_services(fake2, PROFILES)
        plugin = GAssistPlugin(
            services,
            name="afterburner",
            version="1.0.0",
            description="desc",
            functions_meta=[{"name": "get_gpu_status", "description": "Report status."}],
        )
        result = one(plugin.process("initialize", {}, 1))["result"]
        assert result["version"] == "1.0.0"
        assert result["description"] == "desc"
        by_name = {c["name"]: c["description"] for c in result["commands"]}
        assert by_name["get_gpu_status"] == "Report status."

    def test_initialize_degrades_when_afterburner_is_down(self) -> None:
        fake, plugin, _ = make_plugin(status=InterfaceStatus.NOT_RUNNING)
        result = one(plugin.process("initialize", {}, 1))["result"]
        assert result["status"] == "degraded"
        assert "not available" in result["message"]

    def test_ping_echoes_the_timestamp(self) -> None:
        fake, plugin, _ = make_plugin()
        messages = plugin.process("ping", {"timestamp": 1234567890}, 1)
        assert one(messages)["result"] == {"timestamp": 1234567890}

    def test_shutdown_is_a_notification_with_no_response(self) -> None:
        fake, plugin, _ = make_plugin()
        messages = plugin.process("shutdown", {}, None)
        assert messages == []
        assert plugin.shutdown_requested is True


class TestDispatch:
    def test_unknown_function_is_error_and_state_is_retained(self) -> None:
        fake, plugin, _ = make_plugin()
        err = one(execute(plugin, "no_such_function"))
        assert err["error"]["code"] == -32601
        # State retained: the next request still works.
        complete = complete_params(execute(plugin, "get_gpu_status", {}, 2))
        assert complete["success"] is True

    def test_missing_function_name_is_invalid_params(self) -> None:
        fake, plugin, _ = make_plugin()
        err = one(plugin.process("execute", {}, 1))
        assert err["error"]["code"] == -32602

    def test_unknown_protocol_method_is_error_and_loop_survives(self) -> None:
        fake, plugin, _ = make_plugin()
        err = one(plugin.process("something_else", {}, 1))
        assert err["error"]["code"] == -32601
        assert complete_params(execute(plugin, "get_gpu_status"))["success"]

    def test_get_gpu_status_read_requires_no_confirmation(self) -> None:
        fake, plugin, _ = make_plugin()
        complete = complete_params(execute(plugin, "get_gpu_status"))
        assert complete["success"] is True
        assert complete["request_id"] == 1
        assert complete["keep_session"] is False
        # Protocol V2: complete.data is the NL text itself (no structured channel).
        assert isinstance(complete["data"], str)
        assert "60.0" in complete["data"]
        # Data is JSON-serializable.
        json.dumps(complete)

    def test_domain_failure_is_an_error_notification(self) -> None:
        fake = FakeAfterburner(status=InterfaceStatus.OK)
        fake.set_capabilities(default_capabilities())
        fake.telemetry_fail_all = True
        services = build_services(fake, PROFILES)
        plugin = GAssistPlugin(services)
        params = error_notification(execute(plugin, "get_gpu_status"))
        assert params["request_id"] == 1
        assert params["code"] == protocol_code_for(ErrorCode.COMM_FAILURE)
        assert params["message"] == user_message_for(ErrorCode.COMM_FAILURE)

    def test_get_tuning_ownership_dispatches(self) -> None:
        fake, plugin, _ = make_plugin()
        complete = complete_params(execute(plugin, "get_tuning_ownership"))
        assert complete["success"] is True
        # The typed report structure is covered by the service tests; on the wire the
        # ownership summary is plain text.
        assert isinstance(complete["data"], str)
        assert "Afterburner owns the tuning state" in complete["data"]

    def test_show_configuration_lists_detection_and_controls(self) -> None:
        fake, plugin, _ = make_plugin()
        complete = complete_params(execute(plugin, "show_configuration"))
        assert complete["success"] is True
        assert isinstance(complete["data"], str)
        assert "MSI Afterburner: ok" in complete["data"]


class TestLoadProfileAndReset:
    def test_load_profile_accepts_name_or_number_without_confirmation(self) -> None:
        fake, plugin, clock = make_plugin()

        from profiles_util import apply_recorder

        apply_recorder(fake)
        complete = complete_params(
            execute(plugin, "load_profile", {"profile_id": "Profile 1"})
        )
        assert complete["success"] is True
        assert "Profile 1 applied" in msg_text(complete)
        assert (0, ControlFeature.POWER_LIMIT, 100.0) in {
            (g, f, v) for (g, f, v) in fake.applied
        }

    def test_load_profile_unknown_id_is_typed_error(self) -> None:
        fake, plugin, clock = make_plugin()
        params = error_notification(execute(plugin, "load_profile", {"profile_id": 9}))
        assert params["code"] == protocol_code_for(ErrorCode.INVALID_VALUE)
        assert "profile 9" in params["message"]
        assert fake.applied == []

    def test_reset_tuning_is_low_risk(self) -> None:
        fake, plugin, clock = make_plugin()
        from profiles_util import apply_recorder, slot_tuning

        apply_recorder(fake)
        fake.set_tuning(slot_tuning())  # slot 1 active
        complete = complete_params(execute(plugin, "reset_tuning"))
        assert complete["success"] is True
        assert "restored" in msg_text(complete)


class TestConfirmationFlow:
    def test_risky_first_run_prompts_and_never_writes(self) -> None:
        fake, plugin, clock = make_plugin()
        complete = complete_params(execute(plugin, "set_power_limit", {"percent": 110}))
        assert complete["success"] is True
        assert complete["keep_session"] is True
        # Protocol V2: the prompt IS the data string (no structured fields on the wire).
        assert isinstance(complete["data"], str)
        assert "Reply 'confirm'" in complete["data"]
        assert "can't be seen through Afterburner" in complete["data"]  # ownership clause
        assert fake.applied == []  # no write before confirmation

    def test_issued_token_applies_once_and_replay_is_rejected(self) -> None:
        fake, plugin, clock = make_plugin()
        # The token is internal (never on the wire); unit tests obtain it from policy.
        token = plugin.services.policy.issue_confirm_token(
            "set_power_limit", 110.0
        ).token

        complete = complete_params(
            execute(
                plugin,
                "set_power_limit",
                {"percent": 110, "confirm_token": token},
                2,
            )
        )
        assert complete["success"] is True
        assert fake.applied == [(0, ControlFeature.POWER_LIMIT, 110.0)]

        # Replay is rejected and never writes again (single-use, Req 9.1).
        replay = error_notification(
            execute(
                plugin,
                "set_power_limit",
                {"percent": 110, "confirm_token": token},
                3,
            )
        )
        assert replay["code"] == protocol_code_for(ErrorCode.CONFIRMATION_REQUIRED)
        assert fake.applied == [(0, ControlFeature.POWER_LIMIT, 110.0)]

    def test_wrong_function_token_is_rejected(self) -> None:
        fake, plugin, clock = make_plugin()
        token = plugin.services.policy.issue_confirm_token(
            "set_power_limit", 110.0
        ).token
        params = error_notification(
            execute(plugin, "set_fan_percent", {"percent": 40, "confirm_token": token})
        )
        assert params["code"] == protocol_code_for(ErrorCode.CONFIRMATION_REQUIRED)
        assert fake.applied == []

    def test_expired_token_is_rejected(self) -> None:
        clock = _Clock()
        fake, plugin, _ = make_plugin(clock=clock)
        token = plugin.services.policy.issue_confirm_token(
            "set_power_limit", 110.0
        ).token
        clock.advance(301.0)  # beyond CONFIRM_TOKEN_TTL_SECONDS (300 s)
        params = error_notification(
            execute(plugin, "set_power_limit", {"percent": 110, "confirm_token": token})
        )
        assert params["code"] == protocol_code_for(ErrorCode.CONFIRMATION_REQUIRED)
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
        complete = complete_params(execute(plugin, "set_power_limit", {"percent": 118}))
        assert complete["success"] is True
        assert complete["keep_session"] is False
        assert "already applied" in complete["data"]
        assert fake.applied == []

    def test_clamped_prompt_names_the_value_that_would_apply(self) -> None:
        fake, plugin, clock = make_plugin()
        complete = complete_params(execute(plugin, "set_power_limit", {"percent": 500}))
        assert complete["keep_session"] is True
        # Prompt previews the clamped value 118 even though 500 was requested.
        assert "118" in complete["data"]
        assert fake.applied == []

    def test_input_affirmative_proceeds_with_the_change(self) -> None:
        fake, plugin, clock = make_plugin()
        prompt = complete_params(execute(plugin, "optimize_quiet"))
        assert prompt["keep_session"] is True
        assert "Reply 'confirm'" in prompt["data"]
        ack, complete = input_messages(
            plugin.process("input", {"content": "confirm"}, 2)
        )
        assert complete["success"] is True
        assert "Fan lowered" in complete["data"]
        assert (0, ControlFeature.FAN_PERCENT, 40.0) in {
            (g, f, v) for (g, f, v) in fake.applied
        }
        # Single-use at the input layer too: a second confirm has nothing to apply.
        ack2, complete2 = input_messages(
            plugin.process("input", {"content": "confirm"}, 3)
        )
        assert "No change is currently awaiting confirmation" in complete2["data"]
        assert fake.applied == [(0, ControlFeature.FAN_PERCENT, 40.0)]

    def test_input_negative_cancels_and_never_writes(self) -> None:
        fake, plugin, clock = make_plugin()
        execute(plugin, "set_power_limit", {"percent": 110})
        ack, complete = input_messages(
            plugin.process("input", {"content": "cancel"}, 2)
        )
        assert complete["success"] is True
        assert "Change cancelled — nothing was applied" in complete["data"]
        assert fake.applied == []

    def test_input_after_passthrough_timeout_reports_timeout(self) -> None:
        clock = _Clock()
        fake, plugin, _ = make_plugin(clock=clock)
        execute(plugin, "set_power_limit", {"percent": 110})
        clock.advance(61.0)  # beyond the 60 s passthrough window
        ack, complete = input_messages(
            plugin.process("input", {"content": "confirm"}, 2)
        )
        assert complete["success"] is True
        assert "timed out — no change was made" in complete["data"]
        assert fake.applied == []

    def test_input_without_pending_change_is_a_noop(self) -> None:
        fake, plugin, clock = make_plugin()
        ack, complete = input_messages(
            plugin.process("input", {"content": "confirm"}, 1)
        )
        assert complete["success"] is True
        assert "No change is currently awaiting confirmation" in complete["data"]
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
                     "params": {"function": "get_gpu_status", "arguments": {}}})
            + frame({"jsonrpc": "2.0", "id": 3, "method": "ping",
                     "params": {"timestamp": 1234}})
            + frame({"jsonrpc": "2.0", "method": "shutdown", "params": {}})
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
        # initialize -> response; execute -> complete; ping -> timestamp echo; and the
        # shutdown notification is answered with NO message.
        assert messages[0]["id"] == 1 and "result" in messages[0]
        assert messages[1]["method"] == "complete"
        assert messages[1]["params"]["request_id"] == 2
        assert isinstance(messages[1]["params"]["data"], str)
        assert messages[1]["params"]["data"]
        assert messages[2]["id"] == 3 and messages[2]["result"] == {"timestamp": 1234}
        assert len(messages) == 3


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
        if "error" in message:  # JSON-RPC error response (unknown method/function path)
            assert message["error"]["code"] in (-32601, -32602)
        elif message.get("method") == "error":  # engine failure notification
            assert isinstance(message["params"]["message"], str)
            assert isinstance(message["params"]["code"], int)
        else:
            assert message["method"] == "complete"
            params = message["params"]
            assert params["success"] is True
            assert isinstance(params["data"], str)  # Protocol V2: data is the text
            assert params["keep_session"] in (True, False)

    @given(status=st.sampled_from(list(InterfaceStatus)))
    def test_domain_calls_never_crash_the_process(self, status) -> None:
        fake, plugin, clock = make_plugin(status=status)
        for function in READ_FUNCTIONS + CONTROL_FUNCTIONS:
            try:
                execute(plugin, function)
            except Exception as exc:  # pragma: no cover
                raise AssertionError(f"unhandled exception in {function}: {exc}")
        # A follow-up initialize still works after all the failures.
        result = one(plugin.process("initialize", {}, 99))
        assert result.get("result", {}).get("status") in ("ready", "degraded")
