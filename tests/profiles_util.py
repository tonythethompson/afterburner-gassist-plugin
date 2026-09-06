"""Shared helpers for the profile fixture suites (tasks 9.2 / 9.3).

Fixture trees under ``tests/fixtures/profiles/`` are canonical and are never opened for
writing by tests: every sandbox test deep-copies a variant into a ``tmp_path`` first and
mutates only the copy.
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Dict, Optional

from afterburner.integration.client import AfterburnerClient
from afterburner.integration.fake import FakeAfterburner
from afterburner.integration.profiles import ProfileFileReader
from afterburner.models import ControlFeature, ControlResult, TuningState
from afterburner.services.profiles import ProfileManager

FIXTURES = Path(__file__).parent / "fixtures" / "profiles"

VARIANTS = ("startup_enabled", "startup_disabled", "empty_slots")


def fixture_dir(name: str) -> Path:
    path = FIXTURES / name
    if not path.is_dir():
        raise AssertionError(f"unknown fixture variant: {name!r}")
    return path


def sandbox_copy(name: str, tmp_path: Path, suffix: str = "") -> Path:
    """Deep-copy one fixture variant into a sandbox directory (mutations happen here)."""
    target = tmp_path / f"profiles{suffix}"
    shutil.copytree(fixture_dir(name), target)
    return target


def tree_snapshot(root: Path) -> Dict[str, dict]:
    """names + sizes + mtimes + content hashes of every entry under root (recursive)."""
    snapshot: Dict[str, dict] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_dir():
            snapshot[rel + "/"] = {"dir": True}
            continue
        data = path.read_bytes()
        stat = path.stat()
        snapshot[rel] = {
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "mtime_ns": stat.st_mtime_ns,
        }
    return snapshot


def assert_trees_identical(before: Dict[str, dict], after: Dict[str, dict]) -> None:
    """Byte-for-byte + metadata equality of two directory snapshots (with a useful diff)."""
    if before == after:
        return
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
    lines = []
    if added:
        lines.append("added: " + ", ".join(added))
    if removed:
        lines.append("removed: " + ", ".join(removed))
    for key in changed:
        lines.append(f"changed: {key}: {before[key]} -> {after[key]}")
    raise AssertionError("Profiles directory changed under the operation:\n" + "\n".join(lines))


def apply_recorder(fake: FakeAfterburner) -> None:
    """Make apply_control update the fake's applied tuning state (read-back verification).

    Mirrors the real adapter contract: after a successful write the control map's applied
    state reflects the written value, so a subsequent read_tuning_state sees it.
    """

    def _on_apply(gpu_index: int, feature: ControlFeature, value: float):
        state = fake.tuning_by_gpu.get(gpu_index) or TuningState(gpu_index=gpu_index)
        if feature is ControlFeature.POWER_LIMIT:
            state = _set(state, power_limit_pct=value)
        elif feature is ControlFeature.CORE_OFFSET:
            state = _set(state, core_offset_mhz=value)
        elif feature is ControlFeature.MEMORY_OFFSET:
            state = _set(state, memory_offset_mhz=value)
        elif feature is ControlFeature.FAN_PERCENT:
            state = _set(state, fan_percent=value, fan_mode="manual")
        fake.tuning_by_gpu[gpu_index] = state
        return ControlResult(
            feature=feature,
            requested_value=value,
            applied_value=value,
            applied=True,
            message=f"{feature.value} set to {value}.",
        )

    fake.on_apply_control = _on_apply


def _set(state: TuningState, **kw) -> TuningState:
    from dataclasses import replace

    return replace(state, **kw)


def manager_for(
    fake: FakeAfterburner,
    profiles_dir: Path,
    *,
    gpu_index: int = 0,
    tolerance: float = 1.0,
) -> ProfileManager:
    """Build ProfileManager over a fake control interface + a real reader on profiles_dir."""
    client = AfterburnerClient(interface=fake)
    reader = ProfileFileReader(profiles_dir)
    return ProfileManager(
        client, reader, gpu_index=gpu_index, tolerance=tolerance
    )


def slot_tuning(
    *,
    power_limit_pct: Optional[float] = 100.0,
    core_offset_mhz: Optional[float] = 95.0,
    memory_offset_mhz: Optional[float] = 200.0,
    fan_percent: Optional[float] = 31.0,
    gpu_index: int = 0,
) -> TuningState:
    """Applied state matching fixture slot 1's stored values (Profile 1 -> active)."""
    return TuningState(
        gpu_index=gpu_index,
        power_limit_pct=power_limit_pct,
        core_offset_mhz=core_offset_mhz,
        memory_offset_mhz=memory_offset_mhz,
        fan_mode="manual" if fan_percent is not None else "auto",
        fan_percent=fan_percent,
    )


def slot_tuning_profile2(*, gpu_index: int = 0) -> TuningState:
    """Applied state matching fixture slot 2 (MemClkBoost=400000 => +400 MHz)."""
    return slot_tuning(memory_offset_mhz=400.0, gpu_index=gpu_index)
