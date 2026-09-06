"""MACM ctypes binding checks (plan task 20.3).

Mirror of the 18.4 monitoring-side check: (a) the `ctypes.Structure` definitions in
`afterburner/integration/macm.py` must match the committed `macm_layout.json` fixture
field-for-field (reconstructed deterministically from the fixture, then compared — names,
sizes, offsets, alignment, no `_pack_`), plus the header constants; when an Afterburner
install is present the bindings are also checked against the *live installed header* and the
`--diff` drift gate is run. The committed fixture keeps the check running in CI with no
install.
"""
from __future__ import annotations

import ctypes
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from afterburner.integration import macm
from afterburner.integration.sdk_layout import load_fixture

FIXTURE = Path("tests/fixtures/sdk_layouts/macm_layout.json")
GENERATOR = Path("tools/generate_sdk_layout_fixtures.py")

_CTYPES_LEAF = {
    "DWORD": ctypes.c_uint32,
    "LONG": ctypes.c_int32,
    "float": ctypes.c_float,
    "time_t": ctypes.c_int32,
    "__time32_t": ctypes.c_int32,
}

MACM_STRUCTS = (
    "MACM_SHARED_MEMORY_HEADER",
    "MACM_SHARED_MEMORY_GPU_ENTRY",
    "MACM_SHARED_MEMORY_VF_CURVE",
    "MACM_SHARED_MEMORY_VF_POINT_ENTRY",
    "MACM_SHARED_MEMORY_POWER_TUPLE_ENTRY",
    "MACM_SHARED_MEMORY_THERMAL_TUPLE_ENTRY",
)
_BINDINGS = {
    "MACM_SHARED_MEMORY_HEADER": macm.MACM_SHARED_MEMORY_HEADER,
    "MACM_SHARED_MEMORY_GPU_ENTRY": macm.MACM_SHARED_MEMORY_GPU_ENTRY,
    "MACM_SHARED_MEMORY_VF_CURVE": macm.MACM_SHARED_MEMORY_VF_CURVE,
    "MACM_SHARED_MEMORY_VF_POINT_ENTRY": macm.MACM_SHARED_MEMORY_VF_POINT_ENTRY,
    "MACM_SHARED_MEMORY_POWER_TUPLE_ENTRY": macm.MACM_SHARED_MEMORY_POWER_TUPLE_ENTRY,
    "MACM_SHARED_MEMORY_THERMAL_TUPLE_ENTRY": macm.MACM_SHARED_MEMORY_THERMAL_TUPLE_ENTRY,
}

INSTALL_DIR = macm.find_install_dir()
LIVE_HEADER = (
    INSTALL_DIR / "SDK" / "Include" / "MACMSharedMemory.h"
    if INSTALL_DIR is not None
    else None
)


def _struct_from_fixture(doc, struct_name: str):
    structs = doc["structs"]
    seen: dict = {}

    def build(name: str):
        if name in seen:
            return seen[name]
        record = structs[name]
        fields = []
        for field in record["fields"]:
            kind = field["kind"]
            if kind == "scalar":
                fields.append((field["name"], _CTYPES_LEAF[field["type"]]))
            elif kind == "chars":
                fields.append((field["name"], ctypes.c_char * field["count"]))
            elif kind == "struct":
                fields.append((field["name"], build(field["type"])))
            else:  # struct-array
                fields.append((field["name"], build(field["type"]) * field["count"]))
        cls = type(name, (ctypes.Structure,), {"_fields_": fields})
        seen[name] = cls
        return cls

    return build(struct_name)


class TestBindingsVsFixture:
    """Binding-vs-committed-fixture check — always runs (no install required)."""

    @classmethod
    def setup_class(cls) -> None:
        cls.doc = load_fixture(FIXTURE)

    @pytest.mark.parametrize("struct_name", MACM_STRUCTS)
    def test_reconstructed_and_real_bindings_match_fixture(self, struct_name: str) -> None:
        doc = self.doc
        fixture_struct = doc["structs"][struct_name]
        reconstructed = _struct_from_fixture(doc, struct_name)
        real = _BINDINGS[struct_name]

        assert ctypes.alignment(reconstructed) == fixture_struct["alignment"]
        assert ctypes.alignment(real) == fixture_struct["alignment"]
        assert ctypes.sizeof(reconstructed) == fixture_struct["size"]
        assert ctypes.sizeof(real) == fixture_struct["size"]

        assert [n for n, _ in real._fields_] == [n for n, _ in reconstructed._fields_]
        for name, (offset, size) in {
            f["name"]: (f["offset"], f["size"]) for f in fixture_struct["fields"]
        }.items():
            field = getattr(real, name)
            assert field.offset == offset, f"{struct_name}.{name} offset"
            assert ctypes.sizeof(field.type) == size, f"{struct_name}.{name} size"

    def test_bindings_have_no_pack_override(self) -> None:
        for binding in _BINDINGS.values():
            assert not hasattr(binding, "_pack_")

    def test_command_constants(self) -> None:
        assert macm.MACM_SHARED_MEMORY_COMMAND_INIT == 0x00AB0000
        assert macm.MACM_SHARED_MEMORY_COMMAND_FLUSH == 0x00AB0001
        assert macm.MACM_SHARED_MEMORY_COMMAND_FLUSH_WITHOUT_APPLYING == 0x00AB0002
        assert macm.MACM_SHARED_MEMORY_COMMAND_REFRESH_VF_CURVE == 0x00AB0003

    def test_fixture_constants_match_module(self) -> None:
        constants = self.doc["constants"]
        assert constants["MACM_SHARED_MEMORY_COMMAND_FLUSH"] == "0x00ab0001"
        assert (
            constants["MACM_SHARED_MEMORY_GPU_ENTRY_FLAG_POWER_LIMIT"] == "0x00000400"
        )
        assert (
            constants["MACM_SHARED_MEMORY_GPU_ENTRY_FLAG_CORE_CLOCK_BOOST"] == "0x00000800"
        )
        assert (
            constants["MACM_SHARED_MEMORY_GPU_ENTRY_FLAG_VF_CURVE"] == "0x00020000"
        )
        assert (
            constants["MACM_SHARED_MEMORY_VF_CURVE_POINTS_MAX"] == "0x00000100"
        )
        assert (
            constants["MACM_SHARED_MEMORY_VF_CURVE_TUPLES_MAX"] == "0x00000004"
        )

    def test_known_sizes(self) -> None:
        assert ctypes.sizeof(macm.MACM_SHARED_MEMORY_HEADER) == 36
        assert ctypes.sizeof(macm.MACM_SHARED_MEMORY_VF_CURVE) == 3224
        assert ctypes.sizeof(macm.MACM_SHARED_MEMORY_GPU_ENTRY) == 3760


@pytest.mark.skipif(LIVE_HEADER is None, reason="no Afterburner install present")
class TestBindingsVsLiveHeader:
    """Live-header binding check + drift gate — runs only when Afterburner is installed."""

    @classmethod
    def setup_class(cls):
        spec = importlib.util.spec_from_file_location("sdk_fixture_generator", GENERATOR)
        cls.generator = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(cls.generator)

    def test_live_header_parses_and_matches_committed_fixture(self) -> None:
        raw = LIVE_HEADER.read_bytes()
        live_doc = self.generator.build_document("MACMSharedMemory.h", "MACM", raw, INSTALL_DIR, None)
        committed = load_fixture(FIXTURE)
        assert self.generator.compare_documents(committed, live_doc) == []

    def test_live_header_matches_real_bindings(self) -> None:
        raw = LIVE_HEADER.read_bytes()
        live_doc = self.generator.build_document("MACMSharedMemory.h", "MACM", raw, INSTALL_DIR, None)
        for struct_name in MACM_STRUCTS:
            record = live_doc["structs"][struct_name]
            real = _BINDINGS[struct_name]
            assert ctypes.sizeof(real) == record["size"], struct_name
            assert ctypes.alignment(real) == record["alignment"], struct_name
            for field in record["fields"]:
                assert getattr(real, field["name"]).offset == field["offset"]

    def test_diff_gate_exits_zero_against_live_headers(self) -> None:
        result = subprocess.run(
            [sys.executable, str(GENERATOR), "--diff", "--install-dir", str(INSTALL_DIR)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
