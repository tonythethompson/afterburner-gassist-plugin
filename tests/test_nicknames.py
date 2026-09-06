"""Plugin-local profile nicknames: persist in config.json, never Afterburner Profiles."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from afterburner.models import ErrorCode, PluginError
from afterburner.services.nicknames import ProfileNicknames


def test_memory_store_sets_and_resolves_case_insensitively() -> None:
    book = ProfileNicknames()
    assert "Quiet" in book.set_nickname(1, "  Quiet  ")
    assert book.get(1) == "Quiet"
    assert book.resolve("quiet") == 1
    assert book.resolve("QUIET") == 1
    assert book.resolve("missing") is None


def test_empty_nickname_clears() -> None:
    book = ProfileNicknames()
    book.set_nickname(2, "gaming")
    message = book.set_nickname(2, "  ")
    assert "Cleared" in message
    assert book.get(2) is None


def test_rejects_slot_like_and_path_nicknames() -> None:
    book = ProfileNicknames()
    for bad in ("1", "profile", "../quiet", "quiet/../x"):
        with pytest.raises(PluginError) as excinfo:
            book.set_nickname(1, bad)
        assert excinfo.value.code is ErrorCode.INVALID_VALUE


def test_duplicate_nickname_is_invalid() -> None:
    book = ProfileNicknames()
    book.set_nickname(1, "quiet")
    with pytest.raises(PluginError) as excinfo:
        book.set_nickname(2, "Quiet")
    assert excinfo.value.code is ErrorCode.INVALID_VALUE
    assert "already used" in excinfo.value.user_message


def test_persists_without_clobbering_other_config_keys(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"other": True}), encoding="utf-8")
    book = ProfileNicknames(path)
    book.set_nickname(3, "undervolt")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["other"] is True
    assert payload["profile_nicknames"] == {"3": "undervolt"}
    reloaded = ProfileNicknames(path)
    assert reloaded.get(3) == "undervolt"


def test_never_writes_outside_injected_config_path(tmp_path: Path) -> None:
    profiles = tmp_path / "Profiles"
    profiles.mkdir()
    marker = profiles / "Profile1.cfg"
    marker.write_text("[Settings]\nProfileContents=1\n", encoding="utf-8")
    before = marker.read_bytes()
    book = ProfileNicknames(tmp_path / "plugin" / "config.json")
    book.set_nickname(1, "quiet")
    assert marker.read_bytes() == before
    assert (tmp_path / "plugin" / "config.json").is_file()
    assert not (profiles / "config.json").exists()
