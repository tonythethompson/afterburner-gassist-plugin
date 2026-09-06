"""Install the built plugin into the real G-Assist RISE runtime and smoke it with its python.

Run this once the G-Assist runtime has deployed (activate/opt into Project G-Assist in
NVIDIA App so NVIDIA App materializes the runtime it already staged). It:

1. Locates the RISE runtime (env ``RISE_ROOT`` override, then the standard
   ``%PROGRAMDATA%\\NVIDIA Corporation\\nvtopps\\rise``) and fails with guidance when it is
   not deployed yet;
2. Assembles the plugin into ``dist/afterburner`` if it is not already present (stdlib
   transport fallback applies while the official gassist_sdk is not vendored --
   ``libs/README.txt``); pass ``--source <folder>`` to install a pre-built folder instead;
3. Installs it as ``<rise>\\plugins\\afterburner\\`` (Protocol V2 discovery path);
4. Re-runs ``tools/smoke_process_plugin.py`` against the *installed* plugin.py using the
   RISE runtime's own bundled python, so the smoke exercises the interpreter the host will
   actually spawn.

Writing under %PROGRAMDATA% requires elevation when the RISE folder is admin-owned, so this
script is intended to be run elevated (e.g. ``python tools/install_to_rise.py`` from an
administrator shell / after a UAC prompt).

Usage:
    python tools/install_to_rise.py [--source dist/afterburner] [--skip-install] [--skip-smoke]
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent
RISE_DEFAULT = Path(r"C:\ProgramData\NVIDIA Corporation\nvtopps\rise")
PLUGIN_NAME = "afterburner"


def find_rise_root() -> Optional[Path]:
    override = __import__("os").environ.get("RISE_ROOT")
    candidates = ([Path(override)] if override else []) + [RISE_DEFAULT]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def find_rise_python(rise: Path) -> Optional[Path]:
    py_dir = rise / "python"
    if not py_dir.is_dir():
        return None
    for pattern in ("python.exe", "python3*.exe", "pythonw.exe"):
        matches = sorted(py_dir.glob(pattern))
        if matches:
            return matches[0]
    return None


def assemble_source(source: Path) -> Path:
    if source.is_dir() and (source / "plugin.py").is_file():
        return source
    print(f"assembling plugin (--source {source} not a built folder) ...")
    out = ROOT / "dist" / PLUGIN_NAME
    proc = subprocess.run(
        [sys.executable, str(ROOT / "build.py"), "--out", str(out), "--allow-no-sdk", "--force"],
        capture_output=True,
        text=True,
    )
    print(proc.stdout.strip())
    if proc.returncode != 0:
        print(proc.stderr.strip(), file=sys.stderr)
        raise SystemExit(proc.returncode)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(ROOT / "dist" / PLUGIN_NAME))
    parser.add_argument("--skip-install", action="store_true")
    parser.add_argument("--skip-smoke", action="store_true")
    args = parser.parse_args(argv)

    rise = find_rise_root()
    if rise is None:
        print(
            "RISE runtime is not deployed yet: no plugins directory found under "
            f"{RISE_DEFAULT}.\n"
            "Activate/opt into Project G-Assist in NVIDIA App (sign in + enable) so NVIDIA "
            "App deploys the runtime it already staged, then re-run this script.\n"
            "Override the location with RISE_ROOT=<path> if your install differs.",
            file=sys.stderr,
        )
        return 2

    plugins_dir = rise / "plugins"
    target = plugins_dir / PLUGIN_NAME
    print(f"RISE runtime: {rise}")
    print(f"plugins dir : {plugins_dir}")

    source = assemble_source(Path(args.source))

    if not args.skip_install:
        plugins_dir.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)
        print(f"installed plugin -> {target}")
    else:
        print("install skipped")

    if args.skip_smoke:
        return 0

    rise_python = find_rise_python(rise)
    if rise_python is None:
        print(
            f"no bundled python under {rise}\\python\\ -- smoke skipped (run "
            "tools/smoke_process_plugin.py manually once the runtime is complete)",
            file=sys.stderr,
        )
        return 1
    print(f"host python  : {rise_python}")
    smoke = ROOT / "tools" / "smoke_process_plugin.py"
    proc = subprocess.run(
        [str(rise_python), str(smoke), "--python", str(rise_python), "--plugin", str(target / "plugin.py")]
    )
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
