"""Wire-contract gate: run NVIDIA's official plugin_emulator against dist/afterburner.

The ``plugin_emulator`` (github.com/NVIDIA/G-Assist/plugins/plugin_emulator) is NVIDIA's
Python mirror of the engine's Protocol V2 side. Running our built plugin through it is the
closest automated proxy for the real RISE engine — it reproduced the exact failure the live
engine reported as *"Could not parse JSON-RPC message from afterburner plugin"* when
``complete.data`` was a dict ("can only concatenate str (not \"dict\") to str", 2026-09-06).

This check gates on the wire-contract regression signals that bug produced and that a
conforming plugin must never emit:

- **Reader-level parse failures** logged by the emulator ("Failed to parse message from
  plugin", "Invalid JSON-RPC", "Unknown message type", "Message too large", "Reader
  error") — a frame the engine cannot consume.
- **Timeouts / dead process** — an execute that never reaches a terminal frame.
- **initialize** must succeed and every manifest function must resolve and reach a
  terminal ``complete`` (data = NL text) or typed ``error`` notification.
- **Passthrough**: a risky (high-risk) execute must return ``keep_session`` (the engine
  holds the session) and an ``input`` verdict must be acknowledged and resolved.

Environment tolerance: CI runners have no Afterburner, so functions degrade to typed
"not available" errors — those are valid terminal frames, not wire failures. On a live
host, risky functions prompt (never write) and are cancelled via ``input``.

The two write-applying low-risk functions (``load_profile``, ``reset_tuning``) are
included by DEFAULT only in a degraded environment (Afterburner absent/unreachable,
where every write path fails fast as a typed error before touching hardware) and
skipped when Afterburner is live — so the gate keeps full 18-function coverage in CI
while staying safe to run on a real machine. ``--allow-writes`` forces them everywhere.

Usage:
    python tools/check_emulator.py \
        [--emulator <nvidia-gassist>/plugins/plugin_emulator] \
        [--sdk <nvidia-gassist>/plugins/sdk/python] \
        [--out dist/afterburner] [--allow-writes] [--skip-build] [--timeout 10]

The emulator defaults to a local ``G-Assist/plugins/plugin_emulator`` checkout when
present, else the ``GA_EMULATOR_DIR`` environment variable.

Exit codes: 0 = wire contract passes; 1 = a wire-contract regression was detected;
2 = usage / environment error (emulator or SDK not found, build failed).
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The two low-risk functions that APPLY writes without a confirmation prompt. Executing
# them against a live Afterburner changes the user's tuning, so the gate skips them by
# default; CI runners never have Afterburner, so --allow-writes is safe there.
WRITE_APPLYING = frozenset({"load_profile", "reset_tuning"})

# Function arguments the sweep executes. Keys not listed get {"gpu_index": 0}.
FUNCTION_ARGS: dict = {
    "set_power_limit": {"percent": 90, "gpu_index": 0},
    "set_core_offset": {"offset_mhz": 50, "gpu_index": 0},
    "set_memory_offset": {"offset_mhz": 100, "gpu_index": 0},
    "set_fan_percent": {"percent": 40, "gpu_index": 0},
    "set_fan_curve": {"points": [[30, 30], [60, 50], [80, 70]], "gpu_index": 0},
    "optimize_quiet": {"gpu_index": 0},
    "optimize_thermal": {"target_c": 70, "gpu_index": 0},
    "load_profile": {"profile_id": 1, "gpu_index": 0},
    "reset_tuning": {"gpu_index": 0},
}

# Protocol-level failures (as opposed to typed domain errors like "Afterburner isn't
# running"). Anything else in `ExecutionResult.error` is an acceptable typed outcome.
PROTOCOL_FAILURES = (
    "Request timeout",
    "Plugin process died",
    "Failed to send request",
    "Plugin not ready",
)

# Reader-log lines that mean the engine could not consume one of our frames.
READER_FAILURE_MARKERS = (
    "Failed to parse message from plugin",
    "Invalid JSON-RPC",
    "Unknown message type",
    "Message too large",
    "Reader error",
)


class _Capture(logging.Handler):
    """Collects emulator log records so reader-level failures are assertable."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - trivial
        self.records.append(record)


def classify_environment(text: str) -> str:
    """Classify the probe outcome: "live", "degraded", or "unknown".

    ``show_configuration`` completes in both worlds: the message starts
    "MSI Afterburner: ok ..." when the control interface is reachable, and names the
    failure otherwise. Unknown -> callers fail safe (skip write-applying functions).
    """
    low = text.lower()
    if "msi afterburner: ok" in low or "owns the tuning state" in low:
        return "live"
    degraded = (
        "not available", "isn't running", "doesn't appear", "not_running",
        "not_installed", "not installed", "unavailable", "no write controls",
        "read-only", "not reachable",
    )
    if any(token in low for token in degraded):
        return "degraded"
    return "unknown"


def _find_emulator(arg: str | None) -> Path | None:
    if arg:
        path = Path(arg)
        return path if (path / "protocol.py").is_file() else None
    env = Path(__import__("os").environ.get("GA_EMULATOR_DIR", ""))
    if env.is_dir() and (env / "protocol.py").is_file():
        return env
    local = ROOT / "G-Assist" / "plugins" / "plugin_emulator"
    return local if (local / "protocol.py").is_file() else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emulator", default=None, help="path to NVIDIA plugin_emulator")
    parser.add_argument("--sdk", default=None, help="path to the NVIDIA gassist_sdk source")
    parser.add_argument("--out", default=str(ROOT / "dist" / "afterburner"),
                        help="build output dir (assembled by build.py)")
    parser.add_argument("--allow-writes", action="store_true",
                        help="force load_profile / reset_tuning even when Afterburner is "
                             "reachable (may write on a live host)")
    parser.add_argument("--skip-build", action="store_true",
                        help="use the existing --out tree instead of rebuilding")
    parser.add_argument("--timeout", type=float, default=10.0,
                        help="seconds to wait per execute call")
    args = parser.parse_args(argv)

    emulator = _find_emulator(args.emulator)
    if emulator is None:
        print("check_emulator: NVIDIA plugin_emulator not found. Pass --emulator or set "
              "GA_EMULATOR_DIR, or clone github.com/NVIDIA/G-Assist into G-Assist/ "
              "(pinned: ref d69a4e5d360d4ca156eccfa6c02774e8804fc931).", file=sys.stderr)
        return 2
    sdk = Path(args.sdk) if args.sdk else emulator.parent / "sdk" / "python"
    if not (sdk / "gassist_sdk").is_dir():
        print(f"check_emulator: gassist_sdk source not found under {sdk}", file=sys.stderr)
        return 2

    out = Path(args.out)
    if not args.skip_build:
        build = subprocess.run(
            [sys.executable, str(ROOT / "build.py"), "--out", str(out), "--sdk", str(sdk),
             "--force"],
            capture_output=True, text=True,
        )
        if build.returncode != 0:
            print(build.stdout[-2000:])
            print(build.stderr[-2000:], file=sys.stderr)
            print("check_emulator: build.py failed", file=sys.stderr)
            return 2
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    expected = {f["name"] for f in manifest["functions"]}
    if len(expected) != 18:
        print(f"check_emulator: expected 18 manifest functions, found {len(expected)}",
              file=sys.stderr)
        return 2

    # Capture emulator logs BEFORE the engine constructs (its basicConfig is a no-op once
    # the root logger has a handler).
    capture = _Capture()
    root = logging.getLogger()
    root.addHandler(capture)
    root.setLevel(logging.WARNING)

    sys.path.insert(0, str(emulator.parent))  # parent dir of the plugin_emulator package
    try:
        from plugin_emulator import PluginEngine  # noqa: E402
        from plugin_emulator.engine import EngineConfig  # noqa: E402
    except ImportError as exc:
        root.removeHandler(capture)
        print(f"check_emulator: cannot import plugin_emulator from {emulator}: {exc}",
              file=sys.stderr)
        return 2

    failures: list[str] = []
    calls = 0
    skipped_writes: list[str] = []

    tmp = Path(tempfile.mkdtemp(prefix="ab-emu-check-"))
    engine = None
    try:
        plugins_dir = tmp / "plugins"
        plugins_dir.mkdir()
        shutil.copytree(out, plugins_dir / "afterburner")

        engine = PluginEngine(config=EngineConfig(
            plugins_dir=str(plugins_dir),
            watch_plugins=False,
            timeout_ms=int(args.timeout * 1000),
        ))
        if not engine.initialize():
            failures.append("initialize did not report success")
            print(f"initialize: FAIL")
        else:
            print(f"initialize: PASS (plugins: "
                  f"{', '.join(p.name for p in engine.list_plugins())})")

        # Probe whether Afterburner is reachable. Write-applying functions are only safe
        # (can't touch hardware) when it isn't, so the gate runs them in degraded
        # environments and skips them on a live host.
        probe = engine.execute("show_configuration", {"gpu_index": 0})
        environment = classify_environment(probe.response or probe.error or "")
        if args.allow_writes:
            print(f"environment: {environment} (--allow-writes: load_profile/"
                  f"reset_tuning forced)")
        elif environment == "degraded":
            print(f"environment: degraded (no reachable Afterburner) — write-applying "
                  f"functions included (they fail fast without touching hardware)")
        else:
            print(f"environment: {environment} — write-applying functions skipped "
                  f"(load_profile/reset_tuning would write; --allow-writes to force)")

        for name in sorted(expected):
            if name in WRITE_APPLYING and not (
                args.allow_writes or environment == "degraded"
            ):
                skipped_writes.append(name)
                print(f"  {name}: SKIP (write-applying; environment is {environment})")
                continue
            arguments = FUNCTION_ARGS.get(name, {"gpu_index": 0})
            try:
                res = engine.execute(name, dict(arguments))
            except Exception as exc:  # engine-side exception == wire failure
                failures.append(f"{name}: engine exception {type(exc).__name__}: {exc}")
                print(f"  {name}: FAIL (engine exception)")
                continue
            calls += 1
            if res.error in PROTOCOL_FAILURES or (
                res.error and "timeout" in res.error.lower()
            ):
                failures.append(f"{name}: {res.error}")
                print(f"  {name}: FAIL ({res.error})")
                continue
            if not res.success and not res.error:
                failures.append(f"{name}: failed without an error message")
                print(f"  {name}: FAIL (no error message)")
                continue
            if res.awaiting_input:
                # Risky prompt: the engine held the session; resolve with a cancel.
                try:
                    res2 = engine.send_input("cancel")
                except Exception as exc:
                    failures.append(f"{name}: input(cancel) raised {exc}")
                    print(f"  {name}: FAIL (input(cancel) raised {exc})")
                    continue
                if res2.error in PROTOCOL_FAILURES or (
                    res2.error and "timeout" in res2.error.lower()
                ):
                    failures.append(f"{name}: input(cancel) -> {res2.error}")
                    print(f"  {name}: FAIL (input(cancel) -> {res2.error})")
                    continue
                print(f"  {name}: PASS (prompt keep_session -> input(cancel) resolved"
                      + (f" [{res2.error}]" if res2.error else "") + ")")
            else:
                outcome = "complete" if res.success else f"typed error ({res.error})"
                print(f"  {name}: PASS ({outcome})")

        # Reader-level parse failures are the exact wire-contract regression signal.
        reader_failures = [
            r.getMessage()
            for r in capture.records
            if any(marker in r.getMessage() for marker in READER_FAILURE_MARKERS)
        ]
        for line in reader_failures:
            failures.append(f"engine reader: {line}")

        if skipped_writes:
            print(f"note: skipped write-applying functions (live host): "
                  f"{', '.join(skipped_writes)}")
    finally:
        if engine is not None:
            try:
                engine.shutdown()
            except Exception:  # pragma: no cover - cleanup only
                pass
        shutil.rmtree(tmp, ignore_errors=True)
        root.removeHandler(capture)

    if failures:
        print(f"WIRE-CONTRACT: FAIL ({len(failures)}):")
        for f in failures[:15]:
            print(f"  - {f}")
        return 1
    print(f"WIRE-CONTRACT: PASS ({calls} functions executed, "
          f"{len(expected) - calls} live-host write-skips, zero reader parse failures)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
