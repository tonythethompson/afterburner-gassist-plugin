"""plugin.py entry-point resilience: logging must never take the process down.

The RISE plugins directory under %PROGRAMDATA% is admin-owned, so a non-elevated plugin
process cannot create its log file there (the engine's own reference SDK falls back to
the temp dir for exactly this reason). ``_resolve_log_path``/``_setup_logging`` must
degrade instead of raising at startup — a startup crash is what surfaces to the engine as
a protocol failure (live finding: "Could not parse JSON-RPC message from afterburner
plugin").
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("win"),
    reason="plugin.py imports the Windows shared-memory clients (ctypes.wintypes)",
)


@pytest.fixture(scope="module")
def entry() -> object:
    spec = importlib.util.spec_from_file_location("ab_plugin_entry", str(ROOT / "plugin.py"))
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # annotations resolve against this module
    spec.loader.exec_module(module)
    return module


def test_resolve_log_path_rejects_an_unwritable_candidate(entry, tmp_path) -> None:
    # A path whose parent is a *file* can never be opened -> OSError every time.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    bad = str(blocker / "afterburner.log")
    assert entry._resolve_log_path([bad]) is None


def test_resolve_log_path_falls_back_to_tempdir(entry, tmp_path, monkeypatch) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(entry, "_LOG_PATH", str(blocker / "afterburner.log"))
    path = entry._resolve_log_path()  # default candidates: plugin dir, then temp dir
    assert path is not None
    assert Path(path).parent.is_dir()
    # The fallback is actually writable.
    with open(path, "a", encoding="utf-8"):
        pass


def test_setup_logging_never_raises_when_plugin_dir_unwritable(entry, tmp_path, monkeypatch) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(entry, "_LOG_PATH", str(blocker / "afterburner.log"))
    try:
        entry._setup_logging()  # must not raise (falls back to the temp dir)
    finally:
        # Leave the logging system clean for other tests.
        logging.getLogger("afterburner.plugin").handlers.clear()
        logging.getLogger("afterburner.plugin").propagate = True
