"""Plan task 9.2 — ProfileManager unit tests over fixture-backed real readers.

Inject FakeAfterburner for the control interface (mock boundary) and the real
read-only ProfileFileReader over sandboxed copies of the task 9.7 fixtures.
"""
from __future__ import annotations

import pytest

from afterburner.integration.fake import FakeAfterburner, default_capabilities
from afterburner.models import (
    ControlFeature,
    ErrorCode,
    GpuLimits,
    InterfaceStatus,
    PluginError,
    Range,
    TuningState,
)
from afterburner.services.profiles import describe_stored_settings

from profiles_util import (
    apply_recorder,
    assert_trees_identical,
    manager_for,
    sandbox_copy,
    slot_tuning,
    tree_snapshot,
)


def default_fake(*, tuning: TuningState | None = None) -> FakeAfterburner:
    fake = FakeAfterburner()
    fake.set_capabilities(default_capabilities())
    fake.set_tuning(tuning or TuningState(gpu_index=0))
    return fake


class TestListProfiles:
    def test_lists_slots_and_marks_the_matching_active_profile(self, tmp_path) -> None:
        fake = default_fake(tuning=slot_tuning())  # matches slot 1 exactly
        manager = manager_for(fake, sandbox_copy("startup_enabled", tmp_path))

        profiles = manager.get_profiles()
        assert [p.id for p in profiles] == [1, 2, 3]
        assert [p.name for p in profiles] == ["Profile 1", "Profile 2", "Profile 3"]
        assert [p.is_active for p in profiles] == [True, False, False]
        assert "power 100%" in profiles[0].summary
        assert "core +95 MHz" in profiles[0].summary
        assert "memory +200 MHz" in profiles[0].summary
        assert "fan 31% fixed" in profiles[0].summary
        assert "VF curve stored" in profiles[0].summary
        assert "memory +400 MHz" in profiles[1].summary
        assert "memory +600 MHz" in profiles[2].summary

    def test_other_slot_can_be_active(self, tmp_path) -> None:
        fake = default_fake(tuning=slot_tuning(memory_offset_mhz=600.0))  # slot 3
        manager = manager_for(fake, sandbox_copy("startup_enabled", tmp_path))
        profiles = manager.get_profiles()
        assert [p.is_active for p in profiles] == [False, False, True]

    def test_no_match_means_no_active_profile(self, tmp_path) -> None:
        # Applied values differ from every stored slot => none marked (never guessed).
        fake = default_fake(tuning=slot_tuning(power_limit_pct=70.0, memory_offset_mhz=300.0))
        manager = manager_for(fake, sandbox_copy("startup_enabled", tmp_path))
        profiles = manager.get_profiles()
        assert all(not p.is_active for p in profiles)

    def test_ambiguous_match_marks_none_active(self, tmp_path) -> None:
        # Tolerance 150 makes memory offset 300 match BOTH slot 1 (200) and slot 2 (400).
        fake = default_fake(tuning=slot_tuning(memory_offset_mhz=300.0))
        manager = manager_for(
            fake, sandbox_copy("startup_enabled", tmp_path), tolerance=150.0
        )
        profiles = manager.get_profiles()
        assert all(not p.is_active for p in profiles)

    def test_control_interface_down_lists_but_never_marks_active(self, tmp_path) -> None:
        fake = FakeAfterburner(status=InterfaceStatus.NOT_RUNNING)
        fake.set_tuning(slot_tuning())
        manager = manager_for(fake, sandbox_copy("startup_enabled", tmp_path))
        profiles = manager.get_profiles()
        assert [p.id for p in profiles] == [1, 2, 3]
        assert all(not p.is_active for p in profiles)

    def test_control_read_ambiguous_none_fields_mark_nothing_active(self, tmp_path) -> None:
        # Control state with no comparable field values => match is ambiguous.
        fake = default_fake(tuning=TuningState(gpu_index=0))
        manager = manager_for(fake, sandbox_copy("startup_enabled", tmp_path))
        assert all(not p.is_active for p in manager.get_profiles())

    def test_empty_directory_returns_empty_list(self, tmp_path) -> None:
        fake = default_fake()
        manager = manager_for(fake, sandbox_copy("empty_slots", tmp_path))
        assert manager.get_profiles() == []


class TestDescribeStoredSettings:
    def test_includes_thermal_limit_and_skips_empty_vf(self) -> None:
        text = describe_stored_settings(
            {
                "PowerLimit": "80",
                "ThermalLimit": "75",
                "CoreClkBoost": "-350000",
                "CoreVoltageBoost": "0",
            }
        )
        assert text == (
            "power 80%, core -350 MHz, voltage boost 0, thermal limit 75 C"
        )
        assert "VF curve" not in text

    def test_reports_vf_curve_without_dumping_hex(self) -> None:
        text = describe_stored_settings({"VFCurve": "010002007F000000deadbeef"})
        assert text == "VF curve stored"
        assert "deadbeef" not in text
        assert "01000200" not in text


class TestLoadProfile:
    def test_load_applies_stored_values_through_named_controls(self, tmp_path) -> None:
        fake = default_fake()
        apply_recorder(fake)
        manager = manager_for(fake, sandbox_copy("startup_enabled", tmp_path))

        result = manager.load_profile(1)
        assert result.applied is True
        assert result.feature is ControlFeature.PROFILE_LOAD
        assert "Profile 1 applied" in result.message

        applied = {(g, f, v) for (g, f, v) in fake.applied}
        assert (0, ControlFeature.POWER_LIMIT, 100.0) in applied
        assert (0, ControlFeature.CORE_OFFSET, 95.0) in applied  # 95000 / 1000
        assert (0, ControlFeature.MEMORY_OFFSET, 200.0) in applied
        assert (0, ControlFeature.FAN_PERCENT, 31.0) in applied
        # Read-back verified: applied state now equals the slot's stored values.
        state = fake.read_tuning_state(0)
        assert state.power_limit_pct == 100.0
        assert state.core_offset_mhz == 95.0
        assert state.memory_offset_mhz == 200.0
        assert state.fan_percent == 31.0

    def test_load_ignores_unmappable_settings_with_a_notice(self, tmp_path) -> None:
        fake = default_fake()
        apply_recorder(fake)
        manager = manager_for(fake, sandbox_copy("startup_enabled", tmp_path))
        result = manager.load_profile(2)
        # Populated VFCurve / CoreVoltageBoost have no named control => noticed.
        # (FanMode2 / FanSpeed2 are stored EMPTY in the captured file => not populated,
        # so they are not part of the slot and correctly produce no notice.)
        for key in ("VFCurve", "CoreVoltageBoost"):
            assert key in result.message
        assert "FanMode2 ignored" not in result.message

    def test_load_unknown_id_rejects_and_leaves_state_unchanged(self, tmp_path) -> None:
        fake = default_fake(tuning=slot_tuning())
        apply_recorder(fake)
        manager = manager_for(fake, sandbox_copy("startup_enabled", tmp_path))
        before = fake.read_tuning_state(0)

        for bad in (0, 4, 5, 6, 99, -1):
            with pytest.raises(PluginError) as excinfo:
                manager.load_profile(bad)
            assert excinfo.value.code is ErrorCode.INVALID_VALUE
        assert fake.applied == []  # nothing applied
        assert fake.read_tuning_state(0) == before  # active profile unchanged
        # Slot 4/5 exist in Afterburner but are not saved => invalid here too.

    def test_load_rejects_non_integer_ids(self, tmp_path) -> None:
        fake = default_fake()
        manager = manager_for(fake, sandbox_copy("startup_enabled", tmp_path))
        for bad in ("1", "..\\..\\x", None, 1.0, True, ["1"], {"id": 1}):
            with pytest.raises(PluginError) as excinfo:
                manager.load_profile(bad)
            assert excinfo.value.code is ErrorCode.INVALID_VALUE
        assert fake.applied == []

    def test_load_clamps_out_of_range_stored_values(self, tmp_path) -> None:
        target = sandbox_copy("startup_disabled", tmp_path)
        gpu_file = next(target.glob("VEN_*FN_0.cfg"))
        text = gpu_file.read_text(encoding="utf-8")
        # The first populated PowerLimit=100 belongs to [Profile1]; make it absurd so it
        # clamps to the reported 118% maximum.
        text = text.replace("PowerLimit=100", "PowerLimit=5000", 1)
        gpu_file.write_text(text, encoding="utf-8")

        fake = default_fake()
        apply_recorder(fake)
        manager = manager_for(fake, target)
        result = manager.load_profile(1)
        assert result.applied is True
        assert "clamped" in result.message
        assert (0, ControlFeature.POWER_LIMIT, 118.0) in {
            (g, f, v) for (g, f, v) in fake.applied
        }

    def test_load_unsupported_feature_is_skipped_with_notice(self, tmp_path) -> None:
        fake = default_fake()
        apply_recorder(fake)
        caps = default_capabilities(
            supported=(
                ControlFeature.POWER_LIMIT,
                ControlFeature.CORE_OFFSET,
                ControlFeature.FAN_PERCENT,
                ControlFeature.PROFILE_LOAD,
            ),  # memory offset deliberately not offered
            limits=GpuLimits(
                power_limit_pct=Range(50.0, 118.0),
                core_offset_mhz=Range(-300.0, 300.0),
                fan_percent=Range(0.0, 100.0),
            ),
        )
        fake.set_capabilities(caps)
        manager = manager_for(fake, sandbox_copy("startup_enabled", tmp_path))
        result = manager.load_profile(1)
        assert result.applied is True
        assert "MemClkBoost ignored" in result.message
        assert all(f is not ControlFeature.MEMORY_OFFSET for (_, f, _) in fake.applied)

    def test_load_non_numeric_value_is_skipped_with_notice(self, tmp_path) -> None:
        target = sandbox_copy("startup_disabled", tmp_path)
        gpu_file = next(target.glob("VEN_*FN_0.cfg"))
        text = gpu_file.read_text(encoding="utf-8")
        text = text.replace("PowerLimit=100", "PowerLimit=not-a-number", 1)
        gpu_file.write_text(text, encoding="utf-8")
        fake = default_fake()
        apply_recorder(fake)
        manager = manager_for(fake, target)
        result = manager.load_profile(1)
        assert result.applied is True
        assert "PowerLimit skipped" in result.message
        assert all(f is not ControlFeature.POWER_LIMIT for (_, f, _) in fake.applied)

    def test_load_control_interface_unavailable_raises_typed_error(self, tmp_path) -> None:
        fake = default_fake()
        caps = default_capabilities(
            supported=(ControlFeature.PROFILE_LOAD, ControlFeature.PROFILE_RESET),
            control_interface_status=InterfaceStatus.NOT_RUNNING,
        )
        fake.set_capabilities(caps)
        manager = manager_for(fake, sandbox_copy("startup_enabled", tmp_path))
        with pytest.raises(PluginError) as excinfo:
            manager.load_profile(1)
        assert excinfo.value.code is ErrorCode.INTERFACE_UNAVAILABLE
        assert fake.applied == []

    def test_load_success_only_after_readback_verification(self, tmp_path) -> None:
        # No apply_recorder: apply_control records but the applied state never changes,
        # so the read-back cannot confirm the change => honest failure, no false success.
        fake = default_fake(tuning=TuningState(gpu_index=0))  # static, never moves
        manager = manager_for(fake, sandbox_copy("startup_enabled", tmp_path))
        with pytest.raises(PluginError) as excinfo:
            manager.load_profile(1)
        assert excinfo.value.code is ErrorCode.COMM_FAILURE
        assert fake.applied != []  # the write was attempted but verification failed

    def test_load_profile_with_no_applicable_settings_is_best_effort_error(
        self, tmp_path
    ) -> None:
        from afterburner.integration.client import AfterburnerClient
        from afterburner.integration.profiles import ProfileFileReader
        from afterburner.services.profiles import ProfileManager

        target = tmp_path / "profiles"
        target.mkdir()
        (target / "VEN_10DE&DEV_2F04&SUBSYS_89E61043&REV_A1&BUS_11&DEV_0&FN_0.cfg").write_text(
            "[Profile1]\nFormat=2\nVFCurve=01000200\n", encoding="utf-8"
        )
        fake = default_fake()
        apply_recorder(fake)
        manager = ProfileManager(
            AfterburnerClient(interface=fake), ProfileFileReader(target)
        )
        assert manager.get_profiles() != []  # VFCurve-only slot is present...
        with pytest.raises(PluginError) as excinfo:
            manager.load_profile(1)
        assert excinfo.value.code is ErrorCode.INTERFACE_UNAVAILABLE
        assert fake.applied == []  # ...but nothing mappable exists to apply (10.8)


class TestResetProfile:
    def test_reset_reapplies_active_profile_stored_settings(self, tmp_path) -> None:
        fake = default_fake(tuning=slot_tuning())  # slot 1 active
        apply_recorder(fake)
        manager = manager_for(fake, sandbox_copy("startup_enabled", tmp_path))
        result = manager.reset_profile()
        assert result.applied is True
        assert result.feature is ControlFeature.PROFILE_RESET
        assert "Profile 1 restored" in result.message
        assert (0, ControlFeature.POWER_LIMIT, 100.0) in {
            (g, f, v) for (g, f, v) in fake.applied
        }

    def test_reset_with_no_active_profile_errors_cleanly(self, tmp_path) -> None:
        fake = default_fake(tuning=TuningState(gpu_index=0))  # no match
        apply_recorder(fake)
        manager = manager_for(fake, sandbox_copy("startup_enabled", tmp_path))
        with pytest.raises(PluginError) as excinfo:
            manager.reset_profile()
        assert excinfo.value.code is ErrorCode.INVALID_VALUE
        assert fake.applied == []

    def test_reset_control_unavailable_raises_typed_error(self, tmp_path) -> None:
        fake = default_fake(tuning=slot_tuning())
        caps = default_capabilities(
            supported=(ControlFeature.PROFILE_LOAD, ControlFeature.PROFILE_RESET),
            control_interface_status=InterfaceStatus.ACCESS_DENIED,
        )
        fake.set_capabilities(caps)
        manager = manager_for(fake, sandbox_copy("startup_enabled", tmp_path))
        with pytest.raises(PluginError) as excinfo:
            manager.reset_profile()
        assert excinfo.value.code is ErrorCode.INTERFACE_UNAVAILABLE


class TestReadOnlyEnforcement:
    def test_profile_operations_never_touch_the_directory(self, tmp_path) -> None:
        from afterburner.integration.client import AfterburnerClient
        from afterburner.integration.profiles import ProfileFileReader
        from afterburner.services.profiles import ProfileManager

        target = sandbox_copy("startup_enabled", tmp_path, suffix="ro")
        fake = default_fake(tuning=slot_tuning())
        apply_recorder(fake)
        reader = ProfileFileReader(target)
        manager = ProfileManager(AfterburnerClient(interface=fake), reader)

        before = tree_snapshot(target)
        manager.get_profiles()
        try:
            manager.load_profile(1)
        except PluginError:
            pass
        try:
            manager.reset_profile()
        except PluginError:
            pass
        try:
            manager.load_profile("..\\..\\evil")
        except PluginError:
            pass
        assert_trees_identical(before, tree_snapshot(target))
        # Every file handle the reader ever opened was strictly read-only.
        assert reader.open_modes() != ()
        assert set(reader.open_modes()) == {"r"}
