"""TuningOwnershipService — honest report of the single shared GPU tuning state.

Design component table + Requirements 19.x: the GPU exposes ONE shared set of driver tuning
parameters. Afterburner is the only authority this plugin reads or writes (through the
AfterburnerInterface); external authorities — NVIDIA App Automatic Tuning, G-Assist native
tuning, other OC utilities — are **never observable** through Afterburner's interfaces, so
the report always marks them UNKNOWN_NOT_OBSERVABLE, never asserts their state, never claims
their values stack with Afterburner's, and never modifies or disables them (Requirement 19.1
/ Property 10).

Everything Afterburner-observable is read **live on every invocation** (never from the
telemetry cache): the applied tuning state read back through the control interface, the
active profile matched by ProfileManager against that state, and the populated ``[Startup]``
auto-apply corroboration from the read-only Profiles directory.
"""

from __future__ import annotations

from typing import Dict, Optional

from ..integration.client import AfterburnerClient
from ..models import (
    AfterburnerAuthoritySnapshot,
    AuthorityState,
    ControlFeature,
    ErrorCode,
    InterfaceStatus,
    PluginError,
    TuningOwnershipReport,
    TuningState,
)
from .profiles import ProfileManager

# Control feature -> TuningState attribute holding the applied value (live read-back).
_FEATURE_STATE_ATTR = {
    ControlFeature.POWER_LIMIT: "power_limit_pct",
    ControlFeature.CORE_OFFSET: "core_offset_mhz",
    ControlFeature.MEMORY_OFFSET: "memory_offset_mhz",
    ControlFeature.FAN_PERCENT: "fan_percent",
}

_FEATURE_LABELS = {
    ControlFeature.POWER_LIMIT: "power limit (%)",
    ControlFeature.CORE_OFFSET: "core clock offset (MHz)",
    ControlFeature.MEMORY_OFFSET: "memory clock offset (MHz)",
    ControlFeature.FAN_PERCENT: "fan speed (%)",
    ControlFeature.FAN_CURVE: "fan curve",
}

_EXTERNAL_WARNING = (
    "NVIDIA App Automatic Tuning, G-Assist native tuning, and other OC utilities write the "
    "same single GPU tuning state, but they can't be seen through Afterburner and may "
    "override or be overridden."
)


class TuningOwnershipService:
    """Builds tuning-ownership reports and confirmation clauses from Afterburner-observable
    state only."""

    def __init__(self, client: AfterburnerClient, profiles: ProfileManager) -> None:
        self._client = client
        self._profiles = profiles

    # ------------------------------------------------------------------ report
    def build_report(self, gpu_index: int = 0) -> TuningOwnershipReport:
        """Typed report: Afterburner-observable facts; externals always unknown."""
        status = self._client.detect()

        applied: Optional[TuningState] = None
        if status is InterfaceStatus.OK:
            try:
                applied = self._client.read_tuning_state(gpu_index)
            except PluginError:
                applied = None  # control map unreadable -> not observable right now

        active_profile_id: Optional[int] = None
        try:
            profiles = self._profiles.get_profiles(gpu_index)
            active = [p for p in profiles if p.is_active]
            active_profile_id = active[0].id if len(active) == 1 else None
        except PluginError:
            active_profile_id = None

        startup_auto_apply = self._profiles.startup_auto_apply()

        afterburner = AfterburnerAuthoritySnapshot(
            interface_status=status,
            applied_state=applied,
            active_profile_id=active_profile_id,
            startup_auto_apply_present=bool(startup_auto_apply),
        )
        return TuningOwnershipReport(
            gpu_index=gpu_index,
            afterburner=afterburner,
            nvidia_app_auto_tuning=AuthorityState.UNKNOWN_NOT_OBSERVABLE,
            gassist_native_tuning=AuthorityState.UNKNOWN_NOT_OBSERVABLE,
            other_oc_utilities=AuthorityState.UNKNOWN_NOT_OBSERVABLE,
            summary=self._summarize(afterburner),
        )

    # ------------------------------------------------------------------ clause
    def applied_value(
        self, gpu_index: int, feature: ControlFeature
    ) -> Optional[float]:
        """Live applied value for one control feature (None when unknown/unreadable)."""
        attr = _FEATURE_STATE_ATTR.get(feature)
        if attr is None:
            return None
        try:
            state = self._client.read_tuning_state(gpu_index)
        except PluginError:
            return None
        return getattr(state, attr, None)

    def ownership_clause(
        self,
        gpu_index: int,
        feature: ControlFeature,
        requested: Optional[float] = None,
    ) -> str:
        """The ownership wording risky confirmations carry (Requirement 19.2)."""
        label = _FEATURE_LABELS.get(feature, feature.value.replace("_", " "))
        current = self.applied_value(gpu_index, feature)
        current_text = (
            f"{current:g}" if current is not None else "not currently readable"
        )
        if requested is not None:
            state_text = (
                f"Afterburner's current {label} value is {current_text}; "
                "applying this change will replace it."
            )
        else:
            state_text = f"Afterburner's current {label} value is {current_text}."
        return f"{state_text} {_EXTERNAL_WARNING}"

    # ------------------------------------------------------------------ text
    def _summarize(self, afterburner: AfterburnerAuthoritySnapshot) -> str:
        parts: list[str] = []
        if afterburner.interface_status is not InterfaceStatus.OK:
            parts.append(
                "Afterburner isn't available, so no tuning authority can be reported as active."
            )
        else:
            parts.append("MSI Afterburner owns the tuning state this plugin can see.")
            if afterburner.applied_state is not None:
                parts.append(
                    "Applied: "
                    + _state_text(afterburner.applied_state)
                )
            else:
                parts.append("Applied state is not currently readable.")
            if afterburner.active_profile_id is not None:
                parts.append(
                    f"Active profile: Profile {afterburner.active_profile_id}."
                )
            elif afterburner.startup_auto_apply_present:
                parts.append("Startup auto-apply is enabled (no profile slot matches).")
            else:
                parts.append("No Afterburner profile is currently matched as active.")
        parts.append(_EXTERNAL_WARNING)
        return " ".join(parts)


def _state_text(state: TuningState) -> str:
    items = [
        f"power limit {state.power_limit_pct:g}%"
        if state.power_limit_pct is not None
        else None,
        f"core offset {state.core_offset_mhz:g} MHz"
        if state.core_offset_mhz is not None
        else None,
        f"memory offset {state.memory_offset_mhz:g} MHz"
        if state.memory_offset_mhz is not None
        else None,
        f"fan {state.fan_percent:g}% ({state.fan_mode})"
        if state.fan_percent is not None
        else f"fan mode {state.fan_mode}",
    ]
    return ", ".join(item for item in items if item)
