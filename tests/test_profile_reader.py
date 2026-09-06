"""Plan task 9.2 (reader half) — parse rules of the read-only profile-file reader
over the task 9.7 fixture directories."""
from __future__ import annotations

import shutil

import pytest

from afterburner.integration.profiles import (
    ProfileFileReader,
    ProfileSourceState,
)

from profiles_util import FIXTURES, fixture_dir, sandbox_copy

GPU_FILE = "VEN_10DE&DEV_2F04&SUBSYS_89E61043&REV_A1&BUS_11&DEV_0&FN_0.cfg"


class TestStartupEnabledVariant:
    def test_slots_and_values_match_captured_4_6_7(self) -> None:
        snapshot = ProfileFileReader(fixture_dir("startup_enabled")).read()
        assert snapshot.state is ProfileSourceState.AVAILABLE
        assert snapshot.present_slot_ids == (1, 2, 3)
        assert [s.marker_present for s in snapshot.slots] == [True, True, True]

        slot1 = snapshot.slots[0]
        assert slot1.slot == 1
        assert slot1.format_version == "2"
        assert slot1.values["PowerLimit"] == "100"
        assert slot1.values["CoreClkBoost"] == "95000"  # x1000 => +95 MHz
        assert slot1.values["MemClkBoost"] == "200000"
        assert slot1.values["FanMode"] == "1"
        assert slot1.values["FanSpeed"] == "31"
        assert slot1.values["CoreVoltageBoost"] == "0"
        # Per-slot memory offsets differ (200000 / 400000 / 600000).
        assert [s.values["MemClkBoost"] for s in snapshot.slots] == [
            "200000",
            "400000",
            "600000",
        ]

    def test_startup_is_populated_when_enabled(self) -> None:
        snapshot = ProfileFileReader(fixture_dir("startup_enabled")).read()
        assert snapshot.startup_present is True
        assert snapshot.startup_values["PowerLimit"] == "100"
        assert snapshot.startup_values["FanSpeed"] == "30"  # observed: 30 in [Startup]

    def test_real_sized_vfcurve_blob_is_parsed_as_one_value(self) -> None:
        snapshot = ProfileFileReader(fixture_dir("startup_enabled")).read()
        vf = snapshot.slots[0].values["VFCurve"]
        assert len(vf) > 6000  # the captured ~6.5 KB blob survives one tolerant parse

    def test_defaults_and_settings_are_never_loadable_slots(self) -> None:
        snapshot = ProfileFileReader(fixture_dir("startup_enabled")).read()
        ids = [s.slot for s in snapshot.slots]
        assert ids == [1, 2, 3]  # [Defaults] / [Settings] did not become slots


class TestStartupDisabledVariant:
    def test_empty_startup_means_disabled_not_absent(self) -> None:
        snapshot = ProfileFileReader(fixture_dir("startup_disabled")).read()
        assert snapshot.state is ProfileSourceState.AVAILABLE
        assert snapshot.startup_present is False  # present-but-empty = disabled
        assert snapshot.present_slot_ids == (1, 2, 3)  # slots unaffected by startup A/B

    def test_marker_files_are_corroboration_only(self) -> None:
        snapshot = ProfileFileReader(fixture_dir("startup_disabled")).read()
        assert sorted(snapshot.markers) == [1, 2, 3]
        # markers alone never create content: marker parse is skipped entirely


class TestEmptySlotsVariant:
    def test_bare_format_only_sections_are_empty(self) -> None:
        snapshot = ProfileFileReader(fixture_dir("empty_slots")).read()
        assert snapshot.state is ProfileSourceState.AVAILABLE
        # [Profile1..3] each contain only Format=2 => no present slots (empty list).
        assert snapshot.present_slot_ids == ()
        assert len(snapshot.slots) == 3
        assert all(s.values == {} for s in snapshot.slots)
        assert not snapshot.markers
        assert snapshot.startup_present is False


class TestAttribution:
    def test_no_per_gpu_file_means_no_profiles(self, tmp_path) -> None:
        target = tmp_path / "profiles"
        shutil.copytree(fixture_dir("empty_slots"), target)
        (target / GPU_FILE).unlink()  # only markers/global remain... plus remove markers
        snapshot = ProfileFileReader(target).read()
        assert snapshot.state is ProfileSourceState.NO_GPU_FILE

    def test_directory_without_any_profile_files(self, tmp_path) -> None:
        snapshot = ProfileFileReader(tmp_path).read()
        assert snapshot.state is ProfileSourceState.NO_GPU_FILE

    def test_nonexistent_directory(self, tmp_path) -> None:
        snapshot = ProfileFileReader(tmp_path / "does-not-exist").read()
        assert snapshot.state is ProfileSourceState.NO_DIRECTORY

    def test_multiple_per_gpu_files_without_id_are_ambiguous(self, tmp_path) -> None:
        target = sandbox_copy("startup_disabled", tmp_path)
        shutil.copyfile(
            target / GPU_FILE,
            target / "VEN_10DE&DEV_2F04&SUBSYS_89E61043&REV_A1&BUS_10&DEV_0&FN_0.cfg",
        )
        snapshot = ProfileFileReader(target).read()
        assert snapshot.state is ProfileSourceState.AMBIGUOUS_GPU_FILE

    def test_gpu_id_picks_the_matching_file(self, tmp_path) -> None:
        target = sandbox_copy("startup_disabled", tmp_path)
        second = target / "VEN_10DE&DEV_2F04&SUBSYS_89E61043&REV_A1&BUS_10&DEV_0&FN_0.cfg"
        shutil.copyfile(target / GPU_FILE, second)
        # Target the FIRST GPU by its exact MAHM szGpuId encoding.
        reader = ProfileFileReader(target, gpu_id=GPU_FILE[:-4])
        snapshot = reader.read()
        assert snapshot.state is ProfileSourceState.AVAILABLE
        assert snapshot.gpu_file is not None and snapshot.gpu_file.name == GPU_FILE

    def test_gpu_id_with_no_match_means_no_profiles(self, tmp_path) -> None:
        target = sandbox_copy("startup_disabled", tmp_path)
        reader = ProfileFileReader(target, gpu_id="VEN_1234&DEV_5678&SUBSYS_00000000"
                                                "&REV_00&BUS_0&DEV_0&FN_0")
        assert reader.read().state is ProfileSourceState.NO_GPU_FILE

    def test_gpu_id_tolerates_trailing_cfg(self) -> None:
        reader = ProfileFileReader(fixture_dir("startup_disabled"), gpu_id=GPU_FILE)
        assert reader.read().state is ProfileSourceState.AVAILABLE


class TestDefensiveParsing:
    def test_duplicate_keys_last_wins_and_duplicate_sections_merge(self, tmp_path) -> None:
        target = tmp_path / "p"
        target.mkdir()
        (target / GPU_FILE).write_text(
            "[Profile1]\nPowerLimit=100\nPowerLimit=118\n"
            "[Profile1]\nCoreClkBoost=95000\n",
            encoding="utf-8",
        )
        snapshot = ProfileFileReader(target).read()
        assert snapshot.present_slot_ids == (1,)
        slot = snapshot.slots[0]
        assert slot.values["PowerLimit"] == "118"
        assert slot.values["CoreClkBoost"] == "95000"

    def test_junk_lines_and_unexpected_sections_are_ignored(self, tmp_path) -> None:
        target = tmp_path / "p"
        target.mkdir()
        (target / GPU_FILE).write_text(
            "no-section-key=1\n"
            "[..\\..\\evil]\nPowerLimit=../../x\n"
            "[ProfileX]\nPowerLimit=1\n"
            "[Profile6]\nPowerLimit=1\n"
            "[Unknown]=weird\n"
            "garbage line without equals\n"
            "[Profile2]\n=novaluekey\nMemClkBoost=400000\n"
            "# comment\n",
            encoding="utf-8",
        )
        snapshot = ProfileFileReader(target).read()
        assert snapshot.present_slot_ids == (2,)
        assert snapshot.slots[0].values["MemClkBoost"] == "400000"

    def test_crlf_profile_file_parses_identically(self, tmp_path) -> None:
        target = tmp_path / "p"
        target.mkdir()
        (target / GPU_FILE).write_bytes(
            b"[Profile1]\r\nPowerLimit=100\r\nMemClkBoost=200000\r\n"
        )
        snapshot = ProfileFileReader(target).read()
        assert snapshot.present_slot_ids == (1,)

    def test_undecodable_file_is_unparseable_not_fabricated(self, tmp_path) -> None:
        target = tmp_path / "p"
        target.mkdir()
        (target / GPU_FILE).write_bytes(b"\xff\xfe\x00\x81garbage\xff")
        snapshot = ProfileFileReader(target).read()
        assert snapshot.state is ProfileSourceState.UNPARSEABLE

    def test_path_like_values_never_touch_the_filesystem(self, tmp_path) -> None:
        target = tmp_path / "p"
        target.mkdir()
        (target / GPU_FILE).write_text(
            "[Profile1]\nPowerLimit=..\\..\\..\\windows\\evil\n"
            "CoreClkBoost=C:\\temp\\x\nVFCurve=/etc/passwd\nMemClkBoost=200000\n",
            encoding="utf-8",
        )
        snapshot = ProfileFileReader(target).read()
        assert snapshot.present_slot_ids == (1,)
        # Values are inert strings; nothing outside the sandbox was touched.
        assert snapshot.slots[0].values["PowerLimit"].startswith("..")


class TestReadOnlyHandles:
    @pytest.mark.parametrize("variant", ["startup_enabled", "startup_disabled", "empty_slots"])
    def test_every_handle_is_read_only(self, variant) -> None:
        reader = ProfileFileReader(fixture_dir(variant))
        reader.read()
        assert reader.open_modes() != ()
        assert set(reader.open_modes()) == {"r"}  # never a write-capable handle

    def test_canonical_fixture_is_never_modified_by_a_read(self) -> None:
        from profiles_util import assert_trees_identical, tree_snapshot

        before = tree_snapshot(FIXTURES)
        for variant in ("startup_enabled", "startup_disabled", "empty_slots"):
            ProfileFileReader(fixture_dir(variant)).read()
        assert_trees_identical(before, tree_snapshot(FIXTURES))
