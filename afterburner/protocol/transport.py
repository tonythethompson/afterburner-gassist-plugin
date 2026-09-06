"""GAssistProtocol — Protocol V2 transport (design component table; Requirement 1.1/1.2).

JSON-RPC 2.0 messages encoded as UTF-8 JSON, each prefixed with a 4-byte big-endian
unsigned length header, exchanged over stdin/stdout (injectable streams for tests).

Guarantees:
- An incoming header over ``MAX_FRAME_BYTES`` (10 MB) is discarded and answered with a
  JSON-RPC parse error (id null) WITHOUT terminating the loop (Req 1.2).
- A payload that is not valid UTF-8 or not valid JSON-RPC 2.0 yields a parse /
  invalid-request error and the loop continues (Req 1.2).
- Unknown *protocol* methods surface as -32601 method-not-found responses and the loop
  continues (state retained — the plugin decides what is registered).

The official ``gassist_sdk`` (vendored into ``libs/``) is used when present; this module is
the stdlib framing fallback it would otherwise provide (see libs/README.txt) and is what the
unit tests exercise.
"""

from __future__ import annotations

import io
import json
import struct
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

# JSON-RPC error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602

MAX_FRAME_BYTES = 10 * 1024 * 1024  # 10,485,760 (Req 1.2)
_HEADER = struct.Struct(">I")


class ProtocolError(Exception):
    """Raised for a malformed inbound frame; carries the JSON-RPC error code."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- wire shapes
def response(id: Any, result: Any) -> Dict[str, Any]:
    return {"id": id, "result": result}


def error(id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"id": id, "error": {"code": code, "message": message}}


def notification(method: str, params: Any) -> Dict[str, Any]:
    return {"method": method, "params": params}


def parse_error(message: str = "Parse error") -> Dict[str, Any]:
    return error(None, PARSE_ERROR, message)


def invalid_request(message: str = "Invalid Request") -> Dict[str, Any]:
    return error(None, INVALID_REQUEST, message)


# --------------------------------------------------------------------------- transport
RequestHandler = Callable[[str, Dict[str, Any], Any], Tuple[List[Dict[str, Any]], bool]]


class GAssistProtocol:
    """Length-prefixed JSON-RPC 2.0 loop over two binary streams."""

    def __init__(
        self,
        reader: Optional[io.BufferedIOBase] = None,
        writer: Optional[io.BufferedIOBase] = None,
        trace: Optional[Callable[[str, Any], None]] = None,
    ) -> None:
        """``trace(side, payload)`` receives every inbound request / outbound message
        (plus "recv_error" entries) — the live wire transcript for engine debugging."""
        self.reader = reader if reader is not None else sys.stdin.buffer
        self.writer = writer if writer is not None else sys.stdout.buffer
        self.handler: Optional[RequestHandler] = None
        self.trace = trace
        self.requests_seen = 0
        self.errors_handled = 0

    # ------------------------------------------------------------------ framing
    def _read_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self.reader.read(n - len(buf))
            if not chunk:
                raise EOFError("stream closed")
            buf.extend(chunk)
        return bytes(buf)

    def read_frame(self) -> Tuple[Dict[str, Any], Any]:
        """Read + decode one inbound JSON-RPC request -> (request dict, request id).

        Raises ProtocolError with the appropriate JSON-RPC code on oversized / malformed
        frames (callers respond and continue). EOFError means the stream closed cleanly.
        """
        header = self._read_exact(4)
        (length,) = _HEADER.unpack(header)
        if length > MAX_FRAME_BYTES:
            raise ProtocolError(
                PARSE_ERROR,
                f"Frame of {length} bytes exceeds the 10 MB limit",
            )
        raw = self._read_exact(length)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError(PARSE_ERROR, "Payload is not valid UTF-8") from exc
        try:
            obj = json.loads(text)
        except (ValueError, TypeError) as exc:
            raise ProtocolError(PARSE_ERROR, "Payload is not valid JSON") from exc
        return self._validate_request(obj)

    def _validate_request(self, obj: Any) -> Tuple[Dict[str, Any], Any]:
        if not isinstance(obj, dict):
            raise ProtocolError(INVALID_REQUEST, "Request must be a JSON object")
        if obj.get("jsonrpc") != "2.0":
            raise ProtocolError(INVALID_REQUEST, "Missing or invalid jsonrpc version")
        method = obj.get("method")
        if not isinstance(method, str) or not method:
            raise ProtocolError(INVALID_REQUEST, "Missing method")
        params = obj.get("params", {})
        if not isinstance(params, dict):
            raise ProtocolError(INVALID_REQUEST, "params must be an object")
        request_id = obj.get("id")
        return {"method": method, "params": params, "id": request_id}, request_id

    # ------------------------------------------------------------------ writing
    def write_message(self, message: Dict[str, Any]) -> None:
        """Encode one wire message (response or notification) with a length prefix."""
        if "error" in message:
            body = {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "error": message["error"],
            }
        elif "result" in message:
            body = {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "result": message["result"],
            }
        elif "method" in message:
            body = {"jsonrpc": "2.0", "method": message["method"],
                    "params": message.get("params", {})}
        else:  # pragma: no cover - internal misuse guard
            raise ValueError(f"unrecognized wire message: {message!r}")
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.writer.write(_HEADER.pack(len(raw)))
        self.writer.write(raw)
        self.writer.flush()

    # ------------------------------------------------------------------ loop
    def run(self) -> int:
        """Message loop. Returns 0 (clean shutdown/EOF). Never exits on bad frames."""
        if self.handler is None:
            raise RuntimeError("no request handler registered")
        while True:
            try:
                request, request_id = self.read_frame()
            except EOFError:
                return 0
            except ProtocolError as exc:
                self.errors_handled += 1
                if self.trace is not None:
                    self.trace("recv_error", {"code": exc.code, "message": exc.message})
                self.write_message(error(None, exc.code, exc.message))
                continue
            except OSError:
                return 0

            self.requests_seen += 1
            method = request["method"]
            params = request["params"]
            if self.trace is not None:
                self.trace("recv", request)
            try:
                messages, stop = self.handler(method, params, request_id)
            except Exception as exc:  # pragma: no cover - handler must never blow the loop
                self.errors_handled += 1
                messages = [error(request_id, -1, f"handler error: {exc}")]
                stop = False
            for message in messages:
                if self.trace is not None:
                    self.trace("send", message)
                self.write_message(message)
            if stop:
                return 0
