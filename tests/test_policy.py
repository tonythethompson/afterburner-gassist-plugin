"""Task 6.2/6.3 — confirmation flow; Property 4 (high-risk writes need valid tokens)."""
from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

import pytest

from afterburner.models import ErrorCode, PluginError, RiskLevel
from afterburner.safety.policy import (
    CONFIRM_TOKEN_TTL_SECONDS,
    SafetyPolicy,
    HIGH_RISK_FUNCTIONS,
    LOW_RISK_FUNCTIONS,
)


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def make_policy() -> tuple[SafetyPolicy, _Clock]:
    clock = _Clock()
    return SafetyPolicy(clock=clock), clock


class TestRiskClassification:
    def test_high_risk_functions(self) -> None:
        policy, _ = make_policy()
        for fn in ("set_power_limit", "set_core_offset", "set_memory_offset",
                   "set_fan_percent", "set_fan_curve", "set_fan_auto",
                   "optimize_quiet", "optimize_thermal"):
            assert policy.risk_for(fn) is RiskLevel.HIGH
            assert policy.requires_confirmation(fn)

    def test_low_risk_functions(self) -> None:
        policy, _ = make_policy()
        for fn in ("get_gpu_status", "list_gpus", "get_tuning_state", "get_profiles",
                   "load_profile", "reset_tuning", "diagnose_performance"):
            assert policy.risk_for(fn) is RiskLevel.LOW
            assert not policy.requires_confirmation(fn)

    def test_unknown_function_fails_closed(self) -> None:
        policy, _ = make_policy()
        assert policy.risk_for("some_unknown_fn") is RiskLevel.HIGH

    def test_risk_sets_are_complete_and_disjoint(self) -> None:
        assert not (HIGH_RISK_FUNCTIONS & LOW_RISK_FUNCTIONS)
        assert len(HIGH_RISK_FUNCTIONS) == 8
        assert len(LOW_RISK_FUNCTIONS) >= 7


class TestTokenLifecycle:
    def test_issue_validate_consume(self) -> None:
        policy, clock = make_policy()
        token = policy.issue_confirm_token("set_power_limit", 100.0)
        assert token.safe_value == 100.0
        assert token.expires_at == clock.t + CONFIRM_TOKEN_TTL_SECONDS
        validated = policy.validate_token(token.token)
        assert validated.function == "set_power_limit"
        assert validated.safe_value == 100.0

    def test_reused_token_rejected(self) -> None:
        policy, _ = make_policy()
        token = policy.issue_confirm_token("set_fan_percent", 40.0)
        policy.validate_token(token.token)
        with pytest.raises(PluginError) as excinfo:
            policy.validate_token(token.token)
        assert excinfo.value.code is ErrorCode.CONFIRMATION_REQUIRED

    def test_expired_token_rejected(self) -> None:
        policy, clock = make_policy()
        token = policy.issue_confirm_token("set_core_offset", 100.0)
        clock.advance(CONFIRM_TOKEN_TTL_SECONDS + 1)
        with pytest.raises(PluginError) as excinfo:
            policy.validate_token(token.token)
        assert excinfo.value.code is ErrorCode.CONFIRMATION_REQUIRED

    def test_missing_token_rejected(self) -> None:
        policy, _ = make_policy()
        with pytest.raises(PluginError) as excinfo:
            policy.validate_token("nope")
        assert excinfo.value.code is ErrorCode.CONFIRMATION_REQUIRED

    def test_token_valid_at_last_second(self) -> None:
        policy, clock = make_policy()
        token = policy.issue_confirm_token("set_power_limit", 100.0)
        clock.advance(CONFIRM_TOKEN_TTL_SECONDS)  # exactly at expiry -> still valid
        validated = policy.validate_token(token.token)
        assert validated.safe_value == 100.0


# ---------------------------------------------------------------------------
# Property 4: a high-risk write happens only with a currently-valid single-use token.
# ---------------------------------------------------------------------------

VALID_FUNCTIONS = sorted(HIGH_RISK_FUNCTIONS | LOW_RISK_FUNCTIONS)


@given(
    function=st.sampled_from(sorted(HIGH_RISK_FUNCTIONS)),
    when_valid=st.one_of(
        st.just("before"), st.just("exactly_at"), st.just("after")
    ),
    replay=st.booleans(),
)
def test_high_risk_token_lifecycle(function, when_valid, replay) -> None:
    policy, clock = make_policy()
    token = policy.issue_confirm_token(function, 75.0)

    if when_valid == "after":
        clock.advance(CONFIRM_TOKEN_TTL_SECONDS + 1)
        with pytest.raises(PluginError) as excinfo:
            policy.validate_token(token.token)
        assert excinfo.value.code is ErrorCode.CONFIRMATION_REQUIRED
        return
    if when_valid == "exactly_at":
        clock.advance(CONFIRM_TOKEN_TTL_SECONDS)

    # First use is valid
    validated = policy.validate_token(token.token)
    assert validated.function == function
    assert validated.safe_value == 75.0

    if replay:
        # Replay (even immediately) is rejected: single-use.
        with pytest.raises(PluginError) as excinfo:
            policy.validate_token(token.token)
        assert excinfo.value.code is ErrorCode.CONFIRMATION_REQUIRED
