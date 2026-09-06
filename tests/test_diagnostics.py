"""Plan tasks 10.2 / 10.3 — DiagnosticsService classification + Property 5 honesty."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypothesis import given
from hypothesis import strategies as st

from afterburner.integration.fake import FakeAfterburner
from afterburner.models import DiagnosisCause, GpuTelemetry
from afterburner.services.diagnostics import DiagnosticsService
from afterburner.services.telemetry import TelemetryService

ALLOWED_CAUSES = {
    DiagnosisCause.THERMAL,
    DiagnosisCause.POWER,
    DiagnosisCause.VOLTAGE,
    DiagnosisCause.UTILIZATION_BOTTLENECK,
    DiagnosisCause.CPU_LIMITED,
    DiagnosisCause.APP_BEHAVIOR,
    DiagnosisCause.UNKNOWN_INSUFFICIENT_DATA,
}


def telemetry(
    *,
    temperature_c: float | None = 60.0,
    utilization_pct: float | None = 80.0,
    core_clock_mhz: float | None = 1500.0,
    power_watts: float | None = None,
    power_limit_pct: float | None = None,
    voltage_mv: float | None = None,
    fan_percent: float | None = None,
    memory_used_mb: float | None = None,
    memory_total_mb: float | None = None,
) -> GpuTelemetry:
    return GpuTelemetry(
        gpu_index=0,
        gpu_name="Fake",
        temperature_c=temperature_c,
        utilization_pct=utilization_pct,
        core_clock_mhz=core_clock_mhz,
        power_watts=power_watts,
        power_limit_pct=power_limit_pct,
        voltage_mv=voltage_mv,
        fan_percent=fan_percent,
        memory_used_mb=memory_used_mb,
        memory_total_mb=memory_total_mb,
    )


def service_for(fake: FakeAfterburner, **diagnostics_kw) -> DiagnosticsService:
    telemetry_service = TelemetryService(
        fake, ttl=0.0, min_poll_interval=0.0, stale_threshold=5.0
    )
    return DiagnosticsService(telemetry_service, **diagnostics_kw)


def diagnose(t: GpuTelemetry, **kw):
    fake = FakeAfterburner()
    fake.set_telemetry(t)
    return service_for(fake, **kw).diagnose_performance(0)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TestClassificationBranches:
    def test_thermal(self) -> None:
        d = diagnose(telemetry(temperature_c=92.0, utilization_pct=99.0,
                               core_clock_mhz=1900.0, fan_percent=100.0))
        assert d.cause is DiagnosisCause.THERMAL
        assert "temperature_c=" in d.evidence[0]

    def test_power(self) -> None:
        d = diagnose(telemetry(temperature_c=70.0, utilization_pct=99.0,
                               core_clock_mhz=1900.0, power_watts=300.0,
                               power_limit_pct=100.0))
        assert d.cause is DiagnosisCause.POWER
        assert any(e.startswith("power_limit_pct=") or e.startswith("power_watts=")
                   for e in d.evidence)

    def test_voltage(self) -> None:
        d = diagnose(telemetry(temperature_c=70.0, utilization_pct=99.0,
                               core_clock_mhz=1900.0, voltage_mv=1150.0))
        assert d.cause is DiagnosisCause.VOLTAGE
        assert any(e.startswith("voltage_mv=") for e in d.evidence)

    def test_utilization_bottleneck(self) -> None:
        d = diagnose(telemetry(temperature_c=70.0, utilization_pct=99.0,
                               core_clock_mhz=1900.0))
        assert d.cause is DiagnosisCause.UTILIZATION_BOTTLENECK
        assert any(e.startswith("utilization_pct=") for e in d.evidence)

    def test_cpu_limited(self) -> None:
        d = diagnose(telemetry(temperature_c=60.0, utilization_pct=30.0,
                               core_clock_mhz=800.0))
        assert d.cause is DiagnosisCause.CPU_LIMITED

    def test_application_behavior_when_vram_pressured(self) -> None:
        d = diagnose(telemetry(temperature_c=60.0, utilization_pct=30.0,
                               core_clock_mhz=800.0,
                               memory_used_mb=7800.0, memory_total_mb=8000.0))
        assert d.cause is DiagnosisCause.APP_BEHAVIOR
        assert any(e.startswith("memory_used_mb=") for e in d.evidence)

    def test_mid_utilization_with_no_visible_limiter_is_unknown(self) -> None:
        d = diagnose(telemetry(temperature_c=60.0, utilization_pct=75.0,
                               core_clock_mhz=1500.0))
        assert d.cause is DiagnosisCause.UNKNOWN_INSUFFICIENT_DATA

    def test_evidence_is_non_empty_and_names_field_and_value(self) -> None:
        samples = [
            telemetry(temperature_c=92.0, utilization_pct=99.0,
                      core_clock_mhz=1900.0, fan_percent=100.0),
            telemetry(temperature_c=70.0, utilization_pct=99.0,
                      core_clock_mhz=1900.0, power_watts=300.0, power_limit_pct=100.0),
            telemetry(temperature_c=70.0, utilization_pct=99.0,
                      core_clock_mhz=1900.0, voltage_mv=1150.0),
            telemetry(temperature_c=60.0, utilization_pct=30.0, core_clock_mhz=800.0),
            telemetry(temperature_c=60.0, utilization_pct=30.0, core_clock_mhz=800.0,
                      memory_used_mb=7800.0, memory_total_mb=8000.0),
        ]
        for sample in samples:
            d = diagnose(sample)
            assert d.cause is not DiagnosisCause.UNKNOWN_INSUFFICIENT_DATA
            assert d.evidence  # Req 12.2: non-empty supporting evidence
            for item in d.evidence:
                assert "=" in item and item.split("=", 1)[1]  # field name + observed value

    def test_inferred_confidence_strictly_inside_unit_interval(self) -> None:
        samples = [
            telemetry(temperature_c=92.0, utilization_pct=99.0, core_clock_mhz=1900.0),
            telemetry(temperature_c=70.0, utilization_pct=99.0, core_clock_mhz=1900.0,
                      power_watts=300.0, power_limit_pct=100.0),
            telemetry(temperature_c=70.0, utilization_pct=99.0, core_clock_mhz=1900.0,
                      voltage_mv=1150.0),
            telemetry(temperature_c=70.0, utilization_pct=99.0, core_clock_mhz=1900.0),
            telemetry(temperature_c=60.0, utilization_pct=30.0, core_clock_mhz=800.0),
            telemetry(temperature_c=60.0, utilization_pct=30.0, core_clock_mhz=800.0,
                      memory_used_mb=7800.0, memory_total_mb=8000.0),
        ]
        for sample in samples:
            d = diagnose(sample)
            assert d.cause is not DiagnosisCause.UNKNOWN_INSUFFICIENT_DATA
            assert 0.0 < d.confidence < 1.0  # Req 12.5


class TestInsufficientData:
    def test_missing_essential_fields_are_unknown(self) -> None:
        for missing in ("temperature_c", "utilization_pct", "core_clock_mhz"):
            kwargs = {"temperature_c": 60.0, "utilization_pct": 80.0,
                      "core_clock_mhz": 1500.0}
            kwargs[missing] = None
            d = diagnose(telemetry(**kwargs))
            assert d.cause is DiagnosisCause.UNKNOWN_INSUFFICIENT_DATA
            assert "missing" in " ".join(d.evidence).lower()

    def test_non_finite_essential_values_are_unknown(self) -> None:
        d = diagnose(telemetry(temperature_c=float("nan"), utilization_pct=99.0,
                               core_clock_mhz=1900.0))
        assert d.cause is DiagnosisCause.UNKNOWN_INSUFFICIENT_DATA

    def test_flagged_stale_telemetry_is_unknown(self) -> None:
        fake = FakeAfterburner()
        # A clearly busy sample classifies as something other than UNKNOWN before the
        # simulated Afterburner restart makes the retained cache stale.
        fake.set_telemetry(telemetry(temperature_c=60.0, utilization_pct=99.0,
                                     core_clock_mhz=1900.0))
        tsvc = TelemetryService(fake, ttl=0.0, min_poll_interval=0.0, stale_threshold=5.0)
        svc = DiagnosticsService(tsvc)
        assert svc.diagnose_performance(0).cause is not DiagnosisCause.UNKNOWN_INSUFFICIENT_DATA
        # Afterburner "restarts": the refresh fails, the retained cache is flagged stale.
        fake.telemetry_fail_all = True
        d = svc.diagnose_performance(0)
        assert d.cause is DiagnosisCause.UNKNOWN_INSUFFICIENT_DATA
        assert any("stale" in e.lower() or "failed" in e.lower() for e in d.evidence)

    def test_sample_older_than_five_seconds_is_unknown(self) -> None:
        fake = FakeAfterburner()
        fake.set_telemetry(telemetry())
        # Diagnostics judges freshness by sampled_at age (Req 12.4: > 5 s => unknown).
        svc = service_for(fake, now=lambda: utcnow() + timedelta(seconds=8.0))
        d = svc.diagnose_performance(0)
        assert d.cause is DiagnosisCause.UNKNOWN_INSUFFICIENT_DATA
        assert any("older" in e.lower() for e in d.evidence)

    def test_telemetry_read_failure_is_unknown_not_a_crash(self) -> None:
        fake = FakeAfterburner()  # no telemetry at all + fail all reads
        fake.telemetry_fail_all = True
        d = service_for(fake).diagnose_performance(0)
        assert d.cause is DiagnosisCause.UNKNOWN_INSUFFICIENT_DATA

    def test_unknown_diagnosis_confidence_is_low(self) -> None:
        fake = FakeAfterburner()
        fake.set_telemetry(telemetry(utilization_pct=None))
        d = service_for(fake).diagnose_performance(0)
        assert d.cause is DiagnosisCause.UNKNOWN_INSUFFICIENT_DATA
        assert 0.0 < d.confidence < 0.5


# ---------------------------------------------------------------------------
# Property 5: diagnostics never overclaim on stale or missing data.
# ---------------------------------------------------------------------------


@st.composite
def telemetry_sample(draw):
    def maybe(minimum: float, maximum: float) -> st.SearchStrategy[float | None]:
        return st.one_of(st.none(), st.floats(min_value=minimum, max_value=maximum,
                                              allow_nan=False, allow_infinity=False))

    used = draw(st.one_of(
        st.none(),
        st.floats(min_value=0.0, max_value=8000.0, allow_nan=False, allow_infinity=False),
    ))
    total = draw(st.one_of(
        st.none(),
        st.floats(min_value=1.0, max_value=16000.0, allow_nan=False, allow_infinity=False),
    ))
    return GpuTelemetry(
        gpu_index=0,
        gpu_name="random",
        temperature_c=draw(maybe(-20.0, 125.0)),
        utilization_pct=draw(maybe(0.0, 100.0)),
        core_clock_mhz=draw(maybe(0.0, 3500.0)),
        power_watts=draw(maybe(0.0, 800.0)),
        power_limit_pct=draw(maybe(0.0, 160.0)),
        voltage_mv=draw(maybe(0.0, 1400.0)),
        fan_percent=draw(maybe(0.0, 100.0)),
        memory_used_mb=used,
        memory_total_mb=total,
    )


class TestProperty5DiagnosticsHonesty:
    @given(sample=telemetry_sample())
    def test_insufficient_inputs_are_unknown_and_inferences_never_certain(
        self, sample
    ) -> None:
        essential = (
            sample.temperature_c is None
            or sample.utilization_pct is None
            or sample.core_clock_mhz is None
        )
        d = diagnose(sample)
        assert d.cause in ALLOWED_CAUSES  # Req 12.1: exactly one of the allowed values
        assert d.summary
        assert 0.0 < d.confidence < 1.0  # Req 12.3/12.5 (unknowns are also low, never 1.0)
        if essential:
            assert d.cause is DiagnosisCause.UNKNOWN_INSUFFICIENT_DATA  # Req 12.4
        elif d.cause is not DiagnosisCause.UNKNOWN_INSUFFICIENT_DATA:
            # Req 12.2: any inference must come with non-empty field=value evidence.
            assert d.evidence
            for item in d.evidence:
                assert "=" in item and item.split("=", 1)[1]

    @given(offset=st.floats(min_value=0.0, max_value=60.0, allow_nan=False,
                            allow_infinity=False))
    def test_sample_older_than_the_stale_threshold_is_never_classified(
        self, offset
    ) -> None:
        fake = FakeAfterburner()
        fake.set_telemetry(telemetry(temperature_c=92.0, utilization_pct=99.0,
                                     core_clock_mhz=1900.0))
        svc = service_for(fake, now=lambda: utcnow() + timedelta(seconds=offset))
        d = svc.diagnose_performance(0)
        if offset > 5.0:
            assert d.cause is DiagnosisCause.UNKNOWN_INSUFFICIENT_DATA
        else:
            # Under 5 s the classification may proceed, but never with certainty.
            assert 0.0 < d.confidence < 1.0
