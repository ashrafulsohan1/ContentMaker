"""Build the one-file ContentMaker.exe via PyInstaller (v1.0.1 packaging).

Drives PyInstaller with the checked-in ``ContentMaker.spec`` — never regenerates
a spec, so the build is reproducible:

    python scripts/build_exe.py                  # ffmpeg/ffprobe found at runtime
    python scripts/build_exe.py --bundle-ffmpeg  # copy them INTO the exe

``--bundle-ffmpeg`` stages the binaries the 3-tier resolver
(:mod:`app.core.binaries`) finds — CONTENTMAKER_FFMPEG_DIR first, then PATH —
into ``build_staging/ffmpeg_bin``, which the spec includes as datas when that
directory exists (tier 2 of the resolver at runtime). A plain build removes any
stale staging dir first, so it can never ship binaries by accident.

Licensing: gyan.dev/Windows ffmpeg builds are GPL — bundling is fine for this
private/personal distribution; public redistribution would require GPL
compliance (or build without ``--bundle-ffmpeg``). See README §10.

Requires ``pip install pyinstaller`` in the environment used to build.
"""
from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_FILE = REPO_ROOT / "ContentMaker.spec"
STAGING_DIR = REPO_ROOT / "build_staging"
FFMPEG_BIN_STAGING = STAGING_DIR / "ffmpeg_bin"
DIST_DIR = REPO_ROOT / "dist"
WORK_DIR = REPO_ROOT / "build"

# sys.path bootstrap: this script lives outside the app package and is run
# directly (python scripts/build_exe.py), so app/ imports need the repo root.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_TOOL_EXE_NAMES = {"ffmpeg": "ffmpeg.exe", "ffprobe": "ffprobe.exe"}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="build_exe.py",
        description="Build the one-file ContentMaker.exe via PyInstaller.",
    )
    parser.add_argument(
        "--bundle-ffmpeg",
        action="store_true",
        help="copy the resolved ffmpeg.exe/ffprobe.exe into the exe (GPL — see README §10)",
    )
    return parser.parse_args(argv)


def stage_ffmpeg_bin(dest: Path = FFMPEG_BIN_STAGING) -> list[Path]:
    """Copy the resolver's ffmpeg/ffprobe into ``dest``; loud when unresolvable."""
    from app.core import binaries

    copied: list[Path] = []
    for tool, exe_name in _TOOL_EXE_NAMES.items():
        source = Path(binaries.resolve(tool))
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / exe_name
        shutil.copy2(source, target)
        copied.append(target)
    return copied


def prepare_staging(bundle_ffmpeg: bool) -> None:
    """Stage bundled binaries for a --bundle-ffmpeg build; clear them otherwise."""
    if bundle_ffmpeg:
        for target in stage_ffmpeg_bin():
            print(f"bundled: {target}")
    else:
        shutil.rmtree(FFMPEG_BIN_STAGING, ignore_errors=True)


def pyinstaller_command() -> list[str]:
    return [
        sys.executable,
        "-m",
        "PyInstaller",
        str(SPEC_FILE),
        "--noconfirm",
        "--distpath",
        str(DIST_DIR),
        "--workpath",
        str(WORK_DIR / "pyinstaller"),
    ]


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if importlib.util.find_spec("PyInstaller") is None:
        print("PyInstaller is not installed in this Python environment.", file=sys.stderr)
        print("Install it, then rebuild:  pip install pyinstaller", file=sys.stderr)
        return 1
    prepare_staging(args.bundle_ffmpeg)
    print("Running PyInstaller (one-file build — takes a few minutes) ...")
    proc = subprocess.run(pyinstaller_command(), cwd=REPO_ROOT)
    if proc.returncode != 0:
        print(f"PyInstaller failed (exit {proc.returncode}).", file=sys.stderr)
        return proc.returncode
    exe = DIST_DIR / "ContentMaker.exe"
    if not exe.is_file():
        print(f"Expected output {exe} was not produced.", file=sys.stderr)
        return 1
    print(f"Built {exe} ({exe.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
