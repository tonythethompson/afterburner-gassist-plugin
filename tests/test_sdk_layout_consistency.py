"""Committed sdk_layout fixture consistency (plan task 18.3, CI, no Afterburner install).

Loads both committed fixtures through the single strict loader and replays the documented
MSVC layout rules from the recorded field declarations — so corrupted, truncated, or
hand-edited fixtures fail CI before the ctypes binding checks (18.4 / 20.3) run against a
bad fixture. The layout rules live in the same shared helper the generator uses, so the test
can never silently diverge from what the generator computes.
"""
from __future__ import annotations

import json

import pytest

from afterburner.integration.sdk_layout import (
    LayoutError,
    check_consistency,
    consistency_issues,
    load_fixture,
    serialize_doc,
    validate_doc,
)

FIXTURES = {
    "MAHM": "tests/fixtures/sdk_layouts/mahm_layout.json",
    "MACM": "tests/fixtures/sdk_layouts/macm_layout.json",
}


class TestFixtureConsistency:
    @pytest.mark.parametrize("name,path", FIXTURES.items())
    def test_loader_accepts_and_rules_replay_clean(self, name: str, path: str) -> None:
        doc = load_fixture(path)
        assert doc["schema"] == "sdk-layout/1"
        assert doc["version"] == "0x00020000"
        issues = consistency_issues(doc)
        assert issues == [], f"{name}: {issues}"
        check_consistency(doc)  # must not raise

    @pytest.mark.parametrize("name,path", FIXTURES.items())
    def test_canonical_serialization_round_trips(self, name: str, path: str) -> None:
        doc = load_fixture(path)
        reloaded = load_fixture_bytes(serialize_doc(doc).encode("utf-8"))
        assert reloaded == doc

    def test_recorded_sizes_match_ctypes_ground_truth_for_mahm(self) -> None:
        """The committed MAHM sizes are the ones the bindings compile to (independent anchor)."""
        import ctypes

        from afterburner.integration import mahm

        doc = load_fixture(FIXTURES["MAHM"])
        structs = doc["structs"]
        expected = {
            "MAHM_SHARED_MEMORY_HEADER": ctypes.sizeof(mahm.MAHM_SHARED_MEMORY_HEADER),
            "MAHM_SHARED_MEMORY_ENTRY": ctypes.sizeof(mahm.MAHM_SHARED_MEMORY_ENTRY),
            "MAHM_SHARED_MEMORY_GPU_ENTRY": ctypes.sizeof(mahm.MAHM_SHARED_MEMORY_GPU_ENTRY),
        }
        for struct_name, size in expected.items():
            assert structs[struct_name]["size"] == size, struct_name

    def test_mahm_header_offsets_match_documented_example(self) -> None:
        doc = load_fixture(FIXTURES["MAHM"])
        fields = {
            f["name"]: (f["offset"], f["size"])
            for f in doc["structs"]["MAHM_SHARED_MEMORY_HEADER"]["fields"]
        }
        assert fields["dwSignature"] == (0, 4)
        assert fields["time"] == (20, 4)
        assert fields["dwGpuEntrySize"] == (28, 4)
        assert doc["structs"]["MAHM_SHARED_MEMORY_HEADER"]["size"] == 32

    def test_macm_header_and_vf_curve_match_documented_example(self) -> None:
        doc = load_fixture(FIXTURES["MACM"])
        header = doc["structs"]["MACM_SHARED_MEMORY_HEADER"]
        assert header["size"] == 36
        by_name = {f["name"]: f["offset"] for f in header["fields"]}
        assert by_name["dwMasterGpu"] == 20
        assert by_name["dwFlags"] == 24
        assert by_name["time"] == 28
        assert by_name["dwCommand"] == 32
        curve = doc["structs"]["MACM_SHARED_MEMORY_VF_CURVE"]
        assert curve["size"] == 3224
        assert doc["structs"]["MACM_SHARED_MEMORY_VF_POINT_ENTRY"]["size"] == 12
        assert doc["structs"]["MACM_SHARED_MEMORY_GPU_ENTRY"]["size"] == 3760


# ---------------------------------------------------------------------------
# Negative cases: the loader and replay must reject corrupted/hand-edited fixtures
# ---------------------------------------------------------------------------


class TestLoaderRejectsCorruption:
    def test_unknown_top_level_key_rejected(self) -> None:
        doc = load_fixture(FIXTURES["MAHM"])
        doc["extra"] = True
        with pytest.raises(LayoutError, match="unknown key"):
            validate_doc(doc)

    def test_unknown_schema_version_rejected(self) -> None:
        doc = load_fixture(FIXTURES["MAHM"])
        doc["schema"] = "sdk-layout/9"
        with pytest.raises(LayoutError, match="expected"):
            validate_doc(doc)

    def test_unknown_field_key_rejected(self) -> None:
        doc = load_fixture(FIXTURES["MAHM"])
        doc["structs"]["MAHM_SHARED_MEMORY_HEADER"]["fields"][0]["bogus"] = 1
        with pytest.raises(LayoutError, match="unknown key"):
            validate_doc(doc)

    def test_constant_non_canonical_hex_rejected(self) -> None:
        doc = load_fixture(FIXTURES["MAHM"])
        name = next(iter(doc["constants"]))
        doc["constants"][name] = "0xABC"
        with pytest.raises(LayoutError, match="does not match"):
            validate_doc(doc)

    def test_scalar_with_unknown_type_rejected(self) -> None:
        doc = load_fixture(FIXTURES["MAHM"])
        fields = doc["structs"]["MAHM_SHARED_MEMORY_HEADER"]["fields"]
        for f in fields:
            if f["kind"] == "scalar":
                f["type"] = "QWORD"
                break
        with pytest.raises(LayoutError, match="unknown scalar type"):
            validate_doc(doc)

    def test_count_present_on_scalar_rejected(self) -> None:
        doc = load_fixture(FIXTURES["MAHM"])
        fields = doc["structs"]["MAHM_SHARED_MEMORY_HEADER"]["fields"]
        for f in fields:
            if f["kind"] == "scalar":
                f["count"] = 2
                break
        with pytest.raises(LayoutError, match="must not carry a count"):
            validate_doc(doc)

    def test_struct_reference_missing_rejected(self) -> None:
        doc = load_fixture(FIXTURES["MACM"])
        fields = doc["structs"]["MACM_SHARED_MEMORY_GPU_ENTRY"]["fields"]
        for f in fields:
            if f["kind"] == "struct":
                f["type"] = "NO_SUCH_STRUCT"
                break
        with pytest.raises(LayoutError, match="not in structs"):
            validate_doc(doc)

    def test_offset_mismatch_against_layout_replay_detected(self) -> None:
        doc = load_fixture(FIXTURES["MAHM"])
        fields = doc["structs"]["MAHM_SHARED_MEMORY_HEADER"]["fields"]
        fields[1]["offset"] = fields[1]["offset"] + 1  # dwVersion now at 5
        issues = consistency_issues(doc)
        assert any("dwVersion" in issue and "offset" in issue for issue in issues)

    def test_truncated_struct_size_detected(self) -> None:
        doc = load_fixture(FIXTURES["MAHM"])
        rec = doc["structs"]["MAHM_SHARED_MEMORY_HEADER"]
        rec["size"] = rec["size"] - 4
        issues = consistency_issues(doc)
        assert issues

    def test_alignment_mismatch_detected(self) -> None:
        doc = load_fixture(FIXTURES["MACM"])
        rec = doc["structs"]["MACM_SHARED_MEMORY_VF_POINT_ENTRY"]
        rec["alignment"] = 1
        issues = consistency_issues(doc)
        assert any("alignment" in issue for issue in issues)

    def test_missing_required_provenance_rejected(self) -> None:
        doc = load_fixture(FIXTURES["MAHM"])
        del doc["from"]
        with pytest.raises(LayoutError, match="missing required key"):
            validate_doc(doc)


def load_fixture_bytes(raw: bytes):
    """Loader for an in-memory canonical document (round-trip test helper)."""
    doc = json.loads(raw.decode("utf-8"))
    return validate_doc(doc)
