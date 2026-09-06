Vendored SDK folder (placeholder)

`libs/gassist_sdk/` is where the official NVIDIA G-Assist plugin SDK is vendored at build/package
time (see task 22 in .kiro/specs/afterburner-gassist-plugin/tasks.md). It is deliberately NOT
committed to this repository (.gitignore) and is never installed from PyPI.

The `afterburner` package must not import gassist_sdk at module import time: the protocol
transport delegates to it when present and degrades to a stdlib framing fallback otherwise.
