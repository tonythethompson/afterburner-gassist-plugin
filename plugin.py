"""G-Assist plugin entry point (Protocol V2 install layout).

Path bootstrap note (verified against the real G-Assist RISE runtime): the engine runs the
manifest's ``executable`` with an isolated embedded python (``python313._pth`` — the
interpreter ignores ``PYTHONPATH`` and does not add the script's directory), so this module
puts its own folder (and its ``libs/``, when vendored) onto ``sys.path`` before importing
the ``afterburner`` package — the same self-bootstrap the reference plugins use.

Task 16 wiring: build the domain stack around an AfterburnerInterface, run the startup
sequence, then run the Protocol V2 message loop on stdin/stdout.

Interface selection (as the real adapters landed in plan tasks 18-20):

- Afterburner installed **and running** (MAHM shared memory readable): `CombinedAfterburner`
  over the read-only MAHM monitoring client (task 18) and the capability-gated MACM control
  client (task 20) — live telemetry, live capability/limits, mutex-guarded named-field
  writes with FLUSH + read-back verification. If MACM is momentarily down while MAHM is up,
  monitoring keeps working and writes degrade to typed read-only through
  `control_interface_status` (never fabricated).
- The chosen interface is wrapped in `ReconnectingAfterburner`: the engine keeps the plugin
  process for the whole session, so an "Afterburner isn't running" reading at spawn time
  must not stick forever. While down the wrapper reports the concrete typed status (probe
  at most every 2 s); the first call at least 2 s after Afterburner appears swaps in a live
  interface transparently (live finding during the real G-Assist conversation).
- Installed but not exposing shared memory: the `UnavailableAfterburner` fallback with the
  concrete observed status (NOT_RUNNING / UNAVAILABLE / UNSUPPORTED_VERSION / ACCESS_DENIED).
- Not installed: `UnavailableAfterburner` NOT_INSTALLED.

Startup still completes within budget and every call returns a typed, user-facing outcome
(Requirement 16.3 / Property 7); nothing escapes unhandled.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Path bootstrap (runs before any `afterburner` import): the G-Assist RISE runtime
# spawns plugins with an isolated embedded python (python*. _pth present, so
# PYTHONPATH is ignored and the script's folder is NOT added to sys.path). Mirror the
# reference plugins: insert this plugin's own folder, then its vendored libs/.
# ---------------------------------------------------------------------------
_PLUGIN_DIR = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)
_LIBS_DIR = os.path.join(_PLUGIN_DIR, "libs")
if os.path.isdir(_LIBS_DIR) and _LIBS_DIR not in sys.path:
    sys.path.insert(0, _LIBS_DIR)

# --------------------------------------------------------------------------- logging
# The engine treats stdout/stderr as the protocol channel, so ALL diagnostics go to a
# per-plugin log file (same convention as NVIDIA's reference plugins: rise\plugins\
# <name>\<name>.log). Never print or log to stdout/stderr from this process.
_LOG_PATH = os.path.join(_PLUGIN_DIR, "afterburner.log")
logger = logging.getLogger("afterburner.plugin")


def _resolve_log_path(
    candidates: Optional[Sequence[str]] = None,
) -> Optional[str]:
    """First writable log location: the plugin dir, then the user temp dir.

    The RISE plugins directory is admin-owned, so a non-elevated plugin process cannot
    create its log there — logging must never take the process down (Property 7).
    Returns None when nothing is writable (logging disabled).
    """
    for candidate in candidates or (
        _LOG_PATH,
        os.path.join(tempfile.gettempdir(), "afterburner.log"),
    ):
        try:
            with open(candidate, "a", encoding="utf-8"):
                pass
            return candidate
        except OSError:
            continue
    return None


def _setup_logging() -> None:
    path = _resolve_log_path()
    if path is None:  # pragma: no cover - needs an unwritable temp dir
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        return
    logging.basicConfig(
        filename=path,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        force=True,
    )


# Crash capture BEFORE any `afterburner` import: if the engine ever merges the plugin's
# stderr into its protocol channel, a startup traceback would read as a parse failure.
# From the very first line, every unhandled exception goes to the log file only.
_setup_logging()


def _exception_hook(etype, value, tb) -> None:
    logger.error("unhandled exception", exc_info=(etype, value, tb))


sys.excepthook = _exception_hook

from afterburner.integration.combined import CombinedAfterburner
from afterburner.integration.fallback import UnavailableAfterburner
from afterburner.integration.interface import AfterburnerInterface
from afterburner.integration.macm import AfterburnerControlClient
from afterburner.integration.mahm import AfterburnerMonitoringClient, find_install_dir
from afterburner.integration.reconnect import ReconnectingAfterburner
from afterburner.models import InterfaceStatus
from afterburner.protocol.plugin import (
    CommandFailed,
    GAssistPlugin,
    bind_sdk_plugin,
    build_services,
    run_plugin_loop,
)

try:
    from gassist_sdk import Plugin as GAssistSdkPlugin
    from gassist_sdk.types import ErrorCode as SdkErrorCode
    from gassist_sdk.types import JsonRpcResponse
except ImportError:
    GAssistSdkPlugin = None  # type: ignore[misc, assignment]
    JsonRpcResponse = None  # type: ignore[misc, assignment]
    SdkErrorCode = None  # type: ignore[misc, assignment]

_INSTALL_PROFILES_CANDIDATES = (
    r"C:\Program Files (x86)\MSI Afterburner\Profiles",
    r"C:\Program Files\MSI Afterburner\Profiles",
)


def _load_manifest_meta() -> Tuple[str, str, str, List[dict]]:
    """Mirror manifest.json into the initialize response (name/version/commands)."""
    manifest_path = Path(_PLUGIN_DIR) / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        functions = [
            {"name": f["name"], "description": f.get("description", "")}
            for f in manifest.get("functions", [])
            if isinstance(f, dict) and f.get("name")
        ]
        return (
            str(manifest.get("name", "afterburner")),
            str(manifest.get("version", "1.0.0")),
            str(manifest.get("description", "")),
            functions,
        )
    except (OSError, ValueError) as exc:  # pragma: no cover - packaged manifest always valid
        logger.warning("could not read manifest.json: %s", exc)
        return "afterburner", "1.0.0", "", []


def find_profiles_dir() -> Optional[Path]:
    """Detected installation's Profiles directory (env override first, then well-known)."""
    override = os.environ.get("MSI_AFTERBURNER_PROFILES_DIR")
    candidates = ([override] if override else []) + list(_INSTALL_PROFILES_CANDIDATES)
    for candidate in candidates:
        path = Path(candidate)
        if path.is_dir():
            return path
    return None


def _build_live_interface() -> AfterburnerInterface:
    """One concrete interface selection pass (fresh MAHM/MACM clients each call)."""
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


def build_interface() -> AfterburnerInterface:
    """Reconnecting interface: the engine keeps one process for the whole session, so a
    spawn-time "Afterburner is down" must not stick forever — the wrapper re-detects on a
    2 s cooldown and swaps in a live client the moment Afterburner appears (live finding)."""
    return ReconnectingAfterburner(
        _build_live_interface,
        _build_live_interface(),
        on_swapped=lambda _previous: logger.info(
            "Afterburner detected again — swapped to a live interface"
        ),
    )


def build_entry_plugin(*, clock=None) -> GAssistPlugin:
    """Compose the plugin for the real process (live monitoring + control when running)."""
    interface = build_interface()
    profiles_dir = find_profiles_dir()
    name, version, description, functions_meta = _load_manifest_meta()
    services = build_services(
        interface,
        profiles_dir,
        clock=clock,
        optimize_read_interval_s=1.0,  # real read-back cadence between fan polls
    )
    return GAssistPlugin(
        services,
        clock=clock,
        name=name,
        version=version,
        description=description,
        functions_meta=functions_meta,
    )


def _wire_trace(side: str, payload) -> None:
    """Full wire transcript -> afterburner.log (engine debugging; never the wire channel)."""
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) > 4000:
        text = text[:4000] + "…"
    logger.info("wire %s: %s", side, text)


def main() -> int:
    """Entry point: everything that could fail logs to afterburner.log and exits cleanly,
    so the engine's protocol channel never sees a traceback (parse-failure prevention)."""
    logger.info("afterburner plugin starting (python %s)", sys.version.split()[0])
    try:
        plugin = build_entry_plugin()
    except Exception:  # pragma: no cover - startup must never touch the wire channel
        logger.exception("build_entry_plugin failed")
        return 1
    logger.info(
        "plugin=%s version=%s functions=%d (startup done before message loop)",
        plugin.name,
        plugin.version,
        len(plugin.registered_functions()),
    )
    try:
        rc = run_plugin_loop(plugin, trace=_wire_trace)
        logger.info("message loop exited rc=%s", rc)
        return rc
    except KeyboardInterrupt:  # pragma: no cover - interactive stop
        logger.info("keyboard interrupt")
        return 0
    except Exception:  # pragma: no cover - never let a crash reach the wire channel
        logger.exception("run_plugin_loop crashed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
