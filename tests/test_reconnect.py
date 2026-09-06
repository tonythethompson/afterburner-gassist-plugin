"""ReconnectingAfterburner — recovery when Afterburner starts after the plugin spawns.

Covers the live finding (engine-kept plugin reported NOT_RUNNING forever because the
interface was chosen once at build time): delegate-when-OK, typed errors while down,
cooldown-gated probing, and swap-to-live recovery once Afterburner appears.
"""
from __future__ import annotations

import pytest

from afterburner.integration.fake import FakeAfterburner
from afterburner.integration.reconnect import ReconnectingAfterburner
from afterburner.models import (
    ControlFeature,
    ErrorCode,
    GpuTelemetry,
    InterfaceStatus,
    PluginError,
    TuningState,
)


class _Clock:
    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def telemetry(gpu: int = 0) -> GpuTelemetry:
    return GpuTelemetry(
        gpu_index=gpu, gpu_name="Fake", temperature_c=60.0,
        utilization_pct=50.0, core_clock_mhz=1500.0, fan_percent=40.0,
    )


def tuning(gpu: int = 0) -> TuningState:
    return TuningState(
        gpu_index=gpu, power_limit_pct=100.0, core_offset_mhz=0.0,
        memory_offset_mhz=0.0, fan_mode="manual", fan_percent=40.0,
    )


class _World:
    """Mutable 'is Afterburner up?' that the factory reflects at build time."""

    def __init__(self, status: InterfaceStatus = InterfaceStatus.OK) -> None:
        self.status = status
        self.builds = 0
        self.swaps = 0

    def factory(self):
        self.builds += 1
        fake = FakeAfterburner(status=self.status)
        fake.set_telemetry(telemetry())
        fake.set_tuning(tuning())
        return fake


def make(clock: _Clock, world: _World, **kw) -> ReconnectingAfterburner:
    wrapper = ReconnectingAfterburner(
        world.factory,
        world.factory(),  # the initial interface (spawn-time state)
        clock=clock,
        on_swapped=lambda _prev: setattr(world, "swaps", world.swaps + 1),
        **kw,
    )
    return wrapper


def test_delegates_when_ok_without_extra_probes() -> None:
    clock = _Clock()
    world = _World(InterfaceStatus.OK)
    wrapper = make(clock, world)
    assert world.builds == 1  # only the initial (spawn-time) interface was built
    assert wrapper.read_telemetry(0).gpu_name == "Fake"
    assert wrapper.detect() is InterfaceStatus.OK
    assert world.builds == 1  # no probing while healthy


def test_down_raises_the_typed_error() -> None:
    clock = _Clock()
    world = _World(InterfaceStatus.NOT_RUNNING)
    wrapper = make(clock, world)
    with pytest.raises(PluginError) as excinfo:
        wrapper.read_tuning_state(0)
    assert excinfo.value.code is ErrorCode.NOT_RUNNING


def test_probing_is_cooldown_gated_while_down() -> None:
    clock = _Clock()
    world = _World(InterfaceStatus.NOT_RUNNING)
    wrapper = make(clock, world)
    builds_after_init = world.builds
    for _ in range(5):  # five rapid calls inside the cooldown window
        with pytest.raises(PluginError):
            wrapper.read_telemetry(0)
    clock.advance(2.1)
    with pytest.raises(PluginError):
        wrapper.read_telemetry(0)
    # One gated attempt on the first call, one after the cooldown lapse: no hammering.
    assert world.builds <= builds_after_init + 2


def test_recovers_once_afterburner_starts_after_cooldown() -> None:
    clock = _Clock()
    world = _World(InterfaceStatus.NOT_RUNNING)
    wrapper = make(clock, world)
    with pytest.raises(PluginError) as excinfo:
        wrapper.read_telemetry(0)
    assert excinfo.value.code is ErrorCode.NOT_RUNNING

    # Afterburner comes up; within the cooldown a call still reports the typed error...
    world.status = InterfaceStatus.OK
    with pytest.raises(PluginError):
        wrapper.read_telemetry(0)
    assert world.swaps == 0

    # ...and after the cooldown lapses the very next call swaps in a live client.
    clock.advance(2.1)
    telemetry_result = wrapper.read_telemetry(0)
    assert telemetry_result.temperature_c == 60.0
    assert world.swaps == 1
    assert wrapper.detect() is InterfaceStatus.OK


def test_stays_down_honestly_until_afterburner_really_appears() -> None:
    clock = _Clock()
    world = _World(InterfaceStatus.UNAVAILABLE)
    wrapper = make(clock, world)
    with pytest.raises(PluginError) as excinfo:
        wrapper.apply_control(0, ControlFeature.POWER_LIMIT, 90.0)
    assert excinfo.value.code is ErrorCode.INTERFACE_UNAVAILABLE
    assert world.swaps == 0
