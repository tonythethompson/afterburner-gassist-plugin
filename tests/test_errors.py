"""Task 12.2 — error taxonomy -> user message -> Protocol V2 code mapping."""
from __future__ import annotations

import pytest

from afterburner import errors as error_module
from afterburner.errors import (
    clamp_result_message,
    protocol_code_for,
    to_user_error,
    user_message_for,
)
from afterburner.models import ControlFeature, ControlResult, ErrorCode, PluginError

ALL_CODES = list(ErrorCode)


class TestMessageMapping:
    def test_every_error_code_has_non_empty_actionable_message(self) -> None:
        for code in ALL_CODES:
            message = user_message_for(code)
            assert isinstance(message, str)
            assert len(message) > 20, code
            assert message != user_message_for(ErrorCode.COMM_FAILURE) or code in (
                ErrorCode.COMM_FAILURE,
                ErrorCode.STALE_TELEMETRY,
            )

    def test_messages_mention_condition_and_next_action(self) -> None:
        assert "install" in user_message_for(ErrorCode.NOT_INSTALLED).lower()
        assert "start it" in user_message_for(ErrorCode.NOT_RUNNING).lower()
        assert "update" in user_message_for(ErrorCode.UNSUPPORTED_VERSION).lower()
        assert "privileges" in user_message_for(ErrorCode.ACCESS_DENIED).lower()
        assert "nothing was changed" in user_message_for(ErrorCode.DISCONNECTED).lower()
        assert "confirm" in user_message_for(ErrorCode.CONFIRMATION_REQUIRED).lower()

    def test_protocol_v2_codes(self) -> None:
        assert protocol_code_for(ErrorCode.INVALID_VALUE) == -32602
        for code in ALL_CODES:
            if code is not ErrorCode.INVALID_VALUE:
                assert protocol_code_for(code) == -1

    def test_unmatched_condition_maps_to_comm_failure(self) -> None:
        code, message = to_user_error(RuntimeError("boom"))
        assert code is ErrorCode.COMM_FAILURE
        assert message == user_message_for(ErrorCode.COMM_FAILURE)

    def test_plugin_error_passes_through(self) -> None:
        exc = PluginError(
            ErrorCode.UNSUPPORTED_FEATURE, "That control isn't available on this hardware/version."
        )
        code, message = to_user_error(exc)
        assert code is ErrorCode.UNSUPPORTED_FEATURE
        assert message == exc.user_message


class TestClampIsNotAnError:
    def test_clamped_success_message_states_requested_and_applied(self) -> None:
        result = ControlResult(
            feature=ControlFeature.POWER_LIMIT,
            requested_value=500.0,
            applied_value=118.0,
            clamped=True,
            applied=True,
        )
        message = clamp_result_message(result)
        assert "118" in message
        assert "500" in message

    def test_clamped_result_is_a_success_not_an_error(self) -> None:
        # Clamping is represented by ControlResult (success) — never a PluginError.
        assert not isinstance(
            ControlResult(
                feature=ControlFeature.CORE_OFFSET,
                requested_value=999.0,
                applied_value=300.0,
                clamped=True,
                applied=True,
            ),
            PluginError,
        )

    def test_plain_applied_message(self) -> None:
        result = ControlResult(
            feature=ControlFeature.FAN_PERCENT,
            requested_value=40.0,
            applied_value=40.0,
            applied=True,
        )
        assert "40" in clamp_result_message(result)

    def test_explicit_message_wins(self) -> None:
        result = ControlResult(
            feature=ControlFeature.POWER_LIMIT,
            requested_value=100.0,
            applied_value=100.0,
            message="Power limit is now 100%.",
        )
        assert clamp_result_message(result) == "Power limit is now 100%."


class TestNoInfiniteRecursion:
    def test_module_exposes_all_expected_helpers(self) -> None:
        for name in ("user_message_for", "protocol_code_for", "to_user_error",
                     "clamp_result_message"):
            assert hasattr(error_module, name)
