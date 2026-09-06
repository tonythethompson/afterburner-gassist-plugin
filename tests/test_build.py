"""build.py deliverable-verification tests (plan task 22.2).

The verifier must fail with a per-item error naming each missing deliverable and pass
when all are present. Trees are fabricated in tmp dirs (valid manifest + plugin.py +
afterburner package + vendored SDK + a README satisfying the documented conventions),
so no real Afterburner install or vendored SDK is needed. Assembly tests additionally
prove the Protocol V2 layout is produced, forbidden Afterburner/RTSS payloads are
refused, and the --sdk / --allow-no-sdk / --force options behave.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

MANIFEST_TEMPLATE = {
    "manifestVersion": 1,
    "name": "afterburner",
    "version": "1.0.0",
    "description": "Test plugin.",
    "protocol_version": "2.0",
    "executable": "plugin.py",
    "persistent": True,
    "functions": [
        {
            "name": "get_gpu_status",
            "description": "Report GPU status.",
            "properties": {"gpu_index": {"type": "integer"}},
            "required": [],
        }
    ],
}


@pytest.fixture(scope="module")
def bld():
    spec = importlib.util.spec_from_file_location("ab_build", str(ROOT / "build.py"))
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register in sys.modules so dataclasses (and annotations) resolve against this module.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------------------
# tree helpers
# --------------------------------------------------------------------------------------


def write_readme(
    root: Path,
    *,
    install: bool = True,
    example_heading: bool = True,
    examples: int = 3,
    troubleshoot: bool = True,
) -> None:
    lines = ["# Plugin README"]
    if install:
        lines.append(
            "Copy the plugin folder into "
            "`%PROGRAMDATA%\\NVIDIA Corporation\\nvtopps\\rise\\plugins\\afterburner\\`."
        )
    if example_heading:
        lines.append("")
        lines.append("## Example prompts")
    for i in range(examples):
        lines.append(f'- "Example prompt number {i} for the GPU?"')
    if troubleshoot:
        lines.append("")
        lines.append("## Troubleshooting")
        lines.append("")
        lines.append("If the plugin reports an interface error, check Afterburner is running.")
    (root / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_tree(
    root: Path,
    *,
    build: bool = True,
    manifest: bool = True,
    executable: bool = True,
    package: bool = True,
    sdk: bool = True,
    readme_kwargs: dict | None = None,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    if build:
        (root / "build.py").write_text("# package script (self-check target)\n")
    if manifest:
        (root / "manifest.json").write_text(
            json.dumps(MANIFEST_TEMPLATE), encoding="utf-8"
        )
    if executable:
        (root / "plugin.py").write_text("# entry point\n", encoding="utf-8")
    if package:
        (root / "afterburner").mkdir(exist_ok=True)
        (root / "afterburner" / "__init__.py").write_text("", encoding="utf-8")
    (root / "libs").mkdir(exist_ok=True)
    (root / "libs" / "README.txt").write_text("placeholder\n", encoding="utf-8")
    if sdk:
        (root / "libs" / "gassist_sdk").mkdir(exist_ok=True)
        (root / "libs" / "gassist_sdk" / "__init__.py").write_text("", encoding="utf-8")
    write_readme(root, **(readme_kwargs or {}))
    return root


def keyed(results: list) -> dict:
    return {r.key: r for r in results}


# --------------------------------------------------------------------------------------
# deliverable checks: per-item naming + pass-when-present (task 22.2)
# --------------------------------------------------------------------------------------


class TestDeliverableVerification:
    def test_all_deliverables_present_passes(self, bld, tmp_path) -> None:
        root = make_tree(tmp_path / "ok")
        results = bld.check_deliverables(root, include_self=True, require_sdk=True)
        assert bld.failures(results) == []
        assert len(results) == 9  # build-script..config, incl. vendored-sdk

    @pytest.mark.parametrize(
        "key,breaker",
        [
            ("build-script", lambda r: (r / "build.py").unlink()),
            ("manifest", lambda r: (r / "manifest.json").unlink()),
            ("executable", lambda r: (r / "plugin.py").unlink()),
            ("package", lambda r: (r / "afterburner" / "__init__.py").unlink()),
            ("vendored-sdk", lambda r: shutil.rmtree(r / "libs" / "gassist_sdk")),
            (
                "install-instructions",
                lambda r: write_readme(r, install=False, troubleshoot=True, examples=3),
            ),
            (
                "example-prompts",
                lambda r: write_readme(r, install=True, troubleshoot=True, examples=2),
            ),
            (
                "troubleshooting",
                lambda r: write_readme(r, install=True, troubleshoot=False, examples=3),
            ),
        ],
    )
    def test_missing_deliverable_is_named(self, bld, tmp_path, key, breaker) -> None:
        root = make_tree(tmp_path / "tree")
        breaker(root)
        results = bld.check_deliverables(root, include_self=True, require_sdk=True)
        assert not keyed(results)[key].ok
        # every other required deliverable still passes (the report is per-item)
        others = {k: v for k, v in keyed(results).items() if k != key}
        assert all(v.ok for v in others.values()), others
        # the failure text names exactly this deliverable
        assert f"missing deliverable: {key}" in bld.format_failures(results)

    def test_example_prompts_require_heading(self, bld, tmp_path) -> None:
        root = make_tree(
            tmp_path / "tree", readme_kwargs={"example_heading": False, "examples": 3}
        )
        results = bld.check_deliverables(root)
        result = keyed(results)["example-prompts"]
        assert not result.ok and "heading" in result.detail

    def test_invalid_manifest_is_named(self, bld, tmp_path) -> None:
        root = make_tree(tmp_path / "tree")
        (root / "manifest.json").write_text("{not json", encoding="utf-8")
        results = bld.check_deliverables(root)
        result = keyed(results)["manifest"]
        assert not result.ok and "not valid JSON" in result.detail
        # executable falls back to plugin.py and still passes independently
        assert keyed(results)["executable"].ok

    def test_missing_declared_executable_is_named(self, bld, tmp_path) -> None:
        root = make_tree(tmp_path / "tree", executable=False)
        manifest = dict(MANIFEST_TEMPLATE, executable="launcher.py")
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        results = bld.check_deliverables(root)
        result = keyed(results)["executable"]
        assert not result.ok and "launcher.py" in result.detail

    def test_config_is_optional_but_validated_when_present(self, bld, tmp_path) -> None:
        root = make_tree(tmp_path / "absent")
        assert keyed(bld.check_deliverables(root))["config"].ok

        present = make_tree(tmp_path / "present")
        (present / "config.json").write_text('{"foo": 1}', encoding="utf-8")
        assert keyed(bld.check_deliverables(present))["config"].ok

        broken = make_tree(tmp_path / "broken")
        (broken / "config.json").write_text("{oops", encoding="utf-8")
        result = keyed(bld.check_deliverables(broken))["config"]
        assert not result.ok and "config" in bld.format_failures(
            bld.check_deliverables(broken)
        )

    def test_check_without_sdk_requirement_skips_sdk_item(self, bld, tmp_path) -> None:
        root = make_tree(tmp_path / "tree", sdk=False)
        results = bld.check_deliverables(root, require_sdk=False)
        assert bld.failures(results) == []
        assert "vendored-sdk" not in keyed(results)  # repo check, SDK not committed


# --------------------------------------------------------------------------------------
# assembly: install layout, exclusions, sdk handling, out-dir guard
# --------------------------------------------------------------------------------------


class TestAssemble:
    def test_assemble_creates_protocol_v2_layout(self, bld, tmp_path) -> None:
        source = make_tree(tmp_path / "src")
        (source / "afterburner" / "services").mkdir()
        (source / "afterburner" / "services" / "x.py").write_text("", encoding="utf-8")
        (source / "leftover.txt").write_text("not packaged\n", encoding="utf-8")
        out = tmp_path / "out"
        result = bld.assemble(source, out, verbose=False)
        assert result == out
        assert (out / "manifest.json").is_file()
        assert (out / "plugin.py").is_file()
        assert (out / "README.md").is_file()
        assert (out / "afterburner" / "__init__.py").is_file()
        assert (out / "afterburner" / "services" / "x.py").is_file()
        assert (out / "libs" / "README.txt").is_file()
        assert (out / "libs" / "gassist_sdk" / "__init__.py").is_file()
        # whitelist copy: nothing else sneaks in
        assert not (out / "leftover.txt").exists()
        assert not (out / "build.py").exists()
        assert not list(out.rglob("__pycache__"))
        # the artifact itself verifies clean
        final = bld.check_deliverables(out, include_self=False, require_sdk=True)
        assert bld.failures(final) == []

    def test_assemble_refuses_forbidden_afterburner_binaries(self, bld, tmp_path) -> None:
        source = make_tree(tmp_path / "src")
        (source / "libs" / "gassist_sdk" / "RTSS.exe").write_bytes(b"MZ")
        (source / "libs" / "gassist_sdk" / "MSIAfterburner").write_bytes(b"MZ")
        out = tmp_path / "out"
        with pytest.raises(bld.PackagingError) as exc_info:
            bld.assemble(source, out, verbose=False)
        message = str(exc_info.value)
        assert exc_info.value.exit_code == 2
        assert "RTSS.exe" in message and "MSIAfterburner" in message
        assert not out.exists()  # partial artifact removed

    def test_assemble_requires_vendored_sdk(self, bld, tmp_path) -> None:
        source = make_tree(tmp_path / "src", sdk=False)
        out = tmp_path / "out"
        with pytest.raises(bld.PackagingError) as exc_info:
            bld.assemble(source, out, verbose=False)
        assert "missing deliverable: vendored-sdk" in str(exc_info.value)
        assert exc_info.value.exit_code == 1
        assert not out.exists()

        relaxed = tmp_path / "relaxed"
        bld.assemble(source, relaxed, allow_no_sdk=True, verbose=False)
        assert relaxed.is_dir() and not (relaxed / "libs" / "gassist_sdk").exists()

    def test_assemble_sdk_override_is_vendored(self, bld, tmp_path) -> None:
        source = make_tree(tmp_path / "src", sdk=False)
        sdk_copy = tmp_path / "my-sdk"
        sdk_copy.mkdir()
        (sdk_copy / "__init__.py").write_text("", encoding="utf-8")
        (sdk_copy / "sdk.py").write_text("", encoding="utf-8")
        out = tmp_path / "out"
        bld.assemble(source, out, sdk_dir=str(sdk_copy), verbose=False)
        assert (out / "libs" / "gassist_sdk" / "__init__.py").is_file()
        assert (out / "libs" / "gassist_sdk" / "sdk.py").is_file()

    def test_existing_out_requires_force(self, bld, tmp_path) -> None:
        source = make_tree(tmp_path / "src")
        out = tmp_path / "out"
        out.mkdir()
        (out / "stale.txt").write_text("old", encoding="utf-8")
        with pytest.raises(bld.PackagingError) as exc_info:
            bld.assemble(source, out, verbose=False)
        assert exc_info.value.exit_code == 2 and "already exists" in str(exc_info.value)
        bld.assemble(source, out, force=True, verbose=False)
        assert not (out / "stale.txt").exists()
        assert (out / "manifest.json").is_file()


class TestCli:
    def test_check_exit_codes(self, bld, tmp_path) -> None:
        ok = make_tree(tmp_path / "ok")
        assert bld.run(["--check", "--root", str(ok)]) == 0

        missing = make_tree(tmp_path / "missing")
        (missing / "plugin.py").unlink()
        assert bld.run(["--check", "--root", str(missing)]) == 1

        missing_docs = make_tree(
            tmp_path / "missing-docs", readme_kwargs={"troubleshoot": False}
        )
        assert bld.run(["--check", "--root", str(missing_docs)]) == 1

    def test_assemble_cli_exit_codes(self, bld, tmp_path) -> None:
        ok = make_tree(tmp_path / "ok")
        out = tmp_path / "cli-out"
        assert bld.run(["--root", str(ok), "--out", str(out), "--quiet"]) == 0
        assert (out / "manifest.json").is_file()

        missing_docs = make_tree(
            tmp_path / "missing-docs", readme_kwargs={"examples": 1}
        )
        assert (
            bld.run(
                ["--root", str(missing_docs), "--out", str(tmp_path / "nd"), "--quiet"]
            )
            == 1
        )
