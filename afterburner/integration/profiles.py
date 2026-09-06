"""Read-only Afterburner Profiles-directory reader (Requirement 14.3 carve-out).

One of the three OS-touching integration modules (alongside the MAHM monitoring and MACM
control clients). It implements the profile-storage layout from design § "Afterburner
Profile Storage Layout (read-only access)" against a *real* ``Profiles\\`` directory, so the
ProfileManager service and the Property 9 sandbox tests exercise genuine Afterburner-shaped
content without an Afterburner install.

**Never writes.** No file or directory under the Profiles directory is created, modified, or
deleted here; every file is opened read-only (the open mode is recorded per handle via
`open_modes()` so tests can prove no write-capable handle is ever requested), the directory
is only listed (`iterdir`), and no path is ever derived from caller- or content-supplied
strings — the directory is fixed at construction and files are matched by pattern against
already-listed entries.

Parsing rules (from the design / plan task 9.1):

- Only the per-GPU ``VEN_…&FN_<n>.cfg`` file whose instance id matches the target GPU's MAHM
  ``szGpuId`` encoding is parsed. One per-GPU file and no explicit id ⇒ that file; several and
  no id ⇒ *ambiguous* (profiles unavailable, never guessed/merged); no matching file ⇒ *no
  profiles* (empty list).
- A hardware slot 1..5 is **present** only when its ``[ProfileN]`` section holds at least one
  populated (non-empty) setting key. Empty slots in either observed form — absent section or a
  bare ``Format=2``-only section — are empty. ``ProfileN.cfg`` marker files are corroboration
  only and are never parsed as profile content.
- ``[Startup]`` is auto-apply *corroboration* only: populated keys ⇒ enabled, present-but-empty
  ⇒ disabled. ``[Defaults]`` / ``[Settings]`` / any unexpected section are never loadable slots.
- Parsing is defensive: junk lines are skipped, duplicate keys are last-wins, duplicate
  sections merge, and an attributed file that cannot be decoded surfaces state
  ``UNPARSEABLE`` (a typed unavailable result) — never a fabricated profile list.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple, Union

from ..models import PluginError, ErrorCode

SLOT_MIN = 1
SLOT_MAX = 5

# The per-GPU tuning file name == the MAHM GPU-entry szGpuId encoding (documented in the
# shipped MAHMSharedMemory.h): VEN_%04X&DEV_%04X&SUBSYS_%08X&REV_%02X&BUS_%d&DEV_%d&FN_%d.
_VEN_FILE_RE = re.compile(
    r"^VEN_[0-9A-Fa-f]{4}&DEV_[0-9A-Fa-f]{4}&SUBSYS_[0-9A-Fa-f]{8}"
    r"&REV_[0-9A-Fa-f]{2}&BUS_[0-9]+&DEV_[0-9]+&FN_[0-9]+\.cfg$"
)
_MARKER_FILE_RE = re.compile(r"^Profile([1-5])\.cfg$")
_SLOT_SECTION_RE = re.compile(r"^Profile([1-5])$")

# Keys that never represent loadable content and are excluded from "populated" checks
# (bare `Format=2`-only sections are an observed *empty* form).
_FORMAT_KEY = "Format"


class ProfileSourceState(str, Enum):
    AVAILABLE = "available"
    NO_GPU_FILE = "no_gpu_file"  # directory has no per-GPU file => no profiles (empty list)
    AMBIGUOUS_GPU_FILE = "ambiguous_gpu_file"  # several per-GPU files, no id match given
    NO_DIRECTORY = "no_directory"  # Profiles directory absent at the detected location
    UNPARSEABLE = "unparseable"  # attributed file cannot be decoded => unavailable


@dataclass(frozen=True)
class ProfileSlotInfo:
    """One parsed ``[ProfileN]`` section."""

    slot: int
    # Populated (non-empty) setting keys as stored, in file order; `Format` excluded.
    values: Mapping[str, str]
    format_version: Optional[str] = None  # raw `Format=` value, validation only
    marker_present: bool = False  # ProfileN.cfg marker exists (corroboration only)

    @property
    def present(self) -> bool:
        """A slot is present iff it holds at least one populated setting key."""
        return bool(self.values)


@dataclass(frozen=True)
class ProfilesSnapshot:
    """Result of one read-only scan of a Profiles directory."""

    state: ProfileSourceState
    source_dir: Path
    gpu_file: Optional[Path] = None  # the attributed per-GPU file (when AVAILABLE)
    slots: Tuple[ProfileSlotInfo, ...] = ()
    startup_values: Mapping[str, str] = field(default_factory=dict)
    startup_present: bool = False  # populated [Startup] => auto-apply corroboration present
    markers: Mapping[int, bool] = field(default_factory=dict)  # slot -> marker file exists
    notices: Tuple[str, ...] = ()

    @property
    def present_slot_ids(self) -> Tuple[int, ...]:
        return tuple(s.slot for s in self.slots if s.present)


class ProfileFileReader:
    """Read-only parser for one Afterburner Profiles directory (path fixed at construction)."""

    def __init__(
        self,
        profiles_dir: Union[str, "os.PathLike[str]"],
        gpu_id: Optional[str] = None,
    ) -> None:
        import os as _os

        self.dir = Path(profiles_dir)
        # MAHM GPU-entry szGpuId ("VEN_...&FN_0") of the target GPU; None = attribute the
        # single per-GPU file when unambiguous.
        gpu_id = _os.fspath(gpu_id) if gpu_id is not None else None
        if gpu_id is not None and gpu_id.lower().endswith(".cfg"):
            gpu_id = gpu_id[:-4]
        self.gpu_id = gpu_id
        # Every file open records its mode here; tests assert all handles are read-only.
        self._open_modes: List[str] = []

    # ------------------------------------------------------------------ public
    def open_modes(self) -> Tuple[str, ...]:
        """Modes of every file handle opened so far (all read-only by construction)."""
        return tuple(self._open_modes)

    def read(self) -> ProfilesSnapshot:
        """Scan the directory and parse the attributed per-GPU file (read-only)."""
        if not self.dir.is_dir():
            return ProfilesSnapshot(
                state=ProfileSourceState.NO_DIRECTORY,
                source_dir=self.dir,
                notices=("Profiles directory not found.",),
            )

        try:
            cfg_files = sorted(
                e for e in self.dir.iterdir() if e.is_file() and e.suffix.lower() == ".cfg"
            )
        except OSError as exc:
            return ProfilesSnapshot(
                state=ProfileSourceState.UNPARSEABLE,
                source_dir=self.dir,
                notices=(f"Could not list Profiles directory: {exc}",),
            )

        gpu_files = [f for f in cfg_files if _VEN_FILE_RE.match(f.name)]
        markers: Dict[int, bool] = {}
        for f in cfg_files:
            m = _MARKER_FILE_RE.match(f.name)
            if m is not None:
                markers[int(m.group(1))] = True

        gpu_file = self._attribute(gpu_files)
        if gpu_file is None:
            state = (
                ProfileSourceState.AMBIGUOUS_GPU_FILE
                if len(gpu_files) > 1
                else ProfileSourceState.NO_GPU_FILE
            )
            return ProfilesSnapshot(
                state=state,
                source_dir=self.dir,
                markers=markers,
            )

        text: Optional[str] = None
        try:
            text = self._read_text(gpu_file)
        except PluginError as exc:
            return ProfilesSnapshot(
                state=ProfileSourceState.UNPARSEABLE,
                source_dir=self.dir,
                gpu_file=gpu_file,
                markers=markers,
                notices=(exc.detail or exc.user_message,),
            )

        sections = _parse_ini(text)
        slots = _extract_slots(sections, markers)
        startup_values, startup_present = _extract_startup(sections)

        return ProfilesSnapshot(
            state=ProfileSourceState.AVAILABLE,
            source_dir=self.dir,
            gpu_file=gpu_file,
            slots=tuple(slots),
            startup_values=startup_values,
            startup_present=startup_present,
            markers=markers,
        )

    # ------------------------------------------------------------------ internals
    def _attribute(self, gpu_files: List[Path]) -> Optional[Path]:
        """Pick the per-GPU file for the target GPU id, or None (no file / ambiguous)."""
        if not gpu_files:
            return None
        if self.gpu_id is not None:
            for f in gpu_files:
                if f.stem == self.gpu_id:
                    return f
            return None  # no matching per-GPU file => no profiles for this GPU
        if len(gpu_files) > 1:
            return None  # cannot attribute unambiguously => unavailable, never guessed
        return gpu_files[0]

    def _read_text(self, path: Path) -> str:
        """Read one profile file with a strictly-read handle (never a write mode)."""
        self._open_modes.append("r")
        try:
            with io.open(str(path), "r", encoding="utf-8-sig", errors="strict") as handle:
                return handle.read()
        except (OSError, UnicodeError, ValueError) as exc:
            raise PluginError(
                ErrorCode.INTERFACE_UNAVAILABLE,
                "Afterburner's profile data couldn't be read.",
                detail=f"unreadable/undecodable {path.name}: {exc}",
            ) from exc


# --------------------------------------------------------------------------- parse
def _parse_ini(text: str) -> Dict[str, Dict[str, str]]:
    """Tolerant INI parse: `[Section]` headers + `key=value` lines.

    Order preserving; junk lines skipped; duplicate keys last-wins; duplicate sections
    merge. Never raises on content — malformed content simply contributes nothing.
    """
    sections: Dict[str, Dict[str, str]] = {}
    current: Optional[str] = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in (";", "#", "/"):
            continue
        if line.startswith("["):
            end = line.find("]")
            current = line[1:end].strip() if end != -1 else None
            if current is not None:
                sections.setdefault(current, {})
            continue
        if "=" not in line or current is None:
            continue  # key outside a section or no '=' -> junk, skip
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        sections[current][key] = value.strip()
    return sections


def _extract_startup(
    sections: Mapping[str, Mapping[str, str]],
) -> Tuple[Mapping[str, str], bool]:
    startup = dict(sections.get("Startup", {}))
    populated = {k: v for k, v in startup.items() if k != _FORMAT_KEY and v != ""}
    return populated, bool(populated)


def _extract_slots(
    sections: Mapping[str, Mapping[str, str]],
    markers: Mapping[int, bool],
) -> List[ProfileSlotInfo]:
    slots: List[ProfileSlotInfo] = []
    by_number: Dict[int, Dict[str, str]] = {}
    format_version: Dict[int, Optional[str]] = {}
    for name, body in sections.items():
        m = _SLOT_SECTION_RE.match(name)
        if m is None:
            continue
        number = int(m.group(1))
        by_number.setdefault(number, {})
        format_version.setdefault(number, body.get(_FORMAT_KEY))
        for key, value in body.items():
            if key == _FORMAT_KEY:
                continue
            if value != "":
                by_number[number][key] = value
    for number in sorted(by_number):
        slots.append(
            ProfileSlotInfo(
                slot=number,
                values=by_number[number],
                format_version=format_version[number],
                marker_present=markers.get(number, False),
            )
        )
    return slots
