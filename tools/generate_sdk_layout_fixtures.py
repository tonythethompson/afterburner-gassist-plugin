#!/usr/bin/env python3
"""SDK shared-memory layout fixture generator (design § SDK layout fixtures).

Parses the officially shipped Afterburner SDK headers (`SDK\\Include\\MAHMSharedMemory.h`,
`MACMSharedMemory.h`) with a deliberately small preprocessing-lite pass (comments stripped,
object-like `#define`s extracted, `typedef struct` blocks walked, and exactly one supported
conditional — the documented `#ifdef _WIN64 __time32_t time; #else time_t time; #endif` —
whose branches are layout-identical and recorded as the `time_t` form). Any construct outside
that set aborts with a line-named parse error; the generator never guesses.

Usage:
    python tools/generate_sdk_layout_fixtures.py --generate [--install-dir DIR]
    python tools/generate_sdk_layout_fixtures.py --diff [--install-dir DIR]

- `--generate` writes both committed fixtures under tests/fixtures/sdk_layouts/ (atomic
  replace, deterministic canonical serialization); exits non-zero if no installed header is
  present (fixtures can only be produced from a real header).
- `--diff` compares committed fixtures against the installed headers. Exit 0 on exact match
  or when no install is present (skip, committed fixtures stand); exit 1 on semantic drift;
  exit 2 when the installed header no longer parses under the supported subset.

The generator never edits binding code — it only reads headers, writes the two fixture files,
or compares and reports.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import struct
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

# Allow running as a plain script from anywhere in the repo.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from afterburner.integration.sdk_layout import (  # noqa: E402
    LEAF_VOCABULARY,
    PLATFORM_CONSTANTS,
    LayoutError,
    canonical_hex,
    check_consistency,
    compute_layout,
    load_fixture,
    parse_hex,
    serialize_doc,
    validate_doc,
)

HEADERS = (
    ("MAHMSharedMemory.h", "MAHM", "mahm_layout.json"),
    ("MACMSharedMemory.h", "MACM", "macm_layout.json"),
)
FIXTURE_DIR = _ROOT / "tests" / "fixtures" / "sdk_layouts"

_STANDARD_INSTALL_DIRS = (
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "MSI Afterburner",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "MSI Afterburner",
)


class HeaderParseError(Exception):
    """The installed header cannot be parsed under the supported subset (exit 2)."""


# ---------------------------------------------------------------------------
# Preprocessing-lite
# ---------------------------------------------------------------------------


def _strip_comments(text: str) -> str:
    """Remove // and /* */ comments, preserving line count and positions."""
    out: List[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue  # keep the newline in the next iteration
        if ch == "/" and nxt == "*":
            i += 2
            while i < n and not (text[i] == "*" and i + 1 < n and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


_WIN64_TIME = re.compile(
    r"#ifdef[ \t]+_WIN64[^\n]*\n"
    r"[ \t]*__time32_t[ \t]+(time)[ \t]*;[^\n]*\n"
    r"#else[^\n]*\n"
    r"[ \t]*time_t[ \t]+time[ \t]*;[^\n]*\n"
    r"#endif",
    re.MULTILINE,
)
_DIRECTIVE_LINE = re.compile(r"^\s*#\s*(\w+)\b(.*)$")


def _error_at_line(text: str, pattern: re.Pattern, message: str) -> HeaderParseError:
    m = pattern.search(text)
    line = text.count("\n", 0, m.start()) + 1 if m else "?"
    return HeaderParseError(f"{message} (line {line})")


def _preprocess(text: str) -> Tuple[str, Dict[str, int]]:
    """Return (body text, define-name -> value).

    Strips comments, resolves the one supported conditional, removes include-guard and
    directive lines, and extracts every object-like `#define` into a constants table.
    """
    if "\\\n" in text:
        raise HeaderParseError(
            f"macro continuation found (line {text.count(chr(10), 0, text.index(chr(92)) + 1)})"
        )
    clean = _strip_comments(text)
    if "\\\n" in clean:  # pragma: no cover - defensive
        raise HeaderParseError("macro continuation found after comment stripping")

    # The one supported conditional: the _WIN64 time branch (layout-identical).
    clean, replacements = _WIN64_TIME.subn(r"time_t \1;", clean)
    leftover = re.compile(r"^\s*#\s*(?:ifdef|#if|if|else)\b", re.MULTILINE)
    if re.search(r"^\s*#\s*(?:ifdef|if)\b", clean, re.MULTILINE):
        raise _error_at_line(
            clean,
            re.compile(r"^\s*#\s*(?:ifdef|if)\b", re.MULTILINE),
            "unsupported conditional other than the documented _WIN64 time field",
        )

    defines: Dict[str, int] = {}
    kept_body: List[str] = []
    include_or_pragma = re.compile(r"^\s*#\s*(?:include|pragma)\b", re.MULTILINE)
    if include_or_pragma.search(clean):
        raise _error_at_line(
            clean,
            include_or_pragma,
            "#include/#pragma is outside the supported header subset",
        )

    for lineno, line in enumerate(clean.split("\n"), start=1):
        m = _DIRECTIVE_LINE.match(line)
        if not m:
            kept_body.append(line)
            continue
        directive, rest = m.group(1), m.group(2)
        if directive == "define":
            dm = re.match(r"\s*([A-Za-z_]\w*)\s*(.*)$", rest)
            if dm is None:
                raise HeaderParseError(f"malformed #define (line {lineno})")
            name, value = dm.group(1), dm.group(2).strip()
            if name.endswith("_SHARED_MEMORY_INCLUDED_") and not value:
                continue  # include guard metadata
            vm = re.fullmatch(r"(0[xX][0-9a-fA-F]+|\d+)", value)
            if vm is None:
                raise HeaderParseError(
                    f"unsupported macro value on #define {name} (line {lineno}): "
                    "only object-like single-literal macros are supported"
                )
            defines[name] = int(value, 0)
        elif directive == "ifndef":
            # Only the include guard is supported at top level.
            if not re.match(r"\s*_\w+_SHARED_MEMORY_INCLUDED_\s*$", rest):
                raise HeaderParseError(f"unsupported #ifndef (line {lineno})")
        elif directive == "endif":
            pass  # include-guard close
        else:
            raise HeaderParseError(f"unsupported preprocessor directive #{directive} (line {lineno})")
    return "\n".join(kept_body), defines


# ---------------------------------------------------------------------------
# Struct walk
# ---------------------------------------------------------------------------

_TYPEDEF = re.compile(
    r"\btypedef\s+struct\s+([A-Za-z_]\w*)\s*\{(?P<body>.*?)\}\s*"
    r"([A-Za-z_]\w*)\s*(?:,\s*\*\s*[A-Za-z_]\w*\s*)?;",
    re.DOTALL,
)


def _parse_member_decls(body: str, struct_name: str) -> List[Dict[str, object]]:
    decls: List[Dict[str, object]] = []
    for seg in body.split(";"):
        seg = seg.strip()
        if not seg:
            continue
        if "," in seg:
            raise HeaderParseError(
                f"{struct_name}: unsupported multi-declarator member: {seg!r}"
            )
        tokens = re.findall(r"[A-Za-z_]\w*|\[[^\]]*\]", seg)
        if not tokens:
            continue
        type_token = tokens[0]
        if len(tokens) not in (2, 3):
            raise HeaderParseError(
                f"{struct_name}: unsupported member declaration {seg!r} "
                "(expected a single type token, member name, and optional array bound)"
            )
        name = tokens[1]
        if not tokens[1].isidentifier():
            raise HeaderParseError(f"{struct_name}: malformed member {seg!r}")
        array_expr = None
        if len(tokens) == 3:
            am = re.fullmatch(r"\[(.+)\]", tokens[2])
            if am is None:
                raise HeaderParseError(f"{struct_name}.{name}: malformed array declarator")
            array_expr = am.group(1).strip()
        decls.append(
            {"name": name, "type": type_token, "array": array_expr}
        )
    return decls


def parse_header_bytes(raw: bytes) -> Dict[str, object]:
    """Parse one SDK header into an sdk-layout fixture document (unvalidated layout)."""
    try:
        text = raw.decode("windows-1252")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")

    body, defines = _preprocess(text)
    constants = {name: canonical_hex(value) for name, value in sorted(defines.items())}

    # Walk typedef struct blocks in declaration order and ensure nothing else remains.
    struct_decls: Dict[str, List[Dict[str, object]]] = {}
    cursor = 0
    for m in _TYPEDEF.finditer(body):
        if m.start() < cursor:
            raise HeaderParseError("unexpected nested content between structs")
        cursor = m.end()
        # Groups: (1) struct tag after `struct`, (2) named `body`, (3) typedef alias after `}`.
        struct_decls[m.group(3)] = _parse_member_decls(m.group("body"), m.group(1))
    remaining = _TYPEDEF.sub(" ", body)
    if remaining.strip():
        head = remaining.strip().splitlines()[0][:80]
        raise HeaderParseError(f"unexpected content outside typedef struct blocks: {head!r}")

    # Classify each member and fold array bounds (constants are fully collected above).
    decls_by_name: Dict[str, List[Dict[str, object]]] = {}
    for struct_name, members in struct_decls.items():
        fields: List[Dict[str, object]] = []
        for member in members:
            type_token = str(member["type"])
            name = str(member["name"])
            array_expr = member.get("array")
            if type_token == "char":
                if array_expr is None:
                    raise HeaderParseError(f"{struct_name}.{name}: bare char member unsupported")
                count = _resolve_bound(str(array_expr), defines, struct_name, name)
                kind = "chars"
                type_field = "char"
            elif type_token in LEAF_VOCABULARY:
                if array_expr is not None:
                    raise HeaderParseError(
                        f"{struct_name}.{name}: scalar array member unsupported"
                    )
                kind, type_field, count = "scalar", type_token, None
            elif type_token in struct_decls:
                kind, type_field = ("struct-array" if array_expr is not None else "struct"), type_token
                count = (
                    _resolve_bound(str(array_expr), defines, struct_name, name)
                    if array_expr is not None
                    else None
                )
            else:
                raise HeaderParseError(
                    f"{struct_name}.{name}: unknown type token {type_token!r} "
                    "(not a leaf type or a defined struct)"
                )
            field: Dict[str, object] = {
                "name": name,
                "kind": kind,
                "type": type_field,
            }
            if count is not None:
                field["count"] = count
            fields.append(field)
        decls_by_name[struct_name] = fields

    # Compute layouts (single shared helper) and record offsets/sizes.
    layouts = {}
    for struct_name in decls_by_name:
        layouts[struct_name] = compute_layout(
            struct_name, lambda n, _d=decls_by_name: _d[n]
        )
    structs: Dict[str, object] = {}
    for struct_name, fields in decls_by_name.items():
        layout = layouts[struct_name]
        records: List[Dict[str, object]] = []
        for field in fields:
            kind = str(field["kind"])
            name = str(field["name"])
            type_field = str(field["type"])
            count = field.get("count")
            if kind == "scalar":
                size = 4
            elif kind == "chars":
                size = count  # type: ignore[assignment]
            elif kind == "struct":
                size = layouts[type_field].size
            else:  # struct-array
                size = count * layouts[type_field].size  # type: ignore[operator]
            record: Dict[str, object] = {
                "name": name,
                "kind": kind,
                "type": type_field,
                "offset": layout.offsets[name],
                "size": size,
            }
            if count is not None:
                record["count"] = count
            records.append(record)
        structs[struct_name] = {"alignment": layout.alignment, "size": layout.size, "fields": records}
    return {"constants": constants, "structs": structs}


def _resolve_bound(expr: str, defines: Mapping[str, int], struct_name: str, member: str) -> int:
    if expr.isdigit():
        return int(expr, 10)
    if expr in defines:
        return defines[expr]
    if expr in PLATFORM_CONSTANTS:
        return PLATFORM_CONSTANTS[expr]
    raise HeaderParseError(
        f"{struct_name}.{member}: unresolvable array bound {expr!r} "
        "(not a literal, collected #define, or platform constant)"
    )


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def _pe_file_version(exe: Path) -> Optional[str]:
    """Read the FileVersion of an exe via the Win32 version resource (Windows only)."""
    try:
        import ctypes
        from ctypes import wintypes

        ver = ctypes.WinDLL("version", use_last_error=False)
        size_fn = ver.GetFileVersionInfoSizeW
        size_fn.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        size_fn.restype = wintypes.DWORD
        info_fn = ver.GetFileVersionInfoW
        info_fn.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
        info_fn.restype = wintypes.BOOL
        query_fn = ver.VerQueryValueW
        query_fn.argtypes = [
            ctypes.c_void_p,
            wintypes.LPCWSTR,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.UINT),
        ]
        query_fn.restype = wintypes.BOOL
        _reserved = wintypes.DWORD()
        size = size_fn(str(exe), ctypes.byref(_reserved))
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not info_fn(str(exe), 0, size, buf):
            return None
        ptr = ctypes.c_void_p()
        length = wintypes.UINT()
        if not query_fn(buf, "\\", ctypes.byref(ptr), ctypes.byref(length)):
            return None
        # VS_FIXEDFILEINFO: dwSignature(0) dwStrucVersion(4) dwFileVersionMS(8)
        # dwFileVersionLS(12). FileVersion formats as MS.major.minor? -> major.minor.build.rev.
        hi, lo = struct.unpack_from("<II", ctypes.string_at(ptr.value, 16), 8)
        return f"{hi >> 16}.{hi & 0xFFFF}.{lo >> 16}.{lo & 0xFFFF}"
    except Exception:
        return None


def detect_afterburner_version(install_dir: Path) -> str:
    exe = install_dir / "MSIAfterburner.exe"
    if os.name == "nt" and exe.is_file():
        version = _pe_file_version(exe)
        if version:
            return version
    return os.environ.get("MSI_AFTERBURNER_VERSION", "unknown")


def find_install_dir(override: Optional[str]) -> Optional[Path]:
    candidates = []
    if override:
        candidates.append(Path(override))
    env = os.environ.get("MSI_AFTERBURNER_INSTALL_DIR")
    if env:
        candidates.append(Path(env))
    candidates.extend(_STANDARD_INSTALL_DIRS)
    for candidate in candidates:
        if (candidate / "SDK" / "Include").is_dir() or (candidate / "MSIAfterburner.exe").is_file():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Fixture assembly / atomic writes
# ---------------------------------------------------------------------------


def build_document(
    header_name: str,
    signature: str,
    raw_header: bytes,
    install_dir: Path,
    version_override: Optional[str],
) -> Dict[str, object]:
    parsed = parse_header_bytes(raw_header)
    doc: Dict[str, object] = {
        "schema": "sdk-layout/1",
        "header": header_name,
        "signature": signature,
        "version": "0x00020000",
        "from": {
            "afterburner_version": version_override or detect_afterburner_version(install_dir),
            "header_sha256": hashlib.sha256(raw_header).hexdigest(),
        },
        "constants": parsed["constants"],
        "structs": parsed["structs"],
    }
    validate_doc(doc)  # schema + cross-field invariants, before any write
    check_consistency(doc)  # replay the layout rules against the recorded offsets
    return doc


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def header_path(install_dir: Path, header_name: str) -> Optional[Path]:
    candidate = install_dir / "SDK" / "Include" / header_name
    return candidate if candidate.is_file() else None


# ---------------------------------------------------------------------------
# Drift diff
# ---------------------------------------------------------------------------


def _fmt_value(field: Mapping[str, object], attr: str) -> str:
    if attr in ("offset", "size"):
        return str(field[attr])
    if attr == "count":
        return str(field.get("count"))
    return str(field[attr])


def compare_documents(committed: Mapping[str, object], live: Mapping[str, object]) -> List[str]:
    """Return sorted, machine-readable drift lines (design § Drift diff)."""
    lines: List[str] = []
    live_constants = live["constants"]
    committed_constants = committed["constants"]
    assert isinstance(live_constants, dict) and isinstance(committed_constants, dict)
    for name in sorted(set(live_constants) | set(committed_constants)):
        c, l = committed_constants.get(name), live_constants.get(name)
        if c is None:
            lines.append(f"constant: {name} added {l}")
        elif l is None:
            lines.append(f"constant: {name} removed {c}")
        elif parse_hex(str(c)) != parse_hex(str(l)):
            lines.append(f"constant: {name} {c} -> {l}")

    live_structs = live["structs"]
    committed_structs = committed["structs"]
    assert isinstance(live_structs, dict) and isinstance(committed_structs, dict)
    for name in sorted(set(live_structs) | set(committed_structs)):
        if name not in committed_structs:
            lines.append(f"struct-added: {name}")
            continue
        if name not in live_structs:
            lines.append(f"struct-removed: {name}")
            continue
        c, l = committed_structs[name], live_structs[name]
        if c["size"] != l["size"]:
            lines.append(f"struct-size: {name} {c['size']} -> {l['size']}")
        if c["alignment"] != l["alignment"]:
            lines.append(f"struct-alignment: {name} {c['alignment']} -> {l['alignment']}")
        c_fields, l_fields = c["fields"], l["fields"]
        by_name_c = {f["name"]: f for f in c_fields}
        by_name_l = {f["name"]: f for f in l_fields}
        for fname in sorted(set(by_name_c) | set(by_name_l)):
            if fname not in by_name_c:
                lines.append(f"field: {name}.{fname} added")
            elif fname not in by_name_l:
                lines.append(f"field: {name}.{fname} removed")
            else:
                cf, lf = by_name_c[fname], by_name_l[fname]
                for attr in ("kind", "type", "count", "offset", "size"):
                    if _fmt_value(cf, attr) != _fmt_value(lf, attr):
                        lines.append(
                            f"field: {name}.{fname} {attr}: "
                            f"{_fmt_value(cf, attr)} -> {_fmt_value(lf, attr)}"
                        )
        order_c = [f["name"] for f in c_fields]
        order_l = [f["name"] for f in l_fields]
        if order_c != order_l:
            lines.append(f"field: {name} reordered")
    return sorted(set(lines))


def run_diff(install_dir: Path, fixtures_dir: Path) -> Tuple[int, List[str]]:
    """Compare committed fixtures against installed headers. See module docstring."""
    messages: List[str] = []
    saw_header = False
    worst = 0
    for header_name, _, fixture_name in HEADERS:
        live_path = header_path(install_dir, header_name)
        if live_path is None:
            continue
        saw_header = True
        fixture_path = fixtures_dir / fixture_name
        if not fixture_path.is_file():
            messages.append(f"fixture missing: {fixture_path.name} (run --generate)")
            worst = max(worst, 1)
            continue
        try:
            live_doc = build_document(
                header_name,
                _signature_for(header_name),
                live_path.read_bytes(),
                install_dir,
                None,
            )
        except (HeaderParseError, LayoutError) as exc:
            messages.append(f"{header_name}: cannot parse installed header: {exc}")
            worst = max(worst, 2)
            continue
        committed = load_fixture(fixture_path)
        drift = compare_documents(committed, live_doc)
        if drift:
            worst = max(worst, 1)
            messages.extend(drift)
    if not saw_header:
        messages.append("no installed header — using committed fixtures")
    return worst, messages


def _signature_for(header_name: str) -> str:
    for name, signature, _ in HEADERS:
        if name == header_name:
            return signature
    raise AssertionError(header_name)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="generate_sdk_layout_fixtures.py",
        description="Parse installed Afterburner SDK headers into committed layout fixtures.",
    )
    parser.add_argument("--install-dir", help="Afterburner install directory (default: detected)")
    parser.add_argument(
        "--afterburner-version", help="Override recorded install version provenance"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--generate", action="store_true", help="(Re)write the committed fixtures")
    group.add_argument("--diff", action="store_true", help="Compare fixtures against live headers")
    args = parser.parse_args(argv)

    install_dir = find_install_dir(args.install_dir)
    if install_dir is None:
        message = (
            "no Afterburner install detected (pass --install-dir)"
            if args.generate
            else "no installed header — using committed fixtures"
        )
        print(message)
        return 1 if args.generate else 0

    if args.generate:
        wrote = 0
        for header_name, signature, fixture_name in HEADERS:
            path = header_path(install_dir, header_name)
            if path is None:
                print(f"skip {header_name}: not present under {install_dir}\\SDK\\Include")
                continue
            try:
                doc = build_document(
                    header_name,
                    signature,
                    path.read_bytes(),
                    install_dir,
                    args.afterburner_version,
                )
            except (HeaderParseError, LayoutError) as exc:
                print(f"{header_name}: cannot parse installed header: {exc}")
                return 2  # exit-2 contract: header no longer parses under the supported subset
            target = FIXTURE_DIR / fixture_name
            atomic_write(target, serialize_doc(doc))
            print(f"wrote {target} "
                  f"(version {doc['from']['afterburner_version']}, "
                  f"sha256 {str(doc['from']['header_sha256'])[:12]}…)")
            wrote += 1
        if wrote == 0:
            print("no installed headers found under the detected install; nothing generated")
            return 1
        print("fixtures written. Review and commit them, then re-run the binding checks "
              "(plan tasks 18.4 / 20.3) and re-validate on real hardware.")
        return 0

    # --diff
    code, messages = run_diff(install_dir, FIXTURE_DIR)
    for line in messages:
        print(line)
    if code == 1:
        print("drift detected: re-run --generate, review and commit the new fixtures, "
              "re-run the binding checks (18.4 / 20.3), and re-validate on real hardware.")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
