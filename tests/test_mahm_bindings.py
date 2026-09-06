"""MAHM ctypes binding checks (plan task 18.4, monitoring side of the 20.3 mirror).

(a) The `ctypes.Structure` definitions in `afterburner/integration/mahm.py` must match the
committed layout fixture field-for-field (names, sizes, offsets, alignment) — verified by
reconstructing ctypes structs deterministically from the fixture records and comparing with
the real bindings. When an Afterburner install is present, the bindings are additionally
checked against the *live installed header* and `tools/generate_sdk_layout_fixtures.py
--diff` is run as the layout-drift gate (a change in an Afterburner update fails the build).

The committed fixtures make the check always run in CI, install or not.
"""
from __future__ import annotations

import ctypes
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from afterburner.integration import mahm
from afterburner.integration.sdk_layout import load_fixture

FIXTURE = Path("tests/fixtures/sdk_layouts/mahm_layout.json")
GENERATOR = Path("tools/generate_sdk_layout_fixtures.py")

# ctypes leaf vocabulary mapping (design § Fixture JSON shape).
_CTYPES_LEAF = {
    "DWORD": ctypes.c_uint32,
    "LONG": ctypes.c_int32,
    "float": ctypes.c_float,
    "time_t": ctypes.c_int32,
    "__time32_t": ctypes.c_int32,
}

INSTALL_DIR = mahm.find_install_dir()
LIVE_HEADER = (
    INSTALL_DIR / "SDK" / "Include" / "MAHMSharedMemory.h"
    if INSTALL_DIR is not None
    else None
)


def _struct_from_fixture(doc, struct_name: str):
    """Deterministically reconstruct a ctypes.Structure from fixture records."""
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
                ctype = _CTYPES_LEAF[field["type"]]
                fields.append((field["name"], ctype))
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


def _fixture_struct_fields(doc, struct_name: str):
    return {
        f["name"]: (f["offset"], f["size"])
        for f in doc["structs"][struct_name]["fields"]
    }


def _real_binding_fields(binding) -> dict:
    return {name: (getattr(binding, name).offset, ctypes.sizeof(getattr(binding, name).type)) for name, _ in binding._fields_}


MAHM_STRUCTS = (
    "MAHM_SHARED_MEMORY_HEADER",
    "MAHM_SHARED_MEMORY_ENTRY",
    "MAHM_SHARED_MEMORY_GPU_ENTRY",
)
_BINDINGS = {
    "MAHM_SHARED_MEMORY_HEADER": mahm.MAHM_SHARED_MEMORY_HEADER,
    "MAHM_SHARED_MEMORY_ENTRY": mahm.MAHM_SHARED_MEMORY_ENTRY,
    "MAHM_SHARED_MEMORY_GPU_ENTRY": mahm.MAHM_SHARED_MEMORY_GPU_ENTRY,
}


class TestBindingsVsFixture:
    """Binding-vs-committed-fixture check — always runs (no install required)."""

    @classmethod
    def setup_class(cls) -> None:
        cls.doc = load_fixture(FIXTURE)

    @pytest.mark.parametrize("struct_name", MAHM_STRUCTS)
    def test_reconstructed_and_real_bindings_match_fixture(self, struct_name: str) -> None:
        doc = self.doc
        fixture_struct = doc["structs"][struct_name]
        reconstructed = _struct_from_fixture(doc, struct_name)
        real = _BINDINGS[struct_name]

        # Both must agree with the fixture's alignment/size…
        assert ctypes.alignment(reconstructed) == fixture_struct["alignment"]
        assert ctypes.alignment(real) == fixture_struct["alignment"]
        assert ctypes.sizeof(reconstructed) == fixture_struct["size"]
        assert ctypes.sizeof(real) == fixture_struct["size"]

        # …and with each other, field-by-field (name, offset, size).
        assert [n for n, _ in real._fields_] == [n for n, _ in reconstructed._fields_]
        assert _real_binding_fields(real) == _real_binding_fields(reconstructed)
        fixture_fields = _fixture_struct_fields(doc, struct_name)
        for name, (offset, size) in fixture_fields.items():
            real_field = getattr(real, name)
            assert real_field.offset == offset, f"{struct_name}.{name} offset"
            assert ctypes.sizeof(real_field.type) == size, f"{struct_name}.{name} size"

    def test_bindings_have_no_pack_override(self) -> None:
        for struct_name, binding in _BINDINGS.items():
            assert not hasattr(binding, "_pack_"), struct_name

    def test_fixture_constants_include_monitoring_source_ids(self) -> None:
        constants = self.doc["constants"]
        assert constants["MONITORING_SOURCE_ID_GPU_TEMPERATURE"] == "0x00000000"
        assert constants["MONITORING_SOURCE_ID_GPU_USAGE"] == "0x00000030"
        assert constants["MONITORING_SOURCE_ID_CORE_CLOCK"] == "0x00000020"
        assert constants["MONITORING_SOURCE_ID_GPU_ABS_POWER"] == "0x00000061"
        assert constants["MONITORING_SOURCE_ID_FAN_SPEED"] == "0x00000010"
        assert constants["MAHM_SHARED_MEMORY_ENTRY_FLAG_SHOW_IN_OSD"] == "0x00000001"


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
        live_doc = self.generator.build_document("MAHMSharedMemory.h", "MAHM", raw, INSTALL_DIR, None)
        committed = load_fixture(FIXTURE)
        assert self.generator.compare_documents(committed, live_doc) == []

    def test_live_header_matches_real_bindings(self) -> None:
        raw = LIVE_HEADER.read_bytes()
        live_doc = self.generator.build_document("MAHMSharedMemory.h", "MAHM", raw, INSTALL_DIR, None)
        for struct_name in MAHM_STRUCTS:
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
