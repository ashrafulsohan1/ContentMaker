"""3-tier ffmpeg/ffprobe resolver (v1.0.1 packaging).

A packaged ContentMaker.exe cannot assume the dev machine's PATH setup, so
every ffmpeg/ffprobe invocation resolves its binary the same way, tier by
tier, first hit wins:

1. **``CONTENTMAKER_FFMPEG_DIR``** — a directory holding ``ffmpeg(.exe)`` /
   ``ffprobe(.exe)``; lets a user point the app at any ffmpeg folder without
   touching PATH. A set-but-unusable dir (missing dir / missing binary) is
   skipped, not fatal — the next tier may still answer.
2. **Bundled dir** — ``ffmpeg_bin`` included as datas by
   ``scripts/build_exe.py --bundle-ffmpeg``; PyInstaller's one-file bootloader
   unpacks it under ``sys._MEIPASS`` (only present in frozen builds).
3. **PATH** — plain ``shutil.which`` (the dev/setup norm, spec §7).

Nothing resolving raises a loud :class:`PackagingError` naming all three tiers
and the fix — assembly/probe paths must never fail silently (§6.5 spirit).
Resolution happens per call, not at import, so env changes and tests behave;
the :class:`~app.assembly.ffmpeg.FFmpeg` wrapper wires it as its defaults.
"""
from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path

CONTENTMAKER_FFMPEG_DIR = "CONTENTMAKER_FFMPEG_DIR"
BUNDLED_SUBDIR = "ffmpeg_bin"

# Candidates per tool, Windows-first; `shutil.which` handles PATHEXT for the
# bare names on Windows, and the extensionless names keep Linux/WSL dev honest.
_TOOLS: dict[str, tuple[str, str]] = {
    "ffmpeg": ("ffmpeg.exe", "ffmpeg"),
    "ffprobe": ("ffprobe.exe", "ffprobe"),
}


class PackagingError(RuntimeError):
    """No usable binary found in any of the three resolver tiers."""


def _from_dir(tool: str, directory: Path) -> str | None:
    for name in _TOOLS[tool]:
        candidate = directory / name
        if candidate.is_file():
            return str(candidate)
    return None


def resolve(
    tool: str, *, env: Mapping[str, str] | None = None, required: bool = True
) -> str | None:
    """Resolve ``tool`` ("ffmpeg" | "ffprobe") through the three tiers.

    Returns the resolved binary path (string, spawn-ready), or ``None`` when
    nothing resolves and ``required=False`` (diagnostics, e.g. /healthz).
    Raises :class:`PackagingError` naming all three tiers when nothing resolves
    and ``required=True`` (the default).
    """
    if tool not in _TOOLS:
        raise ValueError(f"unknown tool {tool!r} — expected one of {sorted(_TOOLS)}")
    source = os.environ if env is None else env

    # Tier 1: CONTENTMAKER_FFMPEG_DIR
    dir_raw = source.get(CONTENTMAKER_FFMPEG_DIR)
    dir_path = Path(dir_raw).expanduser() if dir_raw else None
    if dir_path is not None:
        found = _from_dir(tool, dir_path)
        if found is not None:
            return found

    # Tier 2: bundled ffmpeg_bin (PyInstaller unpacks datas under sys._MEIPASS)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        found = _from_dir(tool, Path(meipass) / BUNDLED_SUBDIR)
        if found is not None:
            return found

    # Tier 3: PATH
    found = shutil.which(_TOOLS[tool][1])
    if found:
        return str(found)

    if not required:
        return None
    raise PackagingError(
        f"{tool} not found. Looked in all three locations:\n"
        f"  1. {CONTENTMAKER_FFMPEG_DIR} — "
        + (
            f"set to {dir_path}, but no {_TOOLS[tool][0]} there"
            if dir_path is not None
            else "not set"
        )
        + f"\n  2. the bundled {BUNDLED_SUBDIR}/ directory inside the app "
        "(only present in a --bundle-ffmpeg build)\n"
        f"  3. PATH (shutil.which)\n"
        f"Fix: install ffmpeg (winget install Gyan.FFmpeg) and reopen the terminal, "
        f"or set {CONTENTMAKER_FFMPEG_DIR} to the folder holding {_TOOLS[tool][0]}."
    )


def resolve_ffmpeg(*, env: Mapping[str, str] | None = None) -> str:
    """ffmpeg binary path; raises :class:`PackagingError` when unresolvable."""
    return resolve("ffmpeg", env=env)  # type: ignore[return-value]


def resolve_ffprobe(*, env: Mapping[str, str] | None = None) -> str:
    """ffprobe binary path; raises :class:`PackagingError` when unresolvable."""
    return resolve("ffprobe", env=env)  # type: ignore[return-value]
