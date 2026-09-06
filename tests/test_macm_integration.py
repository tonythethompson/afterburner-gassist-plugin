"""Optional MACM control integration test (plan task 20.2).

Control writes touch real hardware, so this suite is opt-in behind an **explicit flag**
(`MSI_AFTERBURNER_CONTROL_INTEGRATION=1`) and additionally skipped when Afterburner/MACM is
not running. It exercises the real mutex-guarded named-field write + FLUSH + read-back
verification on one clamped control, restores the original value afterwards, and proves the
write-time no-op (applying the already-applied value returns success **without** issuing a
change).

**Elevation:** this suite must run at Afterburner's own privilege level. An elevated
Afterburner (verified live on 4.6.7.17439) never confirms MACM FLUSH commands from a
non-elevated process within the protocol's completion budget, so run pytest from an elevated
shell when Afterburner is elevated; `AfterburnerControlClient` detects the mismatch and
fails fast with `ACCESS_DENIED` instead of timing out.
"""
from __future__ import annotations

import os

import pytest

from afterburner.integration import macm
from afterburner.models import ControlFeature, InterfaceStatus

INSTALL_DIR = macm.find_install_dir()
pytestmark = [
    pytest.mark.skipif(
        INSTALL_DIR is None,
        reason="MSI Afterburner install not detected (opt-in integration test)",
    ),
    pytest.mark.skipif(
        os.environ.get("MSI_AFTERBURNER_CONTROL_INTEGRATION") != "1",
        reason="control tests write real hardware; set MSI_AFTERBURNER_CONTROL_INTEGRATION=1 "
        "to opt in",
    ),
]


def _live_client():
    client = macm.AfterburnerControlClient(install_dir=INSTALL_DIR)
    if client.detect() is not InterfaceStatus.OK:
        pytest.skip("MSI Afterburner is not currently exposing MACM shared memory")
    return client


def _pick_power_limit(client) -> tuple[int, float, float]:
    """A POWER_LIMIT feature with a sane range on GPU 0, or skip."""
    caps = client.read_capabilities(0)
    if ControlFeature.POWER_LIMIT not in caps.supported_controls:
        pytest.skip("POWER_LIMIT not advertised on this GPU")
    rng = caps.limits.power_limit_pct
    state = client.read_tuning_state(0)
    current = state.power_limit_pct
    if current is None or rng is None or rng.inverted:
        pytest.skip("power-limit state/range not readable")
    return 0, float(current), float(rng.max)


class TestMacmControlIntegration:
    def test_write_time_noop_returns_without_change(self) -> None:
        client = _live_client()
        gpu, current, _max = _pick_power_limit(client)
        result = client.apply_control(gpu, ControlFeature.POWER_LIMIT, current)
        assert result.applied is False
        assert "no change made" in result.message
        # Applied state is untouched.
        state = client.read_tuning_state(gpu)
        assert abs(state.power_limit_pct - current) <= macm.TUNING_MATCH_TOLERANCE

    def test_apply_and_read_back_single_control_then_restore(self) -> None:
        client = _live_client()
        gpu, current, maximum = _pick_power_limit(client)
        # Move a small, clamped step up (or down if pinned at max), then restore.
        target = min(current + 3.0, maximum)
        if abs(target - current) <= macm.TUNING_MATCH_TOLERANCE:
            target = max(current - 3.0, 0.0)
        if abs(target - current) <= macm.TUNING_MATCH_TOLERANCE:
            pytest.skip("no room to move power limit by a clamp step")
        try:
            result = client.apply_control(gpu, ControlFeature.POWER_LIMIT, target)
            assert result.applied is True
            assert result.replaced_value is not None
            state = client.read_tuning_state(gpu)
            assert abs(state.power_limit_pct - target) <= macm.TUNING_MATCH_TOLERANCE + 0.01
        finally:
            client.apply_control(gpu, ControlFeature.POWER_LIMIT, current)
            restored = client.read_tuning_state(gpu)
            assert abs(restored.power_limit_pct - current) <= macm.TUNING_MATCH_TOLERANCE + 0.01
