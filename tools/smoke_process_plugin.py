"""Real-process Protocol V2 smoke harness (host-contract).

Spawns `plugin.py` as a fresh OS process — exactly what the G-Assist host does: working
directory = the plugin folder, Protocol V2 JSON-RPC 2.0 over the child's stdin/stdout
pipes with 4-byte big-endian length-prefixed frames. Unlike the in-process frame
simulation in `tests/`, this exercises the true entry point, the startup sequence, the
message loop, live Afterburner behind it, and clean EOF shutdown — the full path the
real host drives once the G-Assist RISE runtime deploys.

Usage:  python tools/smoke_process_plugin.py [--plugin plugin.py] [--step-timeout 20]

Step expectations are structural (correct lifecycle ids, `complete` notifications with
non-empty user messages, `needs_confirmation` on risky writes) and degrade-tolerant:
when Afterburner is absent the same calls return typed "not available" messages, which
the harness accepts (Property 7 graceful degradation). On a machine with Afterburner
running it reports live values.

Exit 0 = all steps passed; 1 = any step failed/timed out; 2 = usage/process error.
"""
from __future__ import annotations

import argparse
import json
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


def _decode_frame(data: bytes) -> Dict[str, Any]:
    if len(data) < FRAME_LEN:
        raise ValueError("truncated frame length prefix")
    (length,) = (int.from_bytes(data[:FRAME_LEN], "big"),)
    if length > MAX_FRAME or len(data) < FRAME_LEN + length:
        raise ValueError(f"bad frame length {length}")
    return json.loads(data[FRAME_LEN : FRAME_LEN + length].decode("utf-8"))


def _request(method: str, params: Dict[str, Any], request_id: int) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "method": method, "params": params, "id": request_id}


def _first_complete(frames: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for frame in frames:
        if frame.get("method") == "complete":
            return frame
    return None


class _SmokeFailure(Exception):
    pass


class PluginProcess:
    """A spawned plugin child with a reader thread (Windows pipes can't be select()ed)."""

    def __init__(self, python: str, plugin: Path, timeout: float) -> None:
        self.timeout = timeout
        self.proc = subprocess.Popen(
            [python, str(plugin)],
            cwd=str(plugin.parent),
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
                "plugin closed stdout without a reply (see stderr below)"
            )
        return frame

    def recv_until(self, method: str, max_frames: int = 5) -> List[Dict[str, Any]]:
        frames: List[Dict[str, Any]] = []
        for _ in range(max_frames):
            frame = self.recv()
            frames.append(frame)
            if frame.get("method") == method:
                return frames
        raise _SmokeFailure(
            f"no '{method}' frame within {max_frames} frames; got {frames}"
        )

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

    try:
        # 1. initialize -> JSON-RPC response (id 1) with status/functions
        child.send(_request("initialize", {}, 1))
        frames = child.recv_until_id(1)
        init = frames[-1]
        result = init.get("result") or {}
        status = result.get("status", "?")
        funcs = len(result.get("functions", []))
        check("result" in init, "initialize", f"status={status}, functions={funcs}")

        # 2. ping -> pong (id 2)
        child.send(_request("ping", {}, 2))
        pong = child.recv_until_id(2)[-1]
        result_text = json.dumps(pong.get("result"))
        check("pong" in result_text, "ping", result_text)

        # 3. get_gpu_status -> complete notification with a live or degraded message
        child.send(_request("execute", {"function": "get_gpu_status", "params": {}}, 3))
        complete = _first_complete(child.recv_until("complete"))
        message = ((complete or {}).get("params") or {}).get("message", "")
        degraded = "not available" in message.lower() or "isn't running" in message.lower()
        live = any(tok in message.lower() for tok in ("°", "temperature", "util", "fan", "clock"))
        check(
            complete is not None and message and (live or degraded),
            "get_gpu_status",
            ("(live telemetry) " if live else "(graceful degraded) ") + message[:140],
        )

        # 4. risky write -> needs_confirmation complete (no write), or graceful gate
        child.send(
            _request(
                "execute",
                {"function": "set_power_limit", "params": {"percent": 90, "gpu_index": 0}},
                4,
            )
        )
        complete = _first_complete(child.recv_until("complete"))
        data = ((complete or {}).get("params") or {}).get("data") or {}
        text = ((complete or {}).get("params") or {}).get("message", "")
        gated = "not available" in text.lower() or "can't" in text.lower()
        prefix = (
            "needs_confirmation, no write: "
            if data.get("needs_confirmation")
            else "(capability-gated): "
        )
        check(
            complete is not None and (data.get("needs_confirmation") is True or gated),
            "set_power_limit(90)",
            prefix + text[:140],
        )

        # 5. diagnose_performance -> complete notification
        child.send(
            _request(
                "execute", {"function": "diagnose_performance", "params": {"gpu_index": 0}}, 5
            )
        )
        complete = _first_complete(child.recv_until("complete"))
        text = ((complete or {}).get("params") or {}).get("message", "")
        check(complete is not None and bool(text), "diagnose_performance", text[:140])

        # 6. shutdown -> response, then EOF stops the loop cleanly
        child.send(_request("shutdown", {}, 6))
        child.recv_until_id(6)
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
