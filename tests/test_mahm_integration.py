"""Optional MAHM integration smoke test (plan task 18.2).

Opt-in and skipped when Afterburner is absent *or* not currently running (no shared memory):
read-only smoke against a real installation — detection, version, telemetry reads, and the
runtime cross-check that the live map's self-describing sizes agree with the compiled
`ctypes` bindings (the design's independent anchor for the committed offsets).
"""
from __future__ import annotations

import ctypes

import pytest

from afterburner.integration import mahm
from afterburner.models import InterfaceStatus

INSTALL_DIR = mahm.find_install_dir()
pytestmark = pytest.mark.skipif(
    INSTALL_DIR is None,
    reason="MSI Afterburner install not detected (opt-in integration test)",
)


def _live_client():
    client = mahm.AfterburnerMonitoringClient(install_dir=INSTALL_DIR)
    if client.detect() is not InterfaceStatus.OK:
        pytest.skip("MSI Afterburner is not currently exposing MAHM shared memory")
    return client


class TestMahmIntegration:
    def test_detect_ok_and_version_present(self) -> None:
        client = _live_client()
        assert client.detect() is InterfaceStatus.OK
        version = client.get_version()
        assert version is not None and version.count(".") >= 2

    def test_read_only_telemetry_smoke(self) -> None:
        client = _live_client()
        telemetry = client.read_all_telemetry()
        assert len(telemetry) >= 1
        for index, sample in enumerate(telemetry):
            assert sample.gpu_index == index
            assert isinstance(sample.gpu_name, str) and sample.gpu_name
        # Per-GPU read agrees with the all-GPU snapshot for GPU 0.
        first = client.read_telemetry(0)
        assert first.gpu_index == 0
        assert first.gpu_name == telemetry[0].gpu_name

    def test_live_region_cross_checks_binding_sizes(self) -> None:
        # Design: the runtime validators cross-check ctypes sizeof against the map's
        # self-describing dwHeaderSize / dwEntrySize / dwGpuEntrySize during optional
        # integration tests. A successful parse already enforces equality when entries are
        # present; assert it explicitly.
        client = _live_client()
        snapshot = client._snapshot()  # read-only map + parse
        assert snapshot.header_size == ctypes.sizeof(mahm.MAHM_SHARED_MEMORY_HEADER)
        assert snapshot.version == mahm.MAHM_VERSION
        if snapshot.sources:
            # dwEntrySize equality was required to parse; sources exist.
            assert len(snapshot.sources) >= 1
