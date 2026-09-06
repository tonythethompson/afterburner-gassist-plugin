"""Telemetry caching & staleness (design Caching & Staleness Strategy).

- Serve cache within TELEMETRY_TTL_SECONDS (2 s) — no adapter poll on cache hits.
- Enforce MIN_POLL_INTERVAL_SECONDS (1 s) between underlying polls.
- Flag `is_stale` once data is older than STALE_THRESHOLD_SECONDS (5 s).
- Never fabricate values: when no cached record exists and the poll fails, a typed PluginError
  is raised; when a poll fails but a cached record exists, the cache is retained (and flagged
  stale as it ages) so monitoring keeps degrading gracefully.

Constants are injectable (smaller values in tests) and match design/requirements:
TTL 2 s / min poll 1 s / stale 5 s.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Callable, Dict, Optional, Tuple

from ..integration.interface import AfterburnerInterface
from ..models import GpuTelemetry, PluginError

TELEMETRY_TTL_SECONDS = 2.0
MIN_POLL_INTERVAL_SECONDS = 1.0
STALE_THRESHOLD_SECONDS = 5.0


class TelemetryService:
    """Caches telemetry per GPU and enforces TTL / min-poll / staleness invariants."""

    def __init__(
        self,
        interface: AfterburnerInterface,
        *,
        ttl: float = TELEMETRY_TTL_SECONDS,
        min_poll_interval: float = MIN_POLL_INTERVAL_SECONDS,
        stale_threshold: float = STALE_THRESHOLD_SECONDS,
        now: Optional[Callable[[], datetime]] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        import time as _time
        from datetime import timezone as _tz

        self._interface = interface
        self.ttl = ttl
        self.min_poll_interval = min_poll_interval
        self.stale_threshold = stale_threshold
        self._now = now or (lambda: datetime.now(_tz.utc))
        self._clock = clock or _time.monotonic
        # gpu_index -> (telemetry, polled_at_monotonic)
        self._cache: Dict[int, Tuple[GpuTelemetry, float]] = {}
        # gpu_index -> monotonic time of the last poll *attempt* (success or failure)
        self._last_attempt: Dict[int, float] = {}
        self.poll_count = 0

    # ----------------------------------------------------------------------
    def get_status(self, gpu_index: int = 0) -> GpuTelemetry:
        """Return cached-or-fresh telemetry with an accurate `is_stale` flag."""
        now = self._clock()
        entry = self._cache.get(gpu_index)

        if entry is not None:
            cached, polled_at = entry
            age = now - polled_at
            if age < self.ttl:
                # Fresh cache: serve without touching the adapter.
                return self._flag(cached, False)
            if now - self._last_attempt.get(gpu_index, 0.0) < self.min_poll_interval:
                # Rate-limited (or a recent poll failed): serve what we have, flagging
                # staleness honestly instead of hammering the adapter.
                return self._flag(cached, age > self.stale_threshold)

        try:
            return self._poll(gpu_index)
        except PluginError:
            # A failed refresh keeps the last good cache (never fabricates); flag it stale.
            entry = self._cache.get(gpu_index)
            if entry is None:
                raise
            return self._flag(entry[0], True)

    # ----------------------------------------------------------------------
    def _poll(self, gpu_index: int) -> GpuTelemetry:
        self._last_attempt[gpu_index] = self._clock()
        telemetry = self._interface.read_telemetry(gpu_index)
        self.poll_count += 1
        stamped = replace(
            telemetry,
            sampled_at=self._now(),
            is_stale=False,
        )
        self._cache[gpu_index] = (stamped, self._clock())
        return stamped

    @staticmethod
    def _flag(telemetry: GpuTelemetry, stale: bool) -> GpuTelemetry:
        if telemetry.is_stale == stale:
            return telemetry
        return replace(telemetry, is_stale=stale)

    # ----------------------------------------------------------------------
    def age_seconds(self, gpu_index: int = 0) -> Optional[float]:
        """Age of the last successful poll in seconds (None when never polled)."""
        entry = self._cache.get(gpu_index)
        if entry is None:
            return None
        return self._clock() - entry[1]
