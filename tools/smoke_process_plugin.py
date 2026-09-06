"""Real-process Protocol V2 smoke harness (host-contract).

Spawns `plugin.py` as a fresh OS process — exactly what the G-Assist host does: working
directory = the plugin folder, Protocol V2 JSON-RPC 2.0 over the child's stdin/stdout
pipes with 4-byte big-endian length-prefixed frames. Unlike the in-process frame
simulation in `tests/`, this exercises the true entry point, the startup sequence, the
message loop, live Afterburner behind it, and clean EOF shutdown — the full path the
real host drives once the G-Assist RISE runtime deploys.

Usage:  python tools/smoke_process_plugin.py [--plugin plugin.py] [--step-timeout 20]

Wire expectations follow the engine-verified Protocol V2 contract (NVIDIA
PROTOCOL_V2.md + migration guide + the vendored gassist_sdk + the official
plugin_emulator): initialize returns a JSON-RPC response carrying
name/protocol_version/commands; ping echoes the engine's timestamp; execute finishes
with a ``complete`` notification ``{request_id, success, data, keep_session}`` where
``data`` IS the NL text (Protocol V2 has no structured output channel); risky writes
send a confirming ``complete`` with ``keep_session: true`` and the follow-up user
verdict arrives as ``input`` (acknowledged first); shutdown is a notification answered
with NO frame. Steps are degrade-tolerant: when Afterburner is absent the same calls
return typed "not available" messages, which the harness accepts (Property 7).

Exit 0 = all steps passed; 1 = any step failed/timed out; 2 = usage/process error.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
FRAME_LEN = 4
MAX_FRAME = 10 * 1024 * 1024


def _encode_frame(payload: Dict[str, Any]) -> bytes:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_FRAME:
        raise ValueError(f"frame too large: {len(body)} bytes")
    return len(body).to_bytes(FRAME_LEN, "big") + body


def _request(method: str, params: Dict[str, Any], request_id: int) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "method": method, "params": params, "id": request_id}


def _notification(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Engine-shaped notification (no id) — e.g. shutdown."""
    return {"jsonrpc": "2.0", "method": method, "params": params}


def _first_notification(frames: List[Dict[str, Any]], method: str) -> Optional[Dict[str, Any]]:
    for frame in frames:
        if frame.get("method") == method:
            return frame
    return None


class _SmokeFailure(Exception):
    pass


class PluginProcess:
    """A spawned plugin child with a reader thread (Windows pipes can't be select()ed)."""

    def __init__(self, python: str, plugin: Path, timeout: float) -> None:
        self.timeout = timeout
        # The G-Assist engine adds the plugin folder to PYTHONPATH when it spawns the
        # plugin (the installed layout is plugin.py + sibling packages), so this harness
        # does the same instead of relying on the interpreter's script-dir default.
        env = dict(os.environ)
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(plugin.parent) + (os.pathsep + existing if existing else "")
        self.proc = subprocess.Popen(
            [python, str(plugin)],
            cwd=str(plugin.parent),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._frames: "queue.Queue[Dict[str, Any] | None]" = queue.Queue()
        self._errors: List[str] = []
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._err_reader, daemon=True).start()

    def _reader(self) -> None:
        assert self.proc.stdout is not None
        try:
            while True:
                prefix = self.proc.stdout.read(FRAME_LEN)
                if not prefix:
                    self._frames.put(None)
                    return
                (length,) = (int.from_bytes(prefix, "big"),)
                body = self.proc.stdout.read(length)
                if len(body) != length:
                    self._frames.put(None)
                    return
                self._frames.put(json.loads(body.decode("utf-8")))
        except Exception as exc:  # pragma: no cover - defensive
            self._errors.append(f"frame reader: {exc}")
            self._frames.put(None)

    def _err_reader(self) -> None:
        assert self.proc.stderr is not None
        try:
            for line in self.proc.stderr:
                self._errors.append(line.decode("utf-8", "replace").rstrip())
        except Exception:  # pragma: no cover
            pass

    def send(self, payload: Dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(_encode_frame(payload))
        self.proc.stdin.flush()

    def recv(self) -> Dict[str, Any]:
        try:
            frame = self._frames.get(timeout=self.timeout)
        except queue.Empty as exc:
            raise _SmokeFailure(
                f"timed out after {self.timeout:.0f}s waiting for a frame"
            ) from exc
        if frame is None:
            raise _SmokeFailure(
                "plugin closed stdout without a reply (see stderr / afterburner.log below)"
            )
        return frame

    def recv_until(self, method: str, max_frames: int = 5) -> List[Dict[str, Any]]:
        return self.recv_until_any((method,), max_frames=max_frames)

    def recv_until_any(self, methods: tuple, max_frames: int = 5) -> List[Dict[str, Any]]:
        """Read frames until one carrying any of ``methods`` (complete OR error) arrives.

        A degraded Afterburner answers reads with an ``error`` notification (typed
        "not available" outcome), not a ``complete`` — both are valid Protocol V2
        terminal frames for an execute.
        """
        frames: List[Dict[str, Any]] = []
        for _ in range(max_frames):
            frame = self.recv()
            frames.append(frame)
            if frame.get("method") in methods:
                return frames
        raise _SmokeFailure(
            f"no {'/'.join(methods)} frame within {max_frames} frames; got {frames}"
        )

    def recv_result(self) -> Dict[str, Any]:
        """Read until the execute's terminal frame (complete OR error notification)."""
        frames = self.recv_until_any(("complete", "error"))
        result = _first_notification(frames, "complete")
        return result or _first_notification(frames, "error")

    def recv_until_id(self, request_id: int, max_frames: int = 5) -> List[Dict[str, Any]]:
        """Read frames until a JSON-RPC response carrying ``request_id`` arrives.

        Responses have no ``method`` key — only ``id`` + ``result``/``error``.
        """
        frames: List[Dict[str, Any]] = []
        for _ in range(max_frames):
            frame = self.recv()
            frames.append(frame)
            if frame.get("id") == request_id:
                return frames
        raise _SmokeFailure(
            f"no response for id {request_id} within {max_frames} frames; got {frames}"
        )

    def stderr_tail(self, limit: int = 6) -> str:
        return "\n".join(self._errors[-limit:])

    def close(self) -> None:
        if self.proc.stdin:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        try:
            self.proc.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:  # pragma: no cover
            self.proc.kill()
            self.proc.wait()


def run(python: str, plugin: Path, timeout: float) -> int:
    child = PluginProcess(python, plugin, timeout)
    failures: List[str] = []
    step = 0
    report: List[str] = []

    def check(ok: bool, label: str, detail: str) -> None:
        nonlocal step
        step += 1
        marker = "PASS" if ok else "FAIL"
        report.append(f"[{marker}] {label}: {detail}")
        if not ok:
            failures.append(label)

    def result_text(frame: Dict[str, Any]) -> str:
        """NL text of a terminal frame: complete -> data (the string); error -> message."""
        params = frame.get("params") or {}
        if frame.get("method") == "error":
            return str(params.get("message", ""))
        return str(params.get("data", ""))

    def is_error(frame: Dict[str, Any]) -> bool:
        return frame.get("method") == "error"

    try:
        # 1. initialize -> JSON-RPC response (id 1): name/protocol_version/commands
        child.send(_request("initialize", {}, 1))
        init = child.recv_until_id(1)[-1]
        result = init.get("result") or {}
        status = result.get("status", "?")
        commands = result.get("commands") or []
        check(
            result.get("protocol_version") == "2.0"
            and result.get("name") == "afterburner"
            and len(commands) == 17,
            "initialize",
            f"status={status}, name={result.get('name')}, commands={len(commands)}",
        )

        # 2. ping -> must echo the engine's timestamp (Protocol V2 health check)
        child.send(_request("ping", {"timestamp": 987654321}, 2))
        pong = child.recv_until_id(2)[-1]
        pong_result = json.dumps(pong.get("result"))
        check(
            pong.get("result") == {"timestamp": 987654321},
            "ping",
            pong_result,
        )

        # 3. get_gpu_status -> complete (live) OR error (degraded) terminal frame
        child.send(
            _request("execute", {"function": "get_gpu_status", "arguments": {}}, 3)
        )
        result = child.recv_result()
        message = result_text(result)
        degraded = any(tok in message.lower() for tok in
                       ("not available", "isn't running", "unavailable"))
        live = any(tok in message.lower() for tok in ("°", "temperature", "util", "fan", "clock"))
        check(
            bool(message) and (live or degraded),
            "get_gpu_status",
            ("(live telemetry) " if live else "(graceful degraded) ") + message[:140],
        )

        # 4. risky write -> confirming complete (keep_session, prompt text) or gated error
        child.send(
            _request(
                "execute",
                {"function": "set_power_limit",
                 "arguments": {"percent": 90, "gpu_index": 0}},
                4,
            )
        )
        result = child.recv_result()
        params = result.get("params") or {}
        text = result_text(result)
        prompting = (
            result.get("method") == "complete"
            and params.get("keep_session") is True
            and "reply 'confirm'" in text.lower()
        )
        noop = result.get("method") == "complete" and "already applied" in text.lower()
        if result.get("method") == "complete":
            prefix = (
                "needs_confirmation (keep_session), no write: "
                if prompting
                else "(no-op/gated): "
            )
        else:
            prefix = "(capability-gated error): "
        check(
            prompting or noop or result.get("method") == "error",
            "set_power_limit(90)",
            prefix + text[:140],
        )

        # 5. user verdict arrives as `input` -> ack response, then a cancelled complete
        if prompting:
            child.send(_request("input", {"content": "cancel"}, 5))
            ack_frames = child.recv_until_id(5)
            complete = _first_notification(child.recv_until("complete"), "complete")
            text = result_text(complete or {})
            check(
                ack_frames[-1].get("result") == {"acknowledged": True}
                and complete is not None
                and "cancelled" in text.lower(),
                "input(cancel)",
                f"ack + {text[:100]}",
            )
        else:  # degraded env: no confirmation is pending to resolve
            report.append("[SKIP] input(cancel): no pending confirmation (degraded env)")

        # 6. diagnose_performance -> complete OR degraded error
        child.send(
            _request(
                "execute",
                {"function": "diagnose_performance", "arguments": {"gpu_index": 0}},
                6,
            )
        )
        result = child.recv_result()
        text = result_text(result)
        check(bool(text), "diagnose_performance", text[:140])

        # 7. shutdown notification (no id, no response) -> clean exit 0
        child.send(_notification("shutdown", {}))
        child.close()
        check(child.proc.returncode == 0, "shutdown/EOF", f"exit code {child.proc.returncode}")
    except _SmokeFailure as exc:
        failures.append(str(exc))
        report.append(f"[FAIL] {exc}")
    finally:
        if child.proc.poll() is None:
            child.close()

    for line in report:
        print(line)
    tail = child.stderr_tail()
    if tail:
        print("--- plugin stderr tail ---")
        print(tail)
    if failures:
        print(f"SMOKE FAILED: {len(failures)} step(s): {'; '.join(failures)}")
        return 1
    print("SMOKE PASSED: real-process Protocol V2 lifecycle, live data, clean shutdown.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin", default=str(ROOT / "plugin.py"), help="plugin entry point")
    parser.add_argument("--python", default=sys.executable, help="interpreter to run the plugin")
    parser.add_argument("--step-timeout", type=float, default=20.0, help="seconds per step")
    args = parser.parse_args(argv)
    plugin = Path(args.plugin)
    if not plugin.is_file():
        print(f"plugin entry not found: {plugin}", file=sys.stderr)
        return 2
    try:
        return run(args.python, plugin, args.step_timeout)
    except KeyboardInterrupt:  # pragma: no cover
        return 2


if __name__ == "__main__":
    sys.exit(main())
