"""Shared pytest configuration and fixtures.

Hypothesis profile: every property test runs at least 100 examples (plan task 1).
"""
from __future__ import annotations

import pytest
from hypothesis import HealthCheck, settings


def pytest_configure(config: pytest.Config) -> None:
    settings.register_profile(
        "afterburner",
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    settings.load_profile("afterburner")
