"""Risk classification & confirmation-token lifecycle (design Confirmation Flow).

Risky (HIGH) functions require explicit confirmation: set_*, optimize_*. Everything else —
reads, reset_tuning, load_profile — is LOW and bypasses confirmation.

Tokens are single-use, expire after CONFIRM_TOKEN_TTL_SECONDS (300 s), and `validate_token`
rejects missing, expired, or reused tokens (raising CONFIRMATION_REQUIRED). Pending
confirmation is a success `complete` with needs_confirmation — never an error — so token
validity is checked on the *follow-up* call that carries the token.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from ..models import ErrorCode, PluginError, RiskLevel

CONFIRM_TOKEN_TTL_SECONDS = 300.0

HIGH_RISK_FUNCTIONS = frozenset(
    {
        "set_power_limit",
        "set_core_offset",
        "set_memory_offset",
        "set_fan_percent",
        "set_fan_curve",
        "set_fan_auto",
        "optimize_quiet",
        "optimize_thermal",
    }
)

LOW_RISK_FUNCTIONS = frozenset(
    {
        # Reads
        "get_gpu_status",
        "list_gpus",
        "get_gpu_limits",
        "get_tuning_state",
        "get_profiles",
        "show_configuration",
        "get_tuning_ownership",
        "diagnose_performance",
        # Low-friction applies
        "load_profile",
        "reset_tuning",
    }
)


@dataclass(frozen=True)
class ConfirmToken:
    token: str
    function: str
    safe_value: float
    expires_at: float  # monotonic seconds


class SafetyPolicy:
    """Risk classification and single-use confirmation tokens."""

    def __init__(self, clock: Optional[Callable[[], float]] = None) -> None:
        self._clock = clock or time.monotonic
        self._tokens: Dict[str, ConfirmToken] = {}

    # ------------------------------------------------------------------ risk
    def risk_for(self, function: str) -> RiskLevel:
        if function in HIGH_RISK_FUNCTIONS:
            return RiskLevel.HIGH
        if function in LOW_RISK_FUNCTIONS:
            return RiskLevel.LOW
        # Unknown functions fail closed: never apply without explicit confirmation.
        return RiskLevel.HIGH

    def requires_confirmation(self, function: str) -> bool:
        return self.risk_for(function) is RiskLevel.HIGH

    # ----------------------------------------------------------------- tokens
    def issue_confirm_token(self, function: str, safe_value: float) -> ConfirmToken:
        token = ConfirmToken(
            token=secrets.token_hex(16),
            function=function,
            safe_value=safe_value,
            expires_at=self._clock() + CONFIRM_TOKEN_TTL_SECONDS,
        )
        self._tokens[token.token] = token
        return token

    def validate_token(self, token: str) -> ConfirmToken:
        """Validate + consume a token. Raises CONFIRMATION_REQUIRED on any failure."""
        entry = self._tokens.pop(token, None)
        if entry is None:
            raise PluginError(
                ErrorCode.CONFIRMATION_REQUIRED,
                "Please confirm the change first.",
                detail="missing or already-used token",
            )
        if self._clock() > entry.expires_at:
            raise PluginError(
                ErrorCode.CONFIRMATION_REQUIRED,
                "That confirmation expired. Please confirm the change again.",
                detail="expired token",
            )
        return entry
