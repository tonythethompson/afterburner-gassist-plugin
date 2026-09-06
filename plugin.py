"""G-Assist plugin entry point (Protocol V2 install layout).

Task 16 wiring: build the domain stack around an AfterburnerInterface, run the startup
sequence, then run the Protocol V2 message loop on stdin/stdout.

Interface selection (as the real adapters landed in plan tasks 18-20):

- Afterburner installed **and running** (MAHM shared memory readable): `CombinedAfterburner`
  over the read-only MAHM monitoring client (task 18) and the capability-gated MACM control
  client (task 20) — live telemetry, live capability/limits, mutex-guarded named-field
  writes with FLUSH + read-back verification. If MACM is momentarily down while MAHM is up,
  monitoring keeps working and writes degrade to typed read-only through
  `control_interface_status` (never fabricated).
- Installed but not exposing shared memory: the `UnavailableAfterburner` fallback with the
  concrete observed status (NOT_RUNNING / UNAVAILABLE / UNSUPPORTED_VERSION / ACCESS_DENIED).
- Not installed: `UnavailableAfterburner` NOT_INSTALLED.

Startup still completes within budget and every call returns a typed, user-facing outcome
(Requirement 16.3 / Property 7); nothing escapes unhandled.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from afterburner.integration.combined import CombinedAfterburner
from afterburner.integration.fallback import UnavailableAfterburner
from afterburner.integration.interface import AfterburnerInterface
from afterburner.integration.macm import AfterburnerControlClient
from afterburner.integration.mahm import AfterburnerMonitoringClient, find_install_dir
from afterburner.models import InterfaceStatus
from afterburner.protocol.plugin import GAssistPlugin, build_services, run_plugin_loop

_INSTALL_PROFILES_CANDIDATES = (
    r"C:\Program Files (x86)\MSI Afterburner\Profiles",
    r"C:\Program Files\MSI Afterburner\Profiles",
)


def find_profiles_dir() -> Optional[Path]:
    """Detected installation's Profiles directory (env override first, then well-known)."""
    override = os.environ.get("MSI_AFTERBURNER_PROFILES_DIR")
    candidates = ([override] if override else []) + list(_INSTALL_PROFILES_CANDIDATES)
    for candidate in candidates:
        path = Path(candidate)
        if path.is_dir():
            return path
    return None


def build_interface() -> AfterburnerInterface:
    """Pick the concrete AfterburnerInterface for this process (see module docstring)."""
    install_dir = find_install_dir()
    if install_dir is None:
        return UnavailableAfterburner(
            InterfaceStatus.NOT_INSTALLED, detail="no Afterburner install detected"
        )
    monitoring = AfterburnerMonitoringClient(install_dir=install_dir)
    status = monitoring.detect()
    if status is not InterfaceStatus.OK:
        # Installed but the shared-memory interface is not readable right now: degrade with
        # the concrete observed status instead of fabricating monitoring data.
        return UnavailableAfterburner(status, detail=f"MAHM detect: {status.value}")
    control = AfterburnerControlClient(install_dir=install_dir)
    return CombinedAfterburner(monitoring=monitoring, control=control)


def build_entry_plugin(*, clock=None) -> GAssistPlugin:
    """Compose the plugin for the real process (live monitoring + control when running)."""
    interface = build_interface()
    profiles_dir = find_profiles_dir()
    services = build_services(
        interface,
        profiles_dir,
        clock=clock,
        optimize_read_interval_s=1.0,  # real read-back cadence between fan polls
    )
    return GAssistPlugin(services, clock=clock)


def main() -> int:
    plugin = build_entry_plugin()
    try:
        return run_plugin_loop(plugin)
    except KeyboardInterrupt:  # pragma: no cover - interactive stop
        return 0
    finally:
        sys.stderr.flush()


if __name__ == "__main__":
    raise SystemExit(main())
