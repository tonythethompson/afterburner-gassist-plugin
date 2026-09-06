"""Typed error -> user-facing message -> Protocol V2 code mapping (design Error Taxonomy).

Every `ErrorCode` maps to a user-facing message (condition + affected component/value + next
action) and a Protocol V2 JSON-RPC code. Clamping is a SUCCESS `ControlResult` (never an error);
`DISCONNECTED` states nothing was changed. Unmatched conditions map to COMM_FAILURE.
"""
from __future__ import annotations

from typing import Dict

from .models import ControlResult, ErrorCode, PluginError

# Protocol V2 JSON-RPC codes (see design error taxonomy table).
_JSON_RPC_INVALID_PARAMS = -32602
_JSON_RPC_INTERNAL_ERROR = -1

_MESSAGES: Dict[ErrorCode, str] = {
    ErrorCode.NOT_INSTALLED: (
        "MSI Afterburner doesn't appear to be installed. Install it from msi.com or guru3d.com "
        "and try again."
    ),
    ErrorCode.NOT_RUNNING: "MSI Afterburner isn't running. Start it and try again.",
    ErrorCode.UNSUPPORTED_VERSION: (
        "Your Afterburner version doesn't expose the needed interface. Please update "
        "Afterburner and try again."
    ),
    ErrorCode.UNSUPPORTED_GPU: "This GPU isn't supported for that operation.",
    ErrorCode.INTERFACE_UNAVAILABLE: (
        "Afterburner is installed but its control interface is unavailable. Start MSI "
        "Afterburner and try again."
    ),
    ErrorCode.ACCESS_DENIED: (
        "Controlling Afterburner needs elevated permissions. Run Afterburner (and G-Assist) "
        "with the required privileges."
    ),
    ErrorCode.INVALID_VALUE: "That value isn't valid for this control.",
    ErrorCode.COMM_FAILURE: "I couldn't read from Afterburner just now. Try again in a moment.",
    ErrorCode.STALE_TELEMETRY: "The readings are stale; Afterburner may be restarting.",
    ErrorCode.UNSUPPORTED_FEATURE: "That control isn't available on this hardware/version.",
    ErrorCode.DISCONNECTED: "Afterburner disconnected mid-operation. Nothing was changed.",
    ErrorCode.CONFIRMATION_REQUIRED: "Please confirm the change first.",
}

_V2_CODES: Dict[ErrorCode, int] = {
    ErrorCode.INVALID_VALUE: _JSON_RPC_INVALID_PARAMS,
    # Everything else is a server-side / domain error surfaced as `error` with code -1.
}


def user_message_for(code: ErrorCode) -> str:
    """User-facing NL message for an ErrorCode (COMM_FAILURE fallback for unknowns)."""
    return _MESSAGES.get(code, _MESSAGES[ErrorCode.COMM_FAILURE])


def protocol_code_for(code: ErrorCode) -> int:
    """Protocol V2 JSON-RPC code for an ErrorCode (COMM_FAILURE fallback for unknowns)."""
    if code in _V2_CODES:
        return _V2_CODES[code]
    return _JSON_RPC_INTERNAL_ERROR


def to_user_error(exc: Exception) -> "tuple[ErrorCode, str]":
    """Map any exception to (ErrorCode, user message); unmatched -> COMM_FAILURE."""
    if isinstance(exc, PluginError):
        return exc.code, exc.user_message
    return ErrorCode.COMM_FAILURE, user_message_for(ErrorCode.COMM_FAILURE)


def clamp_result_message(
    result: ControlResult,
) -> str:
    """NL message for a clamped success: states requested AND applied values (never an error)."""
    requested = result.requested_value
    applied = result.applied_value
    feature = result.feature.value
    if result.message:
        return result.message
    if result.clamped and requested is not None and applied is not None:
        return (
            f"{feature} set to the supported value {applied} "
            f"(you asked for {requested}, which was out of range)."
        )
    if applied is not None:
        return f"{feature} is now {applied}."
    return f"{feature} change applied."
