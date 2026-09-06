"""ProfileManager — profile list / active / load / reset (design component table).

Requirement 10 semantics over two injected pieces:

- ``client: AfterburnerClient`` — the mock boundary for control: applied tuning-state
  read-back (active-profile matching, verification) and the named, validated control
  operations every profile apply is routed through.
- ``reader: ProfileFileReader`` — the real read-only Profiles-directory scan
  (``afterburner/integration/profiles.py``). The plugin itself never touches the Profiles
  directory; file access happens only through this integration-layer reader.

Invariants (Requirements 10.1-10.9):

- Profiles are *read* from the Profiles directory only via the reader; nothing under that
  directory is ever written, created, or deleted (Property 9).
- ``get_profiles`` lists present slots; the *active* profile is marked only by comparing the
  applied tuning state (read back through the control interface) with each slot's stored
  values within ``TUNING_MATCH_TOLERANCE`` — at most one, none when the control interface is
  unavailable or the match is ambiguous, never guessed.
- ``load_profile`` / ``reset_profile`` treat stored values as untrusted: every value is
  validated and clamped like an LLM-supplied value and applied only through named,
  capability-gated control operations; settings without a named control are ignored with a
  notice; unknown ids are rejected with the active profile unchanged; a control interface
  that is unavailable yields a typed ``INTERFACE_UNAVAILABLE``; success is reported only when
  the applied state is verified by read-back.
- Every operation returns a typed result/error — never an unhandled exception (10.9).

The Profiles directory path is fixed at construction (detected installation) and is never
derived from function arguments or file contents.
"""

from __future__ import annotations

import math
from typing import List, Mapping, Optional, Sequence, Tuple

from ..integration.client import AfterburnerClient
from ..integration.profiles import (
    ProfileFileReader,
    ProfileSlotInfo,
    ProfileSourceState,
    ProfilesSnapshot,
)
from ..models import (
    ControlFeature,
    ControlResult,
    ErrorCode,
    GpuCapabilities,
    InterfaceStatus,
    PluginError,
    Profile,
    TuningState,
)
from ..safety.capabilities import HardwareCapabilityResolver
from ..safety.validator import TuningValidator

# Shared equality tolerance for tuning read-back matching. One constant serves the
# write-time no-op equality (design MACM write sequence / Requirement 19.4) and the
# active-profile match (design profile storage layout) so they cannot disagree.
TUNING_MATCH_TOLERANCE = 1.0

# Stored key -> named control + unit scale (stored value x scale = control value).
# Order is the apply order. CoreClkBoost / MemClkBoost are stored x1000 (e.g. 95000 = 95 MHz).
_MAPPED_KEYS: Tuple[Tuple[str, ControlFeature, float], ...] = (
    ("PowerLimit", ControlFeature.POWER_LIMIT, 1.0),
    ("CoreClkBoost", ControlFeature.CORE_OFFSET, 0.001),
    ("MemClkBoost", ControlFeature.MEMORY_OFFSET, 0.001),
)
# FanSpeed maps to FAN_PERCENT only when the stored fan mode is a fixed speed (FanMode=1).
_FAN_MODE_KEY = "FanMode"
_FAN_SPEED_KEY = "FanSpeed"

# Populated keys with no corresponding named control -> ignored with a notice on load.
_IGNORED_WITH_NOTICE = (
    "ThermalLimit",
    "CoreVoltageBoost",
    "VFCurve",
    "FanMode2",
    "FanSpeed2",
)

# Control feature -> TuningState attribute used for read-back verification / matching.
_FEATURE_STATE_ATTR = {
    ControlFeature.POWER_LIMIT: "power_limit_pct",
    ControlFeature.CORE_OFFSET: "core_offset_mhz",
    ControlFeature.MEMORY_OFFSET: "memory_offset_mhz",
    ControlFeature.FAN_PERCENT: "fan_percent",
}


def _no_profile_error(profile_id: object) -> PluginError:
    return PluginError(
        ErrorCode.INVALID_VALUE,
        f"There's no Afterburner profile {profile_id}.",
        detail=f"load_profile: unknown id {profile_id!r}",
    )


def _control_unavailable() -> PluginError:
    return PluginError(
        ErrorCode.INTERFACE_UNAVAILABLE,
        "Afterburner is installed but its control interface is unavailable. "
        "Start MSI Afterburner and try again.",
        detail="profile apply requires a functional control interface (Requirement 10.7)",
    )


class ProfileManager:
    """Domain service for profile list/active/load/reset over client + read-only reader."""

    def __init__(
        self,
        client: AfterburnerClient,
        reader: ProfileFileReader,
        *,
        gpu_index: int = 0,
        tolerance: float = TUNING_MATCH_TOLERANCE,
    ) -> None:
        self._client = client
        self._reader = reader
        self.gpu_index = gpu_index
        self.tolerance = tolerance
        if client.interface is not None:
            self._resolver = HardwareCapabilityResolver(client.interface)
            self._validator = TuningValidator(self._resolver)
        else:
            self._resolver = None
            self._validator = None

    # ------------------------------------------------------------------ list
    def get_profiles(self, gpu_index: Optional[int] = None) -> Sequence[Profile]:
        """List present hardware profile slots; mark the single active one if verifiable.

        Returns an empty list when the directory holds no profiles (Req 10.3). Raises typed
        ``INTERFACE_UNAVAILABLE`` when the directory is missing or the per-GPU file cannot be
        attributed/parsed (never a fabricated list — Req 10.8).
        """
        return self._typed(
            lambda: self._get_profiles(self._gpu(gpu_index))
        )

    # ------------------------------------------------------------------ load
    def load_profile(
        self, profile_id: int, gpu_index: Optional[int] = None
    ) -> ControlResult:
        """Apply one stored profile slot via named, validated, capability-gated controls.

        Unknown ids raise ``INVALID_VALUE`` with the active profile unchanged (Req 10.5);
        an unavailable control interface raises ``INTERFACE_UNAVAILABLE`` before anything is
        applied (Req 10.7); success is returned only after read-back verification (Req 10.4).
        """
        return self._typed(lambda: self._load_profile(self._gpu(gpu_index), profile_id))

    # ------------------------------------------------------------------ reset
    def reset_profile(self, gpu_index: Optional[int] = None) -> ControlResult:
        """Restore the active profile to its last-saved state (re-apply stored settings)."""
        return self._typed(lambda: self._reset_profile(self._gpu(gpu_index)))

    # ------------------------------------------------------------------ startup
    def startup_auto_apply(self) -> Optional[bool]:
        """Populated [Startup] section in the attributed per-GPU file (corroboration).

        Returns True/False when the directory is readable and attributed; None when the
        directory is missing, ambiguous, or the file is unparseable (ownership reporting
        then says startup auto-apply is not observable).
        """
        try:
            snapshot = self._reader.read()
        except PluginError:
            return None
        if snapshot.state is not ProfileSourceState.AVAILABLE:
            return None
        return snapshot.startup_present

    # ------------------------------------------------------------------ helpers
    def _gpu(self, gpu_index: Optional[int]) -> int:
        return self.gpu_index if gpu_index is None else gpu_index

    @staticmethod
    def _typed(fn):
        try:
            return fn()
        except PluginError:
            raise
        except Exception as exc:  # pragma: no cover - Req 10.9: never an unhandled exception
            raise PluginError(
                ErrorCode.COMM_FAILURE,
                "I couldn't complete that profile operation just now. Try again.",
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc

    # ------------------------------------------------------------------ get_profiles
    def _get_profiles(self, gpu_index: int) -> Sequence[Profile]:
        snapshot = self._read_snapshot()
        present = [s for s in snapshot.slots if s.present]
        if not present:
            return []

        active_id: Optional[int] = None
        try:
            state = self._client.read_tuning_state(gpu_index)
            matches = [
                s.slot for s in present if _slot_matches(s, state, self.tolerance)
            ]
            if len(matches) == 1:  # ambiguous (0 or >1) => never mark active
                active_id = matches[0]
        except PluginError:
            # Control interface unavailable -> profiles listed, none marked active.
            active_id = None

        return tuple(
            Profile(
                id=s.slot,
                name=f"Profile {s.slot}",
                is_active=(s.slot == active_id),
                summary=describe_stored_settings(s.values),
            )
            for s in present
        )

    # ------------------------------------------------------------------ load_profile
    def _load_profile(self, gpu_index: int, profile_id: object) -> ControlResult:
        self._require_valid_id(profile_id)
        assert isinstance(profile_id, int)  # narrowed by _require_valid_id
        snapshot = self._read_snapshot()
        slot = next((s for s in snapshot.slots if s.slot == profile_id and s.present), None)
        if slot is None:
            raise _no_profile_error(profile_id)

        caps = self._resolve_control(gpu_index)
        notices, applied = self._apply_slot(gpu_index, caps, slot)
        if not applied:
            raise PluginError(
                ErrorCode.INTERFACE_UNAVAILABLE,
                f"Profile {profile_id} contains no settings I can apply here.",
                detail="; ".join(notices) if notices else "no mappable/applicable values",
            )
        self._verify_applied(gpu_index, applied)

        message = f"Profile {profile_id} applied."
        if notices:
            message += " Note: " + " ".join(notices)
        return ControlResult(
            feature=ControlFeature.PROFILE_LOAD,
            requested_value=None,
            applied_value=None,
            applied=True,
            message=message,
        )

    # ------------------------------------------------------------------ reset_profile
    def _reset_profile(self, gpu_index: int) -> ControlResult:
        caps = self._resolve_control(gpu_index)
        snapshot = self._read_snapshot()
        present = [s for s in snapshot.slots if s.present]
        if not present:
            raise PluginError(
                ErrorCode.INVALID_VALUE,
                "No Afterburner profile is active to reset.",
                detail="no stored profiles present",
            )
        try:
            state = self._client.read_tuning_state(gpu_index)
        except PluginError as exc:
            raise _control_unavailable() from exc
        matches = [s for s in present if _slot_matches(s, state, self.tolerance)]
        if len(matches) != 1:
            raise PluginError(
                ErrorCode.INVALID_VALUE,
                "I can't tell which Afterburner profile is active, so I can't reset it.",
                detail=f"{len(matches)} slots matched the applied state",
            )
        slot = matches[0]
        notices, applied = self._apply_slot(gpu_index, caps, slot)
        if not applied:
            raise PluginError(
                ErrorCode.INTERFACE_UNAVAILABLE,
                f"Profile {slot.slot} contains no settings I can apply here.",
                detail="; ".join(notices) if notices else "no mappable/applicable values",
            )
        self._verify_applied(gpu_index, applied)
        message = f"Profile {slot.slot} restored."
        if notices:
            message += " Note: " + " ".join(notices)
        return ControlResult(
            feature=ControlFeature.PROFILE_RESET,
            requested_value=None,
            applied_value=None,
            applied=True,
            message=message,
        )

    # ------------------------------------------------------------------ internals
    def _read_snapshot(self) -> ProfilesSnapshot:
        snapshot = self._reader.read()
        if snapshot.state in (
            ProfileSourceState.AMBIGUOUS_GPU_FILE,
            ProfileSourceState.UNPARSEABLE,
            ProfileSourceState.NO_DIRECTORY,
        ):
            raise PluginError(
                ErrorCode.INTERFACE_UNAVAILABLE,
                "I couldn't read Afterburner's profiles right now.",
                detail=f"reader state={snapshot.state.value}: "
                + ("; ".join(snapshot.notices) if snapshot.notices else "unattributable"),
            )
        return snapshot

    def _resolve_control(self, gpu_index: int) -> "GpuCapabilities":
        if self._resolver is None:
            raise _control_unavailable()
        caps = self._resolver.resolve(gpu_index)
        if caps.control_interface_status is not InterfaceStatus.OK:
            raise _control_unavailable()
        if not caps.supports(ControlFeature.PROFILE_LOAD):
            raise _control_unavailable()
        return caps

    def _apply_slot(
        self, gpu_index: int, caps, slot: ProfileSlotInfo
    ) -> Tuple[List[str], List[Tuple[ControlFeature, float]]]:
        """Apply one slot's stored values -> (notices, [(feature, applied_value), ...])."""
        notices: List[str] = []
        applied: List[Tuple[ControlFeature, float]] = []

        values = slot.values
        for key, feature, scale in _MAPPED_KEYS:
            raw = values.get(key)
            if raw is None:
                continue
            try:
                numeric = float(raw)
            except (TypeError, ValueError):
                notices.append(f"{key} skipped (not a number).")
                continue
            if not _finite(numeric):
                notices.append(f"{key} skipped (not a valid number).")
                continue
            converted = numeric * scale
            self._apply_one(
                gpu_index, caps, feature, converted, key, notices, applied
            )

        fan_mode_raw = values.get(_FAN_MODE_KEY)
        fan_speed_raw = values.get(_FAN_SPEED_KEY)
        if fan_speed_raw is not None:
            if fan_mode_raw is None or not _fan_mode_is_fixed(fan_mode_raw):
                notices.append(
                    f"FanSpeed skipped (stored fan mode isn't a fixed fan speed)."
                )
            else:
                try:
                    fan_pct = float(fan_speed_raw)
                except (TypeError, ValueError):
                    notices.append("FanSpeed skipped (not a number).")
                else:
                    if _finite(fan_pct):
                        self._apply_one(
                            gpu_index,
                            caps,
                            ControlFeature.FAN_PERCENT,
                            fan_pct,
                            "FanSpeed",
                            notices,
                            applied,
                        )
                    else:
                        notices.append("FanSpeed skipped (not a valid number).")

        for key in _IGNORED_WITH_NOTICE:
            if values.get(key):
                notices.append(f"{key} ignored (no matching control).")
        return notices, applied

    def _apply_one(
        self,
        gpu_index: int,
        caps,
        feature: ControlFeature,
        value: float,
        key: str,
        notices: List[str],
        applied: List[Tuple[ControlFeature, float]],
    ) -> None:
        if not caps.supports(feature):
            notices.append(f"{key} ignored (not supported on this GPU/version).")
            return
        assert self._validator is not None
        try:
            safe_value, clamped = self._validator.validate_and_clamp(
                gpu_index, feature, value
            )
        except PluginError as exc:  # malformed stored value: skip, never fail silently-crash
            notices.append(f"{key} skipped ({exc.user_message.lower()})")
            return
        if clamped:
            notices.append(f"{key} clamped to the supported value {safe_value:g}.")
        self._client.apply_control(gpu_index, feature, safe_value)
        applied.append((feature, safe_value))

    def _verify_applied(
        self, gpu_index: int, applied: List[Tuple[ControlFeature, float]]
    ) -> None:
        """Read the applied state back and confirm every applied value within tolerance."""
        try:
            state = self._client.read_tuning_state(gpu_index)
        except PluginError as exc:
            raise PluginError(
                ErrorCode.COMM_FAILURE,
                "I applied the profile but couldn't verify it took effect.",
                detail=f"read-back failed: {exc.code.value}",
            ) from exc
        for feature, safe_value in applied:
            attr = _FEATURE_STATE_ATTR[feature]
            current = getattr(state, attr, None)
            if current is None or abs(current - safe_value) > self.tolerance:
                raise PluginError(
                    ErrorCode.COMM_FAILURE,
                    "I applied the profile but the change didn't stick — "
                    "Afterburner may have overridden it.",
                    detail=(
                        f"{feature.value}: expected {safe_value}, read back "
                        f"{current if current is not None else 'unavailable'}"
                    ),
                )

    @staticmethod
    def _require_valid_id(profile_id: object) -> None:
        """Reject non-int / out-of-range ids with a typed error (nothing applied yet)."""
        if (
            not isinstance(profile_id, int)
            or isinstance(profile_id, bool)
            or not 1 <= profile_id <= 5
        ):
            raise _no_profile_error(profile_id)


def describe_stored_settings(values: Mapping[str, str]) -> str:
    """NL summary of one slot's stored keys. Never dumps the VFCurve hex blob."""
    parts: List[str] = []
    power = _optional_float(values.get("PowerLimit"))
    if power is not None:
        parts.append(f"power {power:g}%")
    core = _optional_float(values.get("CoreClkBoost"))
    if core is not None:
        parts.append(f"core {_signed_mhz(core * 0.001)}")
    memory = _optional_float(values.get("MemClkBoost"))
    if memory is not None:
        parts.append(f"memory {_signed_mhz(memory * 0.001)}")
    voltage = _optional_float(values.get("CoreVoltageBoost"))
    if voltage is not None:
        parts.append(f"voltage boost {voltage:g}")
    thermal = _optional_float(values.get("ThermalLimit"))
    if thermal is not None:
        parts.append(f"thermal limit {thermal:g} C")
    fan = _fan_summary(values)
    if fan is not None:
        parts.append(fan)
    if values.get("VFCurve"):
        parts.append("VF curve stored")
    return ", ".join(parts)


def _optional_float(raw: Optional[str]) -> Optional[float]:
    if raw is None or raw == "":
        return None
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _signed_mhz(value: float) -> str:
    if value > 0:
        return f"+{value:g} MHz"
    return f"{value:g} MHz"


def _fan_summary(values: Mapping[str, str]) -> Optional[str]:
    mode_raw = values.get("FanMode")
    speed = _optional_float(values.get("FanSpeed"))
    if mode_raw is None and speed is None:
        return None
    if mode_raw is not None and _fan_mode_is_fixed(mode_raw):
        if speed is not None:
            return f"fan {speed:g}% fixed"
        return "fan fixed"
    if mode_raw is not None:
        if speed is not None:
            return f"fan mode {mode_raw}, {speed:g}%"
        return f"fan mode {mode_raw}"
    return f"fan {speed:g}%"


# --------------------------------------------------------------------------- matching
def _finite(value: float) -> bool:
    return math.isfinite(value)


def _fan_mode_is_fixed(raw: str) -> bool:
    try:
        return int(raw) == 1
    except (TypeError, ValueError):
        return False


def _slot_matches(
    slot: ProfileSlotInfo, state: object, tolerance: float
) -> bool:
    """Does the applied tuning state equal this slot's stored values (within tolerance)?

    Every populated, mappable stored key must match; a stored value whose applied
    counterpart is unknown/unavailable makes the match ambiguous => False (never guessed).
    """
    assert isinstance(state, TuningState)
    values = slot.values

    def _check(key: str, scale: float, current: Optional[float]) -> Optional[bool]:
        raw = values.get(key)
        if raw is None:
            return None  # key not stored in this slot
        try:
            stored = float(raw) * scale
        except (TypeError, ValueError):
            return False  # stored garbage cannot equal the applied state
        if current is None:
            return False  # applied counterpart unavailable -> cannot confirm a match
        return abs(current - stored) <= tolerance

    comparisons = []
    for key, _, scale in _MAPPED_KEYS:
        result = _check(key, scale, _state_value(state, key))
        if result is False:
            return False
        if result is True:
            comparisons.append(True)

    # Fan: stored fixed-speed request must equal the applied manual fan speed.
    fan_mode = values.get(_FAN_MODE_KEY)
    fan_speed = values.get(_FAN_SPEED_KEY)
    if fan_speed is not None and fan_mode is not None and _fan_mode_is_fixed(fan_mode):
        if state.fan_percent is None:
            return False
        try:
            stored_fan = float(fan_speed)
        except (TypeError, ValueError):
            return False
        if abs(state.fan_percent - stored_fan) > tolerance:
            return False
        comparisons.append(True)

    return bool(comparisons)  # at least one stored setting must be comparable


def _state_value(state: object, key: str) -> Optional[float]:
    assert isinstance(state, TuningState)
    mapping = {
        "PowerLimit": state.power_limit_pct,
        "CoreClkBoost": state.core_offset_mhz,
        "MemClkBoost": state.memory_offset_mhz,
    }
    return mapping.get(key)
