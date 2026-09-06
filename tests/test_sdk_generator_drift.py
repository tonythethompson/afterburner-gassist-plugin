"""Generator drift-contract unit tests (compare_documents + --diff/--generate exit codes).

CI guards the `--diff` reporting itself: every machine-readable drift line kind the design
contracts (constant / struct-added / struct-removed / struct-size / struct-alignment /
field added-removed-reordered-changed) is produced correctly from synthetic headers, and the
exit codes hold end-to-end through `run_diff` and the CLI: 0 = match (or no install),
1 = semantic drift, 2 = the installed header no longer parses under the supported subset.
No Afterburner install is required — headers and committed fixtures are authored in tmp dirs.
"""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest

GENERATOR = Path("tools/generate_sdk_layout_fixtures.py")

# A minimal MAHM-style header covering constants, a two-scalar header struct, a struct with
# a char-array + trailing scalar, and a char-only struct (alignment 1). All constructs stay
# inside the generator's supported subset.
BASE_HEADER = """\
#ifndef _FAKE_SHARED_MEMORY_INCLUDED_
#define _FAKE_SHARED_MEMORY_INCLUDED_
#define FAKE_CONST_A 0x00000001
#define FAKE_CONST_B 0x00000010
#define FAKE_LEN 4
typedef struct FAKE_HEADER {
\tDWORD dwFirst;
\tDWORD dwSecond;
} FAKE_HEADER, *LPFAKE_HEADER;
typedef struct FAKE_ENTRY {
\tDWORD dwFlags;
\tchar szName[FAKE_LEN];
\tLONG lValue;
} FAKE_ENTRY, *LPFAKE_ENTRY;
typedef struct FAKE_CHARS {
\tchar szPad[MAX_PATH];
} FAKE_CHARS, *LPFAKE_CHARS;
#endif //_FAKE_SHARED_MEMORY_INCLUDED_
"""


def _load_generator():
    spec = importlib.util.spec_from_file_location("sdk_generator_under_test", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _doc(gen, header_text: str) -> dict:
    raw = header_text.encode("utf-8")
    doc = gen.build_document("MAHMSharedMemory.h", "MAHM", raw, Path("."), None)
    return copy.deepcopy(doc)


@pytest.fixture(scope="module")
def gen():
    return _load_generator()


# ---------------------------------------------------------------------------
# compare_documents line contract
# ---------------------------------------------------------------------------


class TestDriftLines:
    def test_no_drift_when_identical(self, gen) -> None:
        committed = _doc(gen, BASE_HEADER)
        live = _doc(gen, BASE_HEADER)
        assert gen.compare_documents(committed, live) == []

    def test_constant_value_change_line(self, gen) -> None:
        committed = _doc(gen, BASE_HEADER)
        live = _doc(gen, BASE_HEADER.replace("FAKE_CONST_A 0x00000001", "FAKE_CONST_A 0x00000002"))
        assert gen.compare_documents(committed, live) == [
            "constant: FAKE_CONST_A 0x00000001 -> 0x00000002"
        ]

    def test_constant_added_and_removed_lines(self, gen) -> None:
        with_c = BASE_HEADER.replace(
            "#define FAKE_LEN 4\n",
            "#define FAKE_CONST_C 0x00000020\n#define FAKE_LEN 4\n",
        )
        committed = _doc(gen, with_c)  # has FAKE_CONST_C
        live = _doc(gen, BASE_HEADER)  # does not
        lines = gen.compare_documents(committed, live)
        assert lines == ["constant: FAKE_CONST_C removed 0x00000020"]
        lines2 = gen.compare_documents(live, committed)
        assert lines2 == ["constant: FAKE_CONST_C added 0x00000020"]

    def test_struct_added_and_removed_lines(self, gen) -> None:
        committed = _doc(gen, BASE_HEADER)
        extra = (
            "\ntypedef struct FAKE_NEW {\n\tDWORD x;\n} FAKE_NEW, *LPFAKE_NEW;\n"
        )
        live = _doc(gen, BASE_HEADER + extra)
        lines = gen.compare_documents(committed, live)
        assert "struct-added: FAKE_NEW" in lines
        lines2 = gen.compare_documents(live, committed)
        assert "struct-removed: FAKE_NEW" in lines2

    def test_struct_size_line_from_field_addition(self, gen) -> None:
        committed = _doc(gen, BASE_HEADER)
        live = _doc(
            gen,
            BASE_HEADER.replace("DWORD dwSecond;", "DWORD dwSecond;\n\tDWORD dwThird;", 1),
        )
        lines = gen.compare_documents(committed, live)
        assert "struct-size: FAKE_HEADER 8 -> 12" in lines
        assert "field: FAKE_HEADER.dwThird added" in lines

    def test_struct_alignment_line(self, gen) -> None:
        committed = _doc(gen, BASE_HEADER)
        live = _doc(gen, BASE_HEADER)
        committed["structs"]["FAKE_CHARS"]["alignment"] = 8  # tampered committed
        lines = gen.compare_documents(committed, live)
        assert lines == ["struct-alignment: FAKE_CHARS 8 -> 1"]

    def test_field_offset_size_type_and_count_lines(self, gen) -> None:
        committed = _doc(gen, BASE_HEADER)
        live = _doc(gen, BASE_HEADER)

        # Mutate the committed side so each attribute comparison yields an old -> new line.
        fields = committed["structs"]["FAKE_ENTRY"]["fields"]
        by_name = {f["name"]: f for f in fields}
        by_name["szName"]["offset"] = by_name["szName"]["offset"] + 1
        by_name["szName"]["size"] = by_name["szName"]["size"] + 1
        by_name["dwFlags"]["type"] = "LONG"
        committed["structs"]["FAKE_HEADER"]["fields"][0]["type"] = "LONG"
        committed["structs"]["FAKE_ENTRY"]["fields"][0]["count"] = 2  # scalar w/ count: format only

        lines = gen.compare_documents(committed, live)
        assert "field: FAKE_ENTRY.szName offset: 5 -> 4" in lines
        assert "field: FAKE_ENTRY.szName size: 5 -> 4" in lines
        assert "field: FAKE_ENTRY.dwFlags type: LONG -> DWORD" in lines
        assert "field: FAKE_HEADER.dwFirst type: LONG -> DWORD" in lines

    def test_field_added_removed_and_reordered_lines(self, gen) -> None:
        committed = _doc(gen, BASE_HEADER)
        live = _doc(gen, BASE_HEADER)

        # Remove dwFirst from the live HEADER (=> committed has it -> "removed"),
        # and reorder the remaining fields.
        header_fields = live["structs"]["FAKE_HEADER"]["fields"]
        header_fields[:] = [f for f in header_fields if f["name"] != "dwFirst"]
        header_fields.reverse()

        lines = gen.compare_documents(committed, live)
        assert "field: FAKE_HEADER.dwFirst removed" in lines
        assert "field: FAKE_HEADER reordered" in lines

        # The reverse direction reports the added field on the live side.
        lines2 = gen.compare_documents(live, committed)
        assert "field: FAKE_HEADER.dwFirst added" in lines2

    def test_field_array_count_change_line(self, gen) -> None:
        committed = _doc(gen, BASE_HEADER)
        live = _doc(gen, BASE_HEADER.replace("#define FAKE_LEN 4\n", "#define FAKE_LEN 8\n"))
        lines = gen.compare_documents(committed, live)
        # szName stays at offset 4 (dwFlags still precedes it); count/size grow and the
        # trailing scalar shifts, so the struct size grows.
        assert "field: FAKE_ENTRY.szName count: 4 -> 8" in lines
        assert "field: FAKE_ENTRY.szName size: 4 -> 8" in lines
        assert "field: FAKE_ENTRY.lValue offset: 8 -> 12" in lines
        assert "struct-size: FAKE_ENTRY 12 -> 16" in lines

    def test_lines_are_sorted_and_deduplicated(self, gen) -> None:
        committed = _doc(gen, BASE_HEADER)
        live = _doc(gen, BASE_HEADER.replace("FAKE_CONST_A 0x00000001", "FAKE_CONST_A 0x00000003"))
        live["structs"]["FAKE_CHARS"]["alignment"] = 2
        lines = gen.compare_documents(committed, live)
        assert lines == sorted(set(lines))


# ---------------------------------------------------------------------------
# run_diff / CLI exit codes (0 = match/skip, 1 = drift, 2 = unparseable)
# ---------------------------------------------------------------------------


def _write_install(root: Path, header_text: str = BASE_HEADER) -> Path:
    include = root / "SDK" / "Include"
    include.mkdir(parents=True, exist_ok=True)
    (include / "MAHMSharedMemory.h").write_text(header_text, encoding="utf-8")
    (root / "MSIAfterburner.exe").write_bytes(b"MZ-fake")  # for install detection
    return root


def _write_committed(gen, fixtures_dir: Path, header_text: str = BASE_HEADER) -> Path:
    doc = _doc(gen, header_text)
    target = fixtures_dir / "mahm_layout.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(gen.serialize_doc(doc), encoding="utf-8")
    return target


class TestExitCodes:
    def test_exit0_when_committed_matches_live(self, gen, tmp_path) -> None:
        install = _write_install(tmp_path / "install")
        fixtures = tmp_path / "fixtures"
        _write_committed(gen, fixtures)
        code, messages = gen.run_diff(install, fixtures)
        assert code == 0
        assert messages == []

    def test_exit0_when_no_install_header_present(self, gen, tmp_path) -> None:
        code, messages = gen.run_diff(tmp_path / "empty-install", tmp_path / "empty-fixtures")
        assert code == 0
        assert messages == ["no installed header — using committed fixtures"]

    def test_exit1_on_semantic_drift(self, gen, tmp_path) -> None:
        install = _write_install(tmp_path / "install")
        fixtures = tmp_path / "fixtures"
        _write_committed(gen, fixtures)
        drifted = BASE_HEADER.replace("FAKE_CONST_A 0x00000001", "FAKE_CONST_A 0x00000005")
        _write_install(install, drifted)
        code, messages = gen.run_diff(install, fixtures)
        assert code == 1
        assert "constant: FAKE_CONST_A 0x00000001 -> 0x00000005" in messages

    def test_exit1_on_field_layout_drift(self, gen, tmp_path) -> None:
        install = _write_install(tmp_path / "install")
        fixtures = tmp_path / "fixtures"
        _write_committed(gen, fixtures)
        _write_install(install, BASE_HEADER.replace("#define FAKE_LEN 4\n", "#define FAKE_LEN 16\n"))
        code, messages = gen.run_diff(install, fixtures)
        assert code == 1
        assert any("field: FAKE_ENTRY.szName" in m for m in messages)
        assert any("struct-size: FAKE_ENTRY" in m for m in messages)

    def test_exit2_when_live_header_no_longer_parses(self, gen, tmp_path) -> None:
        install = _write_install(tmp_path / "install")
        fixtures = tmp_path / "fixtures"
        _write_committed(gen, fixtures)
        _write_install(
            install,
            BASE_HEADER + "\n#include <windows.h>\n",
        )
        code, messages = gen.run_diff(install, fixtures)
        assert code == 2
        assert any("cannot parse installed header" in m for m in messages)

    def test_exit2_when_header_uses_unsupported_conditional(self, gen, tmp_path) -> None:
        install = _write_install(tmp_path / "install")
        fixtures = tmp_path / "fixtures"
        _write_committed(gen, fixtures)
        broken = BASE_HEADER.replace("#ifndef _FAKE_SHARED_MEMORY_INCLUDED_",
                                     "#if FAKE_CONST_A\n#ifndef _FAKE_SHARED_MEMORY_INCLUDED_")
        _write_install(install, broken)
        code, messages = gen.run_diff(install, fixtures)
        assert code == 2

    # ------------------------------------------------------------- CLI level
    def test_cli_generate_then_diff_roundtrip(self, gen, tmp_path, monkeypatch, capsys) -> None:
        install = _write_install(tmp_path / "install")
        fixtures = tmp_path / "fixtures"
        monkeypatch.setattr(gen, "FIXTURE_DIR", fixtures)

        assert gen.main(["--generate", "--install-dir", str(install)]) == 0
        assert (fixtures / "mahm_layout.json").is_file()
        assert gen.main(["--diff", "--install-dir", str(install)]) == 0

    def test_cli_generate_exit1_when_no_sdk_headers(self, gen, tmp_path, monkeypatch, capsys) -> None:
        install = _write_install(tmp_path / "install", "")
        (install / "SDK" / "Include" / "MAHMSharedMemory.h").unlink()
        monkeypatch.setattr(gen, "FIXTURE_DIR", tmp_path / "fixtures")
        assert gen.main(["--generate", "--install-dir", str(install)]) == 1
        assert "no installed headers found" in capsys.readouterr().out

    def test_cli_generate_exit2_on_parse_failure(self, gen, tmp_path, monkeypatch, capsys) -> None:
        install = _write_install(
            tmp_path / "install", BASE_HEADER + "\n#pragma pack(push)\n"
        )
        fixtures = tmp_path / "fixtures"
        monkeypatch.setattr(gen, "FIXTURE_DIR", fixtures)
        assert gen.main(["--generate", "--install-dir", str(install)]) == 2
        out = capsys.readouterr().out
        assert "cannot parse installed header" in out
        assert not (fixtures / "mahm_layout.json").exists()  # nothing written

    def test_cli_diff_exit1_and_exit2(self, gen, tmp_path, monkeypatch, capsys) -> None:
        install = _write_install(tmp_path / "install")
        fixtures = tmp_path / "fixtures"
        _write_committed(gen, fixtures)
        monkeypatch.setattr(gen, "FIXTURE_DIR", fixtures)

        _write_install(install, BASE_HEADER.replace("FAKE_CONST_A 0x00000001",
                                                    "FAKE_CONST_A 0x00000007"))
        assert gen.main(["--diff", "--install-dir", str(install)]) == 1
        assert "drift detected" in capsys.readouterr().out

        _write_install(install, BASE_HEADER + "\n#include <stdint.h>\n")
        assert gen.main(["--diff", "--install-dir", str(install)]) == 2
