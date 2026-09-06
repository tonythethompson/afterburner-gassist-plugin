"""Plan task 14.2 — GAssistProtocol framing/transport tests (Requirement 1.1/1.2)."""
from __future__ import annotations

import io
import json
import struct

from afterburner.protocol import transport

HDR = struct.Struct(">I")


def frame(payload: object) -> bytes:
    raw = json.dumps(payload).encode("utf-8")
    return HDR.pack(len(raw)) + raw


def raw_frame(data: bytes) -> bytes:
    return HDR.pack(len(data)) + data


def parse_output(data: bytes) -> list:
    """Parse length-prefixed messages from captured writer bytes."""
    messages = []
    offset = 0
    while offset + 4 <= len(data):
        (length,) = HDR.unpack_from(data, offset)
        offset += 4
        messages.append(json.loads(data[offset:offset + length].decode("utf-8")))
        offset += length
    return messages


def make_protocol(handler=None, inbound: bytes = b"") -> tuple[transport.GAssistProtocol, io.BytesIO]:
    reader = io.BytesIO(inbound)
    writer = io.BytesIO()
    proto = transport.GAssistProtocol(reader=reader, writer=writer)
    if handler is not None:
        proto.handler = handler
    return proto, writer


class TestRoundTrip:
    def test_encode_decode_round_trip(self) -> None:
        requests = []

        def handler(method, params, request_id):
            requests.append((method, params, request_id))
            return [transport.response(request_id, {"echo": method})], False

        inbound = (
            frame({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})
            + frame({"jsonrpc": "2.0", "id": 2, "method": "execute", "params": {"x": 1}})
        )
        proto, writer = make_protocol(handler, inbound)
        assert proto.run() == 0  # clean EOF after both requests
        assert requests == [
            ("ping", {}, 1),
            ("execute", {"x": 1}, 2),
        ]
        messages = parse_output(writer.getvalue())
        assert len(messages) == 2
        assert messages[0] == {"jsonrpc": "2.0", "id": 1, "result": {"echo": "ping"}}

    def test_non_ascii_payload_round_trips(self) -> None:
        def handler(method, params, request_id):
            return [transport.response(request_id, {"text": "GPU 92°C — 日本語"})], False

        proto, writer = make_protocol(
            handler, frame({"jsonrpc": "2.0", "id": 7, "method": "m", "params": {}})
        )
        proto.run()
        assert parse_output(writer.getvalue())[0]["result"]["text"] == "GPU 92°C — 日本語"

    def test_notifications_are_framed_too(self) -> None:
        def handler(method, params, request_id):
            return [transport.notification("complete", {"success": True})], False

        proto, writer = make_protocol(
            handler, frame({"jsonrpc": "2.0", "id": 1, "method": "m", "params": {}})
        )
        proto.run()
        messages = parse_output(writer.getvalue())
        assert messages[0] == {
            "jsonrpc": "2.0",
            "method": "complete",
            "params": {"success": True},
        }


class TestMalformedFrames:
    def test_oversized_length_is_rejected_without_terminating(self) -> None:
        # 10 MB + 1 header; the payload is never sent/read.
        inbound = HDR.pack(transport.MAX_FRAME_BYTES + 1) + b""
        calls = []

        def handler(method, params, request_id):
            calls.append(method)
            return [transport.response(request_id, {})], False

        proto, writer = make_protocol(handler, inbound)
        assert proto.run() == 0
        assert calls == []  # never dispatched
        assert proto.errors_handled == 1
        message = parse_output(writer.getvalue())[0]
        assert message["id"] is None
        assert message["error"]["code"] == -32700

    def test_invalid_utf8_payload_is_parse_error(self) -> None:
        proto, writer = make_protocol(handler=lambda m, p, i: ([], False),
                                      inbound=raw_frame(b"\xff\xfe\x00\x81garbage"))
        proto.run()
        assert parse_output(writer.getvalue())[0]["error"]["code"] == -32700

    def test_invalid_json_payload_is_parse_error(self) -> None:
        proto, writer = make_protocol(handler=lambda m, p, i: ([], False),
                                      inbound=raw_frame(b"{not json"))
        proto.run()
        assert parse_output(writer.getvalue())[0]["error"]["code"] == -32700

    def test_missing_jsonrpc_version_is_invalid_request(self) -> None:
        proto, writer = make_protocol(
            handler=lambda m, p, i: ([], False),
            inbound=frame({"id": 1, "method": "ping"}),
        )
        proto.run()
        message = parse_output(writer.getvalue())[0]
        assert message["error"]["code"] == -32600

    def test_missing_method_is_invalid_request(self) -> None:
        proto, writer = make_protocol(
            handler=lambda m, p, i: ([], False),
            inbound=frame({"jsonrpc": "2.0", "id": 1}),
        )
        proto.run()
        assert parse_output(writer.getvalue())[0]["error"]["code"] == -32600

    def test_non_object_params_is_invalid_request(self) -> None:
        proto, writer = make_protocol(
            handler=lambda m, p, i: ([], False),
            inbound=frame({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": []}),
        )
        proto.run()
        assert parse_output(writer.getvalue())[0]["error"]["code"] == -32600

    def test_loop_continues_after_malformed_frames(self) -> None:
        def handler(method, params, request_id):
            return [transport.response(request_id, {"ok": method})], False

        inbound = (
            raw_frame(b"not json at all")
            + frame({"jsonrpc": "2.0", "id": 9, "method": "execute", "params": {}})
        )
        proto, writer = make_protocol(handler, inbound)
        proto.run()
        messages = parse_output(writer.getvalue())
        assert messages[0]["error"]["code"] == -32700
        assert messages[1]["id"] == 9 and messages[1]["result"] == {"ok": "execute"}
        assert proto.requests_seen == 1


class TestStopAndErrors:
    def test_handler_can_stop_the_loop(self) -> None:
        def handler(method, params, request_id):
            return [transport.response(request_id, {"bye": True})], True

        proto, writer = make_protocol(
            handler,
            frame({"jsonrpc": "2.0", "id": 1, "method": "shutdown", "params": {}}),
        )
        assert proto.run() == 0
        assert parse_output(writer.getvalue())[0]["id"] == 1

    def test_handler_errors_never_kill_the_loop(self) -> None:
        def bad_handler(method, params, request_id):
            raise RuntimeError("boom")

        inbound = (
            frame({"jsonrpc": "2.0", "id": 1, "method": "m", "params": {}})
            + frame({"jsonrpc": "2.0", "id": 2, "method": "m2", "params": {}})
        )
        proto, writer = make_protocol(bad_handler, inbound)
        proto.run()
        messages = parse_output(writer.getvalue())
        assert len(messages) == 2
        assert messages[0]["id"] == 1 and messages[0]["error"]["code"] == -1
        assert proto.errors_handled == 2
