Vendored SDK folder (placeholder)

`libs/gassist_sdk/` is where the official NVIDIA G-Assist plugin SDK would be vendored at
build/package time (see task 22 in .kiro/specs/afterburner-gassist-plugin/tasks.md). It is
deliberately NOT committed to this repository (.gitignore) and is never installed from PyPI.

Verified against the real G-Assist RISE runtime (System Assistant, nvtopps\rise): the engine
spawns plugins with an **isolated embedded python** (`python313._pth` — PYTHONPATH is ignored
and the script's folder is not added to sys.path), and NVIDIA's own reference plugins carry the
SDK under their own `libs/` and self-insert that path. `plugin.py` mirrors that bootstrap by
adding its own folder + `libs/` to sys.path before importing the `afterburner` package.

Note: the `afterburner` package never imports gassist_sdk at module import time. The protocol
transport in `afterburner/protocol/transport.py` is a self-contained stdlib implementation of
the Protocol V2 wire framing (4-byte big-endian length + UTF-8 JSON-RPC 2.0 — byte-identical to
the SDK's `protocol.py`), so a vendored SDK is optional. What matters is the MESSAGE CONTRACT:
live testing against the real engine ("Could not parse JSON-RPC message from afterburner
plugin") proved the SDK's exact envelope shapes are required — complete notifications carry
`{request_id, success, data, keep_session}` where **`data` is the NL text string itself**
(Protocol V2 has NO structured output channel), failures are an `error` notification, ping
echoes `timestamp`, `execute` arguments arrive under `arguments`, `input` is acknowledged
before acting, and `shutdown` is answered with no frame. That contract is verified against
NVIDIA's PROTOCOL_V2.md + migration guide, the `gassist_sdk`, and the official
`plugin_emulator` (which crashes on dict `data` with "can only concatenate str (not \"dict\")
to str" — the exact failure the live engine surfaced as a parse error; dict payloads were
finally ruled out 2026-09-06). `afterburner/protocol/plugin.py` emits exactly these shapes
(confirmation tokens are internal to the plugin and never travel on the wire).
