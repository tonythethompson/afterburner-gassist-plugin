"""Build/package script with per-item deliverable verification (plan task 22.1).

Assembles the plugin into the Protocol V2 install layout and refuses to produce an
artifact until every deliverable is present, naming each one that is missing:

    manifest.json + plugin.py + afterburner/ + vendored libs/gassist_sdk
    + README.md (install instructions, >=3 example prompts, troubleshooting)

No Afterburner/RTSS (or any third-party) binaries are ever packaged: the copy list is
an explicit whitelist, and the assembled tree is scanned for forbidden payloads.

Deliverables verified (each failure is reported as ``missing deliverable: <key>``):

- ``build-script``        -- this file (build.py) at the project root (self-check).
- ``manifest``            -- manifest.json parses and has the Protocol V2 required keys.
- ``executable``          -- the file manifest.json names as ``executable`` exists.
- ``package``             -- the afterburner/ package (afterburner/__init__.py).
- ``vendored-sdk``        -- libs/gassist_sdk/ present and non-empty. Required only when
                             assembling an artifact (the SDK is deliberately NOT committed
                             -- see libs/README.txt); pass ``--sdk <path>`` to vendor it
                             from elsewhere, or ``--allow-no-sdk`` for dev-only assembly.
- ``install-instructions``-- README.md documents the Protocol V2 install path
                             (``nvtopps\\rise\\plugins\\afterburner``).
- ``example-prompts``     -- README.md lists at least 3 example G-Assist prompts as quoted
                             bullets under an "Example prompts" heading.
- ``troubleshooting``     -- README.md has a troubleshooting section (heading containing
                             "troubleshoot").
- ``config``              -- OPTIONAL: this plugin does not use config.json. When present it
                             must parse as JSON (checked); absence is success.

README.md conventions (doc task 23 must satisfy these exactly):

- Install instructions: a paragraph/step naming the install path
  ``%PROGRAMDATA%\\NVIDIA Corporation\\nvtopps\\rise\\plugins\\afterburner\\``.
- Example prompts: a markdown heading matching "example prompts?" followed by at least
  three bullet lines that contain a quoted prompt, e.g.::

      ## Example prompts

      - "What's my GPU temperature?"
      - "Make my GPU quieter."
      - "Why is my GPU clock dropping?"

- Troubleshooting: a markdown heading containing "troubleshoot", e.g. ``## Troubleshooting``.

Exit codes (CI-contract): 0 = verified/assembled; 1 = missing deliverable(s) (per-item
errors printed); 2 = packaging error (forbidden binary found, output dir exists without
--force, I/O failure).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------------------
# README conventions (the checks task 23's README must satisfy)
# --------------------------------------------------------------------------------------

INSTALL_PATH_RE = re.compile(
    r"nvtopps[\\/]{1,2}rise[\\/]{1,2}plugins[\\/]{1,2}afterburner", re.IGNORECASE
)
EXAMPLES_HEADING_RE = re.compile(r"(?im)^#+\s+.*\bexample\s+prompts?\b")
EXAMPLE_BULLET_RE = re.compile(r"(?m)^\s*[-*]\s+[^\n]*[\"\u201c]")
TROUBLESHOOT_HEADING_RE = re.compile(r"(?im)^#+\s+.*\btroubleshoot")
MIN_EXAMPLE_PROMPTS = 3

# Manifest Protocol V2 top-level keys the packaged artifact must carry.
MANIFEST_REQUIRED_KEYS = (
    "manifestVersion",
    "name",
    "version",
    "description",
    "protocol_version",
    "executable",
    "persistent",
    "functions",
)

# Whitelist copied verbatim into the artifact root.
_ARTIFACT_FILES = ("manifest.json", "plugin.py", "README.md")

# Forbidden payloads: after assembly, any file that looks like a redistributable binary,
# or is named like an Afterburner/RTSS artifact, causes the build to fail naming the file.
_BINARY_EXTENSIONS = (
    ".exe",
    ".dll",
    ".sys",
    ".ocx",
    ".scr",
    ".cpl",
    ".com",
    ".msi",
    ".cab",
)
_FORBIDDEN_BASENAMES = (
    "msiafterburner.exe",
    "msiafterburner_x64.exe",
    "mactray.exe",
    "rtss.exe",
    "rivatunerstatisticsserver.exe",
    "encoderserver.exe",
    "rtsshooks.dll",
    "rtsshooks64.dll",
    "rtsshooks_d3d9.dll",
    "rtsshooks_d3d10.dll",
    "rtsshooks_d3d11.dll",
    "rtsshooks_ogl.dll",
    "rtsshooks_vulkan.dll",
    "rtssharedmemory.dll",
    "afterburnerhooks.dll",
    "encoder.dll",
)


@dataclass(frozen=True)
class CheckResult:
    """One deliverable check."""

    key: str  # stable machine-readable id (named in failure output)
    ok: bool
    detail: str


class PackagingError(Exception):
    """Build failure. ``exit_code`` follows the CLI contract (1 or 2)."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


# --------------------------------------------------------------------------------------
# Deliverable checks
# --------------------------------------------------------------------------------------


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _check_manifest(root: Path) -> CheckResult:
    path = root / "manifest.json"
    raw = _read_text(path)
    if not raw:
        return CheckResult(
            "manifest", False, "manifest.json is missing at the project root"
        )
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return CheckResult("manifest", False, f"manifest.json is not valid JSON: {exc}")
    missing = [k for k in MANIFEST_REQUIRED_KEYS if k not in data]
    if missing:
        return CheckResult(
            "manifest",
            False,
            f"manifest.json is missing Protocol V2 keys: {', '.join(missing)}",
        )
    if not isinstance(data["functions"], list) or not data["functions"]:
        return CheckResult(
            "manifest", False, "manifest.json must declare a non-empty functions list"
        )
    if not isinstance(data["executable"], str) or not data["executable"]:
        return CheckResult(
            "manifest", False, "manifest.json executable must be a non-empty string"
        )
    return CheckResult("manifest", True, "manifest.json present and Protocol V2 shape valid")


def _check_executable(root: Path) -> CheckResult:
    raw = _read_text(root / "manifest.json")
    executable = "plugin.py"
    if raw:
        try:
            executable = json.loads(raw).get("executable", executable)
        except ValueError:
            pass
    if (root / executable).is_file():
        return CheckResult("executable", True, f"{executable} present")
    return CheckResult(
        "executable",
        False,
        f"{executable} (manifest.json's executable) is missing at the project root",
    )


def _check_package(root: Path) -> CheckResult:
    if (root / "afterburner" / "__init__.py").is_file():
        return CheckResult("package", True, "afterburner/ package present")
    return CheckResult(
        "package", False, "afterburner/ package missing (no afterburner/__init__.py)"
    )


def _check_vendored_sdk(root: Path) -> CheckResult:
    sdk = root / "libs" / "gassist_sdk"
    if sdk.is_dir() and any(sdk.iterdir()):
        return CheckResult("vendored-sdk", True, "libs/gassist_sdk/ present")
    return CheckResult(
        "vendored-sdk",
        False,
        "libs/gassist_sdk/ missing or empty -- vendor the official SDK there "
        "(see libs/README.txt) or pass --sdk <path>",
    )


def _check_install_instructions(readme: str) -> CheckResult:
    if INSTALL_PATH_RE.search(readme):
        return CheckResult(
            "install-instructions",
            True,
            "README.md documents the Protocol V2 install path "
            "(nvtopps\\rise\\plugins\\afterburner)",
        )
    return CheckResult(
        "install-instructions",
        False,
        "README.md must document the Protocol V2 install path "
        "(nvtopps\\rise\\plugins\\afterburner)",
    )


def _check_example_prompts(readme: str) -> CheckResult:
    if not EXAMPLES_HEADING_RE.search(readme):
        return CheckResult(
            "example-prompts",
            False,
            "README.md must include an \"Example prompts\" section heading",
        )
    count = len(EXAMPLE_BULLET_RE.findall(readme))
    if count >= MIN_EXAMPLE_PROMPTS:
        return CheckResult(
            "example-prompts",
            True,
            f"README.md lists {count} example prompts (>= {MIN_EXAMPLE_PROMPTS})",
        )
    return CheckResult(
        "example-prompts",
        False,
        f"README.md must list at least {MIN_EXAMPLE_PROMPTS} example prompts as quoted "
        f"bullets (found {count})",
    )


def _check_troubleshooting(readme: str) -> CheckResult:
    if TROUBLESHOOT_HEADING_RE.search(readme):
        return CheckResult(
            "troubleshooting",
            True,
            "README.md has a troubleshooting section",
        )
    return CheckResult(
        "troubleshooting",
        False,
        "README.md must include a troubleshooting section "
        "(a heading containing \"troubleshoot\")",
    )


def _check_config(root: Path) -> CheckResult:
    path = root / "config.json"
    if not path.exists():
        return CheckResult(
            "config", True, "config.json absent -- this plugin does not use one (optional)"
        )
    raw = _read_text(path)
    try:
        json.loads(raw)
    except ValueError as exc:
        return CheckResult("config", False, f"config.json is not valid JSON: {exc}")
    return CheckResult("config", True, "config.json present and valid")


def check_deliverables(
    root: Path, *, include_self: bool = True, require_sdk: bool = True
) -> list[CheckResult]:
    """Run every deliverable check against ``root``.

    ``include_self`` adds the build-script self-check (meaningful only at the source
    project root). ``require_sdk`` gates the vendored-SDK check (a build-time input --
    the SDK is not committed to the repo, see libs/README.txt).
    """
    readme = _read_text(root / "README.md")
    results: list[CheckResult] = []
    if include_self:
        if (root / "build.py").is_file():
            results.append(CheckResult("build-script", True, "build.py present"))
        else:
            results.append(
                CheckResult(
                    "build-script", False, "build.py (the package script) is missing"
                )
            )
    results.append(_check_manifest(root))
    results.append(_check_executable(root))
    results.append(_check_package(root))
    if require_sdk:
        results.append(_check_vendored_sdk(root))
    results.append(_check_install_instructions(readme))
    results.append(_check_example_prompts(readme))
    results.append(_check_troubleshooting(readme))
    results.append(_check_config(root))
    return results


def failures(results: list[CheckResult]) -> list[CheckResult]:
    return [r for r in results if not r.ok]


def format_failures(results: list[CheckResult]) -> str:
    """Per-item errors, one line each, naming the missing deliverable (task 22.1)."""
    return "\n".join(
        f"missing deliverable: {r.key} -- {r.detail}" for r in failures(results)
    )


# --------------------------------------------------------------------------------------
# Forbidden-binary scan
# --------------------------------------------------------------------------------------


def _is_forbidden_name(name: str) -> bool:
    folded = name.casefold()
    if folded in _FORBIDDEN_BASENAMES:
        return True
    return (
        folded.startswith("rtss")
        or "msiafterburner" in folded
        or "rivatuner" in folded
    )


def forbidden_files(root: Path) -> list[str]:
    """Absolute paths under ``root`` that look like Afterburner/RTSS or binary payloads."""
    bad: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() in _BINARY_EXTENSIONS or _is_forbidden_name(path.name):
            bad.append(str(path))
    return bad


# --------------------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------------------


def _ignore_pycache(directory: str, names: list[str]) -> set[str]:
    return {n for n in names if n == "__pycache__" or n.endswith(".pyc")}


def _copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _copy_tree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, ignore=_ignore_pycache)


def assemble(
    source: Path,
    out: Path,
    *,
    sdk_dir: Path | None = None,
    allow_no_sdk: bool = False,
    force: bool = False,
    verbose: bool = True,
) -> Path:
    """Verify deliverables, then assemble ``source`` into the Protocol V2 layout at ``out``.

    Raises PackagingError (exit code 1) naming each missing deliverable, or (exit code 2)
    on a packaging error (existing output dir without --force, forbidden binary, I/O).
    """
    source = Path(source)
    out = Path(out)

    results = check_deliverables(source, include_self=True, require_sdk=True)
    if sdk_dir is not None:
        sdk_override = Path(sdk_dir)
        results = [
            CheckResult(
                "vendored-sdk",
                sdk_override.is_dir() and any(sdk_override.iterdir()),
                f"using --sdk {sdk_override}",
            )
            if r.key == "vendored-sdk"
            else r
            for r in results
        ]
    missing = failures(results)
    if allow_no_sdk:
        missing = [r for r in missing if r.key != "vendored-sdk"]
    if missing:
        raise PackagingError(
            "cannot package -- missing deliverables:\n" + format_failures(missing)
        )

    if out.exists():
        if not force:
            raise PackagingError(
                f"output directory {out} already exists; remove it or pass --force",
                exit_code=2,
            )
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    try:
        for name in _ARTIFACT_FILES:
            src = source / name
            if src.is_file():
                _copy_file(src, out / name)
        if (source / "LICENSE").is_file():
            _copy_file(source / "LICENSE", out / "LICENSE")

        # afterburner/ package: Python sources only (no __pycache__, no stray data).
        package_src = source / "afterburner"
        if package_src.is_dir():
            for py in sorted(package_src.rglob("*.py")):
                _copy_file(py, out / "afterburner" / py.relative_to(package_src))

        # libs/: README.txt + anything present except gassist_sdk (handled below).
        libs_src = source / "libs"
        if libs_src.is_dir():
            for item in sorted(libs_src.iterdir()):
                if item.name == "gassist_sdk":
                    continue
                if item.is_dir():
                    _copy_tree(item, out / "libs" / item.name)
                else:
                    _copy_file(item, out / "libs" / item.name)

        # Vendored SDK (explicit override, or the repo's libs/ copy).
        sdk_source = Path(sdk_dir) if sdk_dir is not None else source / "libs" / "gassist_sdk"
        if sdk_source.is_dir() and any(sdk_source.iterdir()):
            _copy_tree(sdk_source, out / "libs" / "gassist_sdk")
    except OSError as exc:
        shutil.rmtree(out, ignore_errors=True)
        raise PackagingError(f"failed to assemble the plugin: {exc}", exit_code=2) from exc

    # Safety scan of the finished tree: refuse to ship Afterburner/RTSS or any binary.
    bad = forbidden_files(out)
    if bad:
        shutil.rmtree(out, ignore_errors=True)
        raise PackagingError(
            "refusing to package Afterburner/RTSS or binary payloads:\n  "
            + "\n  ".join(bad),
            exit_code=2,
        )

    # Structural guarantee: the artifact itself passes every deliverable check.
    final = check_deliverables(out, include_self=False, require_sdk=not allow_no_sdk)
    final_missing = failures(final)
    if final_missing:
        shutil.rmtree(out, ignore_errors=True)
        raise PackagingError(
            "assembled artifact failed verification:\n" + format_failures(final_missing)
        )

    if verbose:
        n_files = sum(1 for p in out.rglob("*") if p.is_file())
        print(f"assembled {n_files} files into {out}")
        print(f"verified: {len(final)}/{len(final)} deliverables present")
    return out


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the deliverables at --root and exit (0 ok / 1 missing) without assembling",
    )
    parser.add_argument(
        "--root", default=".", help="project root to verify/assemble from (default: .)"
    )
    parser.add_argument(
        "--out",
        default=None,
        help="output plugin folder (default: dist/afterburner under the current directory)",
    )
    parser.add_argument(
        "--sdk",
        default=None,
        help="path to a gassist_sdk copy to vendor into libs/gassist_sdk",
    )
    parser.add_argument(
        "--allow-no-sdk",
        action="store_true",
        help="assemble without the vendored SDK (dev/testing only -- the stdlib transport "
        "fallback applies; not for distribution)",
    )
    parser.add_argument(
        "--force", action="store_true", help="replace an existing output directory"
    )
    parser.add_argument("--quiet", action="store_true", help="suppress progress output")
    args = parser.parse_args(argv)

    root = Path(args.root)
    quiet = args.quiet

    if args.check:
        results = check_deliverables(root, include_self=True, require_sdk=False)
        missing = failures(results)
        if quiet:
            return 1 if missing else 0
        if missing:
            print(f"verification FAILED for {root}:\n" + format_failures(results))
            print(f"{len(missing)} deliverable(s) missing -- fix and re-run build.py --check")
        else:
            print(f"verification PASSED for {root}: all {len(results)} deliverables present")
        return 1 if missing else 0

    out = Path(args.out) if args.out else Path("dist") / "afterburner"
    try:
        assemble(
            root,
            out,
            sdk_dir=args.sdk,
            allow_no_sdk=args.allow_no_sdk,
            force=args.force,
            verbose=not quiet,
        )
    except PackagingError as exc:
        if not quiet:
            print(str(exc))
        return exc.exit_code
    return 0


def main() -> None:
    sys.exit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
