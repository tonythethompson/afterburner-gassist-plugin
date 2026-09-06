"""Plugin-local profile nicknames (never written to Afterburner's Profiles directory).

Afterburner hardware slots are numbered 1..5 with no stored names. This store keeps an
optional nickname per slot in the G-Assist plugin ``config.json`` so ``get_profiles`` can
show "quiet" / "gaming" and ``load_profile`` can resolve those labels.

The path is injected (official RISE plugin dir in production, a temp file in tests). Missing
or unreadable files are treated as no nicknames. Writes use a temp file + replace so a
partial JSON body cannot replace a good config.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, Mapping, Optional, Union

from ..models import ErrorCode, PluginError

SLOT_MIN = 1
SLOT_MAX = 5
NICKNAME_MAX_LEN = 40
_NICKNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 _'-]{0,39}$")
_RESERVED = frozenset({"profile", "slot", "p"})
_CONFIG_KEY = "profile_nicknames"


class ProfileNicknames:
    """Read/write the ``profile_nicknames`` map in plugin config.json."""

    def __init__(self, path: Optional[Union[str, Path]] = None) -> None:
        self.path = Path(path) if path is not None else None
        self._memory: Dict[int, str] = {}
        if self.path is not None:
            self._memory = self._load()

    def all(self) -> Mapping[int, str]:
        return dict(self._memory)

    def get(self, slot: int) -> Optional[str]:
        return self._memory.get(slot)

    def resolve(self, raw: str) -> Optional[int]:
        """Case-insensitive nickname -> slot, or None if unknown."""
        needle = " ".join(raw.strip().split()).casefold()
        if not needle:
            return None
        matches = [
            slot for slot, name in self._memory.items() if name.casefold() == needle
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def set_nickname(self, slot: int, nickname: str) -> str:
        """Set or clear a slot's nickname. Empty nickname clears. Returns NL text."""
        if not isinstance(slot, int) or isinstance(slot, bool) or not SLOT_MIN <= slot <= SLOT_MAX:
            raise PluginError(
                ErrorCode.INVALID_VALUE,
                f"There's no Afterburner profile {slot}.",
            )
        text = " ".join(str(nickname).split())
        if not text:
            if slot not in self._memory:
                return f"Profile {slot} has no nickname to clear."
            del self._memory[slot]
            self._save()
            return f"Cleared the nickname on Profile {slot}."
        normalized = _validate_nickname(text)
        owner = self.resolve(normalized)
        if owner is not None and owner != slot:
            raise PluginError(
                ErrorCode.INVALID_VALUE,
                f'That nickname is already used by Profile {owner}.',
            )
        self._memory[slot] = normalized
        self._save()
        return f'Profile {slot} is now nicknamed "{normalized}".'

    def _load(self) -> Dict[int, str]:
        assert self.path is not None
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError:
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if not isinstance(payload, dict):
            return {}
        stored = payload.get(_CONFIG_KEY, {})
        if not isinstance(stored, dict):
            return {}
        nicknames: Dict[int, str] = {}
        for key, value in stored.items():
            try:
                slot = int(key)
            except (TypeError, ValueError):
                continue
            if not SLOT_MIN <= slot <= SLOT_MAX or not isinstance(value, str):
                continue
            try:
                nicknames[slot] = _validate_nickname(value)
            except PluginError:
                continue
        return nicknames

    def _save(self) -> None:
        if self.path is None:
            return
        payload: Dict[str, object] = {}
        try:
            existing = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                payload = existing
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            payload = {}
        payload[_CONFIG_KEY] = {str(slot): name for slot, name in sorted(self._memory.items())}
        encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        parent = self.path.parent
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(encoded, encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise PluginError(
                ErrorCode.ACCESS_DENIED,
                "I couldn't save that nickname. The plugin config folder isn't writable.",
                detail=f"config write failed for {self.path}: {exc}",
            ) from exc


def _validate_nickname(text: str) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) > NICKNAME_MAX_LEN or not _NICKNAME_RE.match(cleaned):
        raise PluginError(
            ErrorCode.INVALID_VALUE,
            "Nicknames need to start with a letter and use letters, numbers, spaces, "
            "hyphens, or underscores (up to 40 characters).",
        )
    if cleaned.casefold() in _RESERVED:
        raise PluginError(
            ErrorCode.INVALID_VALUE,
            "That nickname is reserved. Pick a label like 'quiet' or 'gaming'.",
        )
    return cleaned
