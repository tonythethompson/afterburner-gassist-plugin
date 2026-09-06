"""Committed SDK-layout fixtures: schema, strict loader, layout rules.

Single source of truth for the `sdk-layout/1` fixture format (design § Fixture JSON
shape, normative) shared by every consumer:

- `tools/generate_sdk_layout_fixtures.py` — produces fixtures and diffs live headers;
- the fixture-consistency CI test (plan task 18.3) — replays the layout rules;
- the ctypes binding checks (plan tasks 18.4 / 20.3) — compare bindings against the
  committed layout.

The layout rules below (`compute_layout`) are deliberately a *pure* replay of the recorded
field declarations against the documented MSVC natural-alignment rules, so the generator and
the consistency test call the same helper and can never silently diverge. This module is
tooling/test support — the runtime monitoring/control clients do not import it.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

SCHEMA_VERSION = "sdk-layout/1"
HEADER_ENUM = ("MAHMSharedMemory.h", "MACMSharedMemory.h")
SIGNATURE_ENUM = ("MAHM", "MACM")
INTERFACE_VERSION = "0x00020000"

# Leaf type token -> (alignment, size) per design: DWORD/LONG/time_t/__time32_t/float are
# 4-byte, 4-aligned. `char` is handled through the `chars` kind (alignment 1, size = count).
LEAF_ALIGN_SIZE: Mapping[str, Tuple[int, int]] = {
    "DWORD": (4, 4),
    "LONG": (4, 4),
    "float": (4, 4),
    "time_t": (4, 4),
    "__time32_t": (4, 4),
}
LEAF_VOCABULARY = frozenset(LEAF_ALIGN_SIZE)

# Platform constants the Afterburner headers reference but do not define.
PLATFORM_CONSTANTS: Mapping[str, int] = {"MAX_PATH": 260}

_FIELD_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HEX_RE = re.compile(r"^0x[0-9a-f]{8}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class LayoutError(ValueError):
    """Fixture violates the schema, cross-field invariants, or the layout rules."""


# ---------------------------------------------------------------------------
# Canonical serialization
# ---------------------------------------------------------------------------


def canonical_hex(value: int) -> str:
    """Normalize a #define value to canonical unsigned-32-bit hex (`0x` + 8 lowercase)."""
    return f"0x{int(value) & 0xFFFFFFFF:08x}"


def parse_hex(text: str) -> int:
    """Parse a canonical hex string back to an int (numeric diffs, never text diffs)."""
    if not _HEX_RE.match(text):
        raise LayoutError(f"not canonical hex: {text!r}")
    return int(text, 16)


def serialize_doc(doc: Mapping[str, object]) -> str:
    """Deterministic canonical serialization: sorted keys, LF endings, trailing newline."""
    text = json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=True)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.endswith("\n"):
        text += "\n"
    return text


# ---------------------------------------------------------------------------
# The normative JSON Schema (draft-07), verbatim from design § Fixture JSON shape.
# A tiny evaluator for exactly the keyword subset the schema uses keeps this dependency-free.
# ---------------------------------------------------------------------------

_SCHEMA_JSON = r"""{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "$id": "afterburner-gassist-plugin/sdk-layout-v1",
  "title": "SDK shared-memory layout fixture",
  "type": "object",
  "required": ["schema", "header", "signature", "version", "from", "constants", "structs"],
  "additionalProperties": false,
  "properties": {
    "schema": { "const": "sdk-layout/1" },
    "header": { "enum": ["MAHMSharedMemory.h", "MACMSharedMemory.h"] },
    "signature": { "enum": ["MAHM", "MACM"] },
    "version": { "const": "0x00020000" },
    "from": {
      "type": "object",
      "required": ["afterburner_version", "header_sha256"],
      "additionalProperties": false,
      "properties": {
        "afterburner_version": { "type": "string" },
        "header_sha256": { "type": "string", "pattern": "^[0-9a-f]{64}$" }
      }
    },
    "constants": {
      "type": "object",
      "additionalProperties": { "type": "string", "pattern": "^0x[0-9a-f]{8}$" }
    },
    "structs": {
      "type": "object",
      "additionalProperties": { "$ref": "#/definitions/struct" }
    }
  },
  "definitions": {
    "struct": {
      "type": "object",
      "required": ["alignment", "size", "fields"],
      "additionalProperties": false,
      "properties": {
        "alignment": { "type": "integer", "minimum": 1 },
        "size": { "type": "integer", "minimum": 1 },
        "fields": { "type": "array", "items": { "$ref": "#/definitions/field" } }
      }
    },
    "field": {
      "type": "object",
      "required": ["name", "kind", "type", "offset", "size"],
      "additionalProperties": false,
      "properties": {
        "name": { "type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_]*$" },
        "kind": { "enum": ["scalar", "chars", "struct", "struct-array"] },
        "type": { "type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_]*$" },
        "count": { "type": "integer", "minimum": 1 },
        "offset": { "type": "integer", "minimum": 0 },
        "size": { "type": "integer", "minimum": 1 }
      }
    }
  }
}
"""

SCHEMA: Dict[str, object] = json.loads(_SCHEMA_JSON)


def _type_matches(value: object, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "boolean":
        return isinstance(value, bool)
    return False


def _validate_schema(value: object, node: object, path: str) -> None:
    """Evaluate the subset of draft-07 used by SCHEMA against a value."""
    if not isinstance(node, dict):
        return

    if "type" in node and not _type_matches(value, node["type"]):
        raise LayoutError(f"{path}: expected {node['type']}, got {type(value).__name__}")
    if "const" in node and value != node["const"]:
        raise LayoutError(f"{path}: expected {node['const']!r}, got {value!r}")
    if "enum" in node and value not in node["enum"]:
        raise LayoutError(f"{path}: expected one of {node['enum']}, got {value!r}")
    if "pattern" in node and isinstance(value, str) and not re.match(node["pattern"], value):
        raise LayoutError(f"{path}: {value!r} does not match {node['pattern']!r}")
    if "minimum" in node and isinstance(value, int) and not isinstance(value, bool):
        if value < node["minimum"]:
            raise LayoutError(f"{path}: {value} < minimum {node['minimum']}")

    if node.get("type") == "object" and isinstance(value, dict):
        props = node.get("properties", {})
        for key, subschema in props.items():
            if key in value:
                _validate_schema(value[key], subschema, f"{path}.{key}")
        for key in node.get("required", []):
            if key not in value:
                raise LayoutError(f"{path}: missing required key {key!r}")
        additional = node.get("additionalProperties")
        if additional is False:
            extra = [k for k in value if k not in props]
            if extra:
                raise LayoutError(f"{path}: unknown key(s) {extra}")
        elif isinstance(additional, dict):
            for key, item in value.items():
                if key not in props:
                    _validate_schema(item, additional, f"{path}.{key}")
    elif node.get("type") == "array" and isinstance(value, list):
        items = node.get("items")
        if isinstance(items, dict):
            for i, item in enumerate(value):
                _validate_schema(item, items, f"{path}[{i}]")

    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/definitions/"):
        _validate_schema(value, SCHEMA["definitions"][ref.split("/")[-1]], path)


# ---------------------------------------------------------------------------
# Cross-field invariants (beyond what the schema can express)
# ---------------------------------------------------------------------------


def _field_decl(field: Mapping[str, object]) -> None:
    kind = field["kind"]
    name = field["name"]
    ftype = field["type"]
    count = field.get("count")
    size = field["size"]

    if kind == "scalar":
        if count is not None:
            raise LayoutError(f"field {name}: scalar must not carry a count")
        if ftype not in LEAF_VOCABULARY:
            raise LayoutError(f"field {name}: unknown scalar type {ftype!r}")
        if size != 4:
            raise LayoutError(f"field {name}: scalar size must be 4, got {size}")
    elif kind == "chars":
        if ftype != "char":
            raise LayoutError(f"field {name}: chars kind must have type 'char', got {ftype!r}")
        if count is None:
            raise LayoutError(f"field {name}: chars requires a count")
        if size != count:
            raise LayoutError(f"field {name}: chars size must equal count ({count}), got {size}")
    elif kind in ("struct", "struct-array"):
        if kind == "struct" and count is not None:
            raise LayoutError(f"field {name}: struct must not carry a count")
        if kind == "struct-array" and count is None:
            raise LayoutError(f"field {name}: struct-array requires a count")


def _validate_invariants(doc: Mapping[str, object]) -> None:
    structs = doc["structs"]
    assert isinstance(structs, dict)
    constants = doc["constants"]
    assert isinstance(constants, dict)
    for const_name, value in constants.items():
        if not _FIELD_NAME_RE.match(str(const_name)):
            raise LayoutError(f"constant {const_name!r}: invalid name")
        parse_hex(str(value))

    if not structs:
        raise LayoutError("structs: at least one struct is required")
    for struct_name, struct in structs.items():
        if not _FIELD_NAME_RE.match(str(struct_name)):
            raise LayoutError(f"struct {struct_name!r}: invalid name")
        assert isinstance(struct, dict)
        fields = struct["fields"]
        if not fields:
            raise LayoutError(f"struct {struct_name}: must declare at least one field")
        seen: set = set()
        for field in fields:
            assert isinstance(field, dict)
            _field_decl(field)
            if field["name"] in seen:
                raise LayoutError(f"struct {struct_name}: duplicate field {field['name']!r}")
            seen.add(field["name"])
            kind = field["kind"]
            ftype = field["type"]
            if kind in ("struct", "struct-array") and ftype not in structs:
                raise LayoutError(
                    f"struct {struct_name}.{field['name']}: type {ftype!r} not in structs"
                )
            # kind-rule sizes using the *recorded* referenced struct sizes.
            if kind == "struct":
                if field["size"] != structs[ftype]["size"]:
                    raise LayoutError(
                        f"struct {struct_name}.{field['name']}: size must equal {ftype} size"
                    )
            elif kind == "struct-array":
                if field["size"] != field["count"] * structs[ftype]["size"]:
                    raise LayoutError(
                        f"struct {struct_name}.{field['name']}: size must equal "
                        f"count × {ftype} size"
                    )


def validate_doc(doc: object) -> Dict[str, object]:
    """Validate an in-memory fixture document (schema + cross-field invariants)."""
    _validate_schema(doc, SCHEMA, "$")
    assert isinstance(doc, dict)
    _validate_invariants(doc)
    return doc


def load_fixture(path) -> Dict[str, object]:
    """Strict loader: parse, schema-validate, invariant-check. Rejects unknown keys."""
    raw = Path(path).read_bytes()
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LayoutError(f"{path}: invalid JSON: {exc}") from exc
    return validate_doc(doc)


# ---------------------------------------------------------------------------
# Layout rules — the single shared helper (generator + consistency test)
# ---------------------------------------------------------------------------


def _align_up(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment


@dataclass(frozen=True)
class Layout:
    """Result of replaying the MSVC natural-alignment rules over a struct's fields."""

    alignment: int
    size: int
    offsets: Mapping[str, int]  # field name -> byte offset, in declaration order


FieldDecl = Mapping[str, object]  # keys: name, kind, type[, count]
FieldResolver = Callable[[str], Sequence[FieldDecl]]


def compute_layout(name: str, fields_of: FieldResolver) -> Layout:
    """Compute offset/size/alignment from field declarations (MSVC natural alignment).

    Recursively resolves `struct` / `struct-array` members through `fields_of`, so children
    must ultimately be present in the resolver. Raises `LayoutError` on cycles, unknown leaf
    types, or malformed kinds.
    """

    memo: Dict[str, Layout] = {}
    visiting: set = set()

    def _compute(struct_name: str) -> Layout:
        if struct_name in memo:
            return memo[struct_name]
        if struct_name in visiting:
            raise LayoutError(f"struct {struct_name}: cyclic struct reference")
        visiting.add(struct_name)
        try:
            fields = list(fields_of(struct_name))
            if not fields:
                raise LayoutError(f"struct {struct_name}: no fields to lay out")
            cursor = 0
            max_align = 1
            offsets: Dict[str, int] = {}
            for field in fields:
                kind = field["kind"]
                ftype = field["type"]
                count = field.get("count")
                if kind == "scalar":
                    if ftype not in LEAF_ALIGN_SIZE:
                        raise LayoutError(
                            f"struct {struct_name}.{field['name']}: unknown scalar type "
                            f"{ftype!r}"
                        )
                    elem_align, elem_size = LEAF_ALIGN_SIZE[ftype]
                elif kind == "chars":
                    elem_align, elem_size = 1, count
                elif kind == "struct":
                    child = _compute(str(ftype))
                    elem_align, elem_size = child.alignment, child.size
                elif kind == "struct-array":
                    child = _compute(str(ftype))
                    if count is None:
                        raise LayoutError(
                            f"struct {struct_name}.{field['name']}: array requires a count"
                        )
                    elem_align, elem_size = child.alignment, count * child.size
                else:
                    raise LayoutError(
                        f"struct {struct_name}.{field['name']}: unknown kind {kind!r}"
                    )
                max_align = max(max_align, elem_align)
                offset = _align_up(cursor, elem_align)
                offsets[str(field["name"])] = offset
                cursor = offset + elem_size
            result = Layout(
                alignment=max_align, size=_align_up(cursor, max_align), offsets=offsets
            )
            memo[struct_name] = result
            return result
        finally:
            visiting.remove(struct_name)

    return _compute(name)


def fixture_fields_of(doc: Mapping[str, object]) -> FieldResolver:
    """Resolver adapter: field declarations straight from a fixture document."""

    def _fields(struct_name: str) -> Sequence[FieldDecl]:
        structs = doc["structs"]
        assert isinstance(structs, dict)
        if struct_name not in structs:
            raise LayoutError(f"struct {struct_name}: not recorded in fixture")
        fields = structs[struct_name]["fields"]
        assert isinstance(fields, list)
        return [dict(f) for f in fields]  # type: ignore[arg-type]

    return _fields


def consistency_issues(doc: Mapping[str, object]) -> Sequence[str]:
    """Replay the layout rules over the recorded declarations; report every mismatch.

    Catches corrupted, truncated, or hand-edited fixtures: recorded offsets/alignment/size
    that contradict a replay of the rules, fields out of declaration-order offsets, or a
    first field that does not start at offset 0.
    """
    issues: List[str] = []
    structs = doc["structs"]
    assert isinstance(structs, dict)
    fields_of = fixture_fields_of(doc)
    for struct_name, struct in structs.items():
        layout = compute_layout(str(struct_name), fields_of)
        assert isinstance(struct, dict)
        if layout.alignment != struct["alignment"]:
            issues.append(
                f"{struct_name}: alignment {struct['alignment']} != replay {layout.alignment}"
            )
        if layout.size != struct["size"]:
            issues.append(f"{struct_name}: size {struct['size']} != replay {layout.size}")
        fields = struct["fields"]
        assert isinstance(fields, list)
        prev_offset = -1
        for field in fields:
            name = field["name"]
            recorded = field["offset"]
            replayed = layout.offsets[str(name)]
            if recorded != replayed:
                issues.append(f"{struct_name}.{name}: offset {recorded} != replay {replayed}")
            if recorded <= prev_offset:
                issues.append(
                    f"{struct_name}.{name}: offset {recorded} not strictly increasing "
                    f"(previous {prev_offset})"
                )
            prev_offset = recorded
            if recorded + field["size"] > struct["size"]:
                issues.append(
                    f"{struct_name}.{name}: offset+size exceeds struct size "
                    f"({recorded + field['size']} > {struct['size']})"
                )
    return issues


def check_consistency(doc: Mapping[str, object]) -> None:
    issues = consistency_issues(doc)
    if issues:
        raise LayoutError("fixture layout inconsistent:\n  " + "\n  ".join(issues))
