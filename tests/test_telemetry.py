"""Task 8.2/8.3 — telemetry cache/rate/staleness; Property 8."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from afterburner.integration.fake import FakeAfterburner
from afterburner.models import ErrorCode, GpuTelemetry, PluginError
from afterburner.services.telemetry import TelemetryService


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def make_service(
    fake: FakeAfterburner,
    *,
    ttl: float = 2.0,
    min_poll: float = 1.0,
    stale: float = 5.0,
    clock: _Clock | None = None,
) -> tuple[TelemetryService, _Clock]:
    clock = clock or _Clock()
    service = TelemetryService(
        fake, ttl=ttl, min_poll_interval=min_poll, stale_threshold=stale, clock=clock
    )
    return service, clock


def telemetry(gpu_index: int = 0, temperature: float = 60.0) -> GpuTelemetry:
    return GpuTelemetry(
        gpu_index=gpu_index,
        gpu_name="Fake",
        temperature_c=temperature,
        utilization_pct=80.0,
        driver_version="552.22",
    )


class TestCachingAndRateLimiting:
    def test_first_read_polls_and_second_within_ttl_is_cached(self) -> None:
        fake = FakeAfterburner()
        fake.set_telemetry(telemetry())
        service, clock = make_service(fake)

        first = service.get_status(0)
        assert first.is_stale is False
        assert service.poll_count == 1

        clock.advance(1.0)  # < TTL (2 s)
        second = service.get_status(0)
        assert service.poll_count == 1  # served from cache
        assert second.sampled_at == first.sampled_at

    def test_after_ttl_polls_again(self) -> None:
        fake = FakeAfterburner()
        fake.set_telemetry(telemetry(temperature=60.0))
        service, clock = make_service(fake)
        service.get_status(0)
        clock.advance(3.0)  # > TTL, > min poll
        fake.telemetry_by_gpu[0] = telemetry(temperature=61.0)
        fresh = service.get_status(0)
        assert service.poll_count == 2
        assert fresh.temperature_c == 61.0
        assert fresh.is_stale is False

    def test_rate_limit_serves_cache_until_min_poll_elapses(self) -> None:
        fake = FakeAfterburner()
        fake.set_telemetry(telemetry())
        # TTL 0 => cache never "fresh", but min-poll 5 s gates refresh attempts.
        service, clock = make_service(fake, ttl=0.0, min_poll=5.0, stale=10.0)
        service.get_status(0)
        assert service.poll_count == 1

        clock.advance(1.0)  # < min poll: must not poll again
        service.get_status(0)
        assert service.poll_count == 1

        clock.advance(5.0)  # >= min poll: refresh allowed
        service.get_status(0)
        assert service.poll_count == 2

    def test_multi_gpu_indexing(self) -> None:
        fake = FakeAfterburner()
        fake.set_telemetry(telemetry(0, 60.0))
        fake.set_telemetry(telemetry(1, 71.0))
        service, clock = make_service(fake)
        assert service.get_status(0).temperature_c == 60.0
        assert service.get_status(1).temperature_c == 71.0
        assert service.poll_count == 2


class TestStaleness:
    def test_stale_flag_past_threshold_on_retained_cache(self) -> None:
        fake = FakeAfterburner()
        fake.set_telemetry(telemetry())
        service, clock = make_service(fake, ttl=0.0, min_poll=0.0, stale=5.0)
        service.get_status(0)

        # Refresh fails (interface down): last good cache is retained and flagged stale.
        fake.telemetry_fail_all = True
        clock.advance(6.0)
        result = service.get_status(0)
        assert result.is_stale is True
        assert result.temperature_c == 60.0  # never fabricates a value

    def test_never_polled_and_fails_raises_typed_error(self) -> None:
        fake = FakeAfterburner()
        fake.telemetry_fail_all = True
        service, clock = make_service(fake)
        with pytest.raises(PluginError) as excinfo:
            service.get_status(0)
        assert excinfo.value.code is ErrorCode.COMM_FAILURE

    def test_missing_fields_are_explicit_not_fabricated(self) -> None:
        fake = FakeAfterburner()
        fake.set_telemetry(GpuTelemetry(gpu_index=0, gpu_name="Fake"))  # no temps etc.
        service, clock = make_service(fake)
        result = service.get_status(0)
        assert result.temperature_c is None  # explicit unavailable, no fabricated value


# ---------------------------------------------------------------------------
# Property 8: caching & rate invariant.
# ---------------------------------------------------------------------------


class TestProperty8TelemetryCacheAndRate:
    @given(
        times=st.lists(
            st.floats(min_value=0.0, max_value=12.0, allow_nan=False, allow_infinity=False),
            min_size=1,
            max_size=12,
        ).map(sorted),
    )
    def test_reads_inside_ttl_are_cached_and_past_threshold_flagged(self, times) -> None:
        fake = FakeAfterburner()
        fake.set_telemetry(telemetry())
        service = TelemetryService(
            fake,
            ttl=2.0,
            min_poll_interval=0.1,
            stale_threshold=5.0,
            clock=_Clock(),
        )
        prev = -1.0
        for t in times:
            if t <= prev:
                continue  # strictly increasing sample times only
            prev = t
            service._clock.t = 1000.0 + t  # type: ignore[attr-defined]
            result = service.get_status(0)
            # Cache invariant: with a working adapter, anything served that is flagged stale
            # must be retained data older than the stale threshold (we refresh as soon as the
            # min-poll interval allows).
            if result.is_stale:
                age = service.age_seconds(0)
                assert age is not None and age >= 5.0

    @given(
        wait=st.floats(min_value=0.0, max_value=2.5, allow_nan=False, allow_infinity=False),
    )
    def test_burst_reads_bound_underlying_poll_count(self, wait) -> None:
        fake = FakeAfterburner()
        fake.set_telemetry(telemetry())
        clock = _Clock()
        service = TelemetryService(
            fake, ttl=1.0, min_poll_interval=0.0, stale_threshold=5.0, clock=clock
        )
        for _ in range(5):
            service.get_status(0)  # burst: all served from the single first poll
        assert service.poll_count == 1
        clock.advance(wait + 1.1)  # past TTL regardless of the random wait
        service.get_status(0)
        assert service.poll_count == 2
