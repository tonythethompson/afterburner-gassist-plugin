"""Plan tasks 9.4/9.5/9.6 — TuningOwnershipService reporting + Property 10 honesty.

The ownership report is built from Afterburner-observable state only (applied tuning read
back through the control interface, the active profile matched against it, and populated
[Startup] corroboration). External authorities are always UNKNOWN_NOT_OBSERVABLE.
"""
from __future__ import annotations

from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from afterburner.integration.client import AfterburnerClient
from afterburner.integration.fake import FakeAfterburner, default_capabilities
from afterburner.integration.profiles import ProfileFileReader
from afterburner.models import (
    AuthorityState,
    ControlFeature,
    GpuTelemetry,
    InterfaceStatus,
    TuningState,
)
from afterburner.protocol.plugin import GAssistPlugin, build_services
from afterburner.services.ownership import TuningOwnershipService
from afterburner.services.profiles import ProfileManager

from profiles_util import fixture_dir, slot_tuning

ENABLED = Path("tests/fixtures/profiles/startup_enabled")
DISABLED = Path("tests/fixtures/profiles/startup_disabled")


def make_service(
    *,
    status: InterfaceStatus = InterfaceStatus.OK,
    tuning: TuningState | None = None,
    profiles_dir: Path = ENABLED,
) -> tuple[FakeAfterburner, TuningOwnershipService]:
    fake = FakeAfterburner(status=status)
    fake.set_capabilities(default_capabilities())
    fake.set_telemetry(
        GpuTelemetry(gpu_index=0, gpu_name="Fake", temperature_c=60.0,
                     utilization_pct=90.0, core_clock_mhz=1500.0)
    )
    fake.set_tuning(tuning or TuningState(gpu_index=0))
    client = AfterburnerClient(interface=fake)
    profiles = ProfileManager(client, ProfileFileReader(profiles_dir))
    service = TuningOwnershipService(client, profiles)
    return fake, service


class TestOwnershipReport:
    def test_afterburner_observable_state_is_reported(self) -> None:
        tuning = slot_tuning()
        fake, service = make_service(tuning=tuning)
        report = service.build_report(0)

        assert report.afterburner.interface_status is InterfaceStatus.OK
        assert report.afterburner.applied_state == tuning
        assert report.afterburner.active_profile_id == 1  # slot 1 stored match
        assert report.afterburner.startup_auto_apply_present is True
        assert "Profile 1" in report.summary

    def test_startup_auto_apply_false_when_startup_is_empty(self) -> None:
        tuning = slot_tuning()
        fake, service = make_service(tuning=tuning, profiles_dir=DISABLED)
        report = service.build_report(0)
        assert report.afterburner.active_profile_id == 1
        assert report.afterburner.startup_auto_apply_present is False

    def test_external_authorities_are_always_unknown_not_observable(self) -> None:
        for status in (InterfaceStatus.OK, InterfaceStatus.NOT_RUNNING,
                       InterfaceStatus.NOT_INSTALLED):
            fake, service = make_service(status=status)
            report = service.build_report(0)
            assert report.nvidia_app_auto_tuning is AuthorityState.UNKNOWN_NOT_OBSERVABLE
            assert report.gassist_native_tuning is AuthorityState.UNKNOWN_NOT_OBSERVABLE
            assert report.other_oc_utilities is AuthorityState.UNKNOWN_NOT_OBSERVABLE

    def test_afterburner_down_yields_honest_snapshot(self) -> None:
        fake, service = make_service(status=InterfaceStatus.NOT_RUNNING)
        report = service.build_report(0)
        assert report.afterburner.interface_status is InterfaceStatus.NOT_RUNNING
        assert report.afterburner.applied_state is None
        assert report.afterburner.active_profile_id is None
        assert "isn't available" in report.summary

    def test_summary_never_claims_stacking(self) -> None:
        fake, service = make_service()
        report = service.build_report(0)
        assert "stack" not in report.summary.lower()


class TestOwnershipClause:
    def test_applied_value_reads_live_from_the_control_map(self) -> None:
        tuning = TuningState(
            gpu_index=0, power_limit_pct=100.0, core_offset_mhz=25.0,
            memory_offset_mhz=0.0, fan_mode="manual", fan_percent=60.0,
        )
        fake, service = make_service(tuning=tuning)
        assert service.applied_value(0, ControlFeature.POWER_LIMIT) == 100.0
        assert service.applied_value(0, ControlFeature.CORE_OFFSET) == 25.0
        assert service.applied_value(0, ControlFeature.FAN_PERCENT) == 60.0

    def test_applied_value_is_none_when_control_is_unreadable(self) -> None:
        fake, service = make_service(status=InterfaceStatus.NOT_RUNNING)
        assert service.applied_value(0, ControlFeature.POWER_LIMIT) is None

    def test_clause_names_current_value_and_warns_about_external_authorities(self) -> None:
        tuning = TuningState(gpu_index=0, power_limit_pct=100.0, core_offset_mhz=0.0,
                             memory_offset_mhz=0.0, fan_mode="auto", fan_percent=None)
        fake, service = make_service(tuning=tuning)
        clause = service.ownership_clause(0, ControlFeature.POWER_LIMIT, requested=118.0)
        assert "100" in clause  # the value this change replaces, read at decision time
        assert "replace" in clause
        assert "can't be seen through Afterburner" in clause

    def test_clause_without_request_describes_current_state(self) -> None:
        fake, service = make_service(status=InterfaceStatus.NOT_RUNNING)
        clause = service.ownership_clause(0, ControlFeature.FAN_PERCENT)
        assert "not currently readable" in clause
        assert "can't be seen through Afterburner" in clause


def plugin_over(tuning: TuningState | None = None) -> GAssistPlugin:
    fake = FakeAfterburner()
    fake.set_capabilities(default_capabilities())
    fake.set_telemetry(
        GpuTelemetry(gpu_index=0, gpu_name="Fake", temperature_c=60.0,
                     utilization_pct=90.0, core_clock_mhz=1500.0, fan_percent=50.0)
    )
    fake.set_tuning(tuning or TuningState(gpu_index=0))
    return GAssistPlugin(build_services(fake, DISABLED))


# ---------------------------------------------------------------------------
# Property 10: tuning-ownership honesty.
# ---------------------------------------------------------------------------


@st.composite
def random_tuning(draw):
    return TuningState(
        gpu_index=0,
        power_limit_pct=draw(st.one_of(
            st.none(), st.floats(min_value=50.0, max_value=118.0)
        )),
        core_offset_mhz=draw(st.one_of(
            st.none(), st.floats(min_value=-300.0, max_value=300.0)
        )),
        memory_offset_mhz=draw(st.one_of(
            st.none(), st.floats(min_value=-500.0, max_value=1000.0)
        )),
        fan_mode=draw(st.sampled_from(["auto", "manual", "curve"])),
        fan_percent=draw(st.one_of(
            st.none(), st.floats(min_value=0.0, max_value=100.0)
        )),
    )


class TestProperty10TuningOwnershipHonesty:
    @given(
        status=st.sampled_from(list(InterfaceStatus)),
        tuning=random_tuning(),
    )
    def test_external_authorities_always_unknown_and_no_stacking_claims(
        self, status, tuning
    ) -> None:
        fake, service = make_service(status=status, tuning=tuning)
        report = service.build_report(0)
        # (a) external authorities always report unknown_not_observable
        assert report.nvidia_app_auto_tuning is AuthorityState.UNKNOWN_NOT_OBSERVABLE
        assert report.gassist_native_tuning is AuthorityState.UNKNOWN_NOT_OBSERVABLE
        assert report.other_oc_utilities is AuthorityState.UNKNOWN_NOT_OBSERVABLE
        # summary never claims stacking or asserts external state
        assert "stack" not in report.summary.lower()
        for name in ("nvidia", "g-assist"):
            # the summary warns these authorities exist but are hidden behind Afterburner
            assert name.lower() in report.summary.lower()

    @given(
        tuning=random_tuning(),
        requested=st.floats(min_value=50.0, max_value=118.0),
    )
    def test_risky_confirmation_carries_ownership_clause_or_is_a_noop(
        self, tuning, requested
    ) -> None:
        plugin = plugin_over(tuning=tuning)
        messages = plugin.process(
            "execute",
            {"function": "set_power_limit",
             "arguments": {"percent": requested}},
            1,
        )
        assert len(messages) == 1
        params = messages[0]["params"]
        # Protocol V2: the complete data IS the text (no structured fields on the wire).
        assert isinstance(params["data"], str)
        if "already applied" in params["data"]:
            # Req 19.4 no-op: applied state already equals the request.
            assert params["keep_session"] is False
        else:
            # (c) risky confirmations prompt in text, hold the session open, and carry
            # the ownership clause.
            assert params["keep_session"] is True
            assert "can't be seen through Afterburner" in params["data"]
