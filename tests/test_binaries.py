"""Seam: app.core.binaries — 3-tier ffmpeg/ffprobe resolver (v1.0.1 packaging).

The packaged exe must find ffmpeg/ffprobe without the dev machine's PATH setup:
tier 1 — the ``CONTENTMAKER_FFMPEG_DIR`` env dir; tier 2 — the bundled
``ffmpeg_bin`` directory PyInstaller unpacks to ``sys._MEIPASS`` (only present
in a ``--bundle-ffmpeg`` build); tier 3 — PATH via ``shutil.which``. A tier
without the tool falls through to the next; nothing resolving is a loud
PackagingError naming all three tiers, never a silent fallback.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from app.assembly.ffmpeg import FFmpeg
from app.core import binaries
from app.core.binaries import PackagingError, resolve, resolve_ffmpeg, resolve_ffprobe


def _bindir(parent: Path, *names: str) -> Path:
    """A directory holding fake binary files (existence is what the resolver checks)."""
    d = parent / "bin"
    d.mkdir()
    for name in names:
        (d / name).write_bytes(b"fake binary")
    return d


def test_resolve_rejects_unknown_tool():
    with pytest.raises(ValueError, match="ffmpeg"):
        resolve("notatool")


def test_tier1_env_dir_wins_over_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fake = _bindir(tmp_path, "ffmpeg.exe", "ffprobe.exe")
    monkeypatch.setenv("CONTENTMAKER_FFMPEG_DIR", str(fake))
    # Real ffmpeg/ffprobe also exist on PATH here (suite requirement) — env wins anyway.
    assert resolve_ffmpeg() == str(fake / "ffmpeg.exe")
    assert resolve_ffprobe() == str(fake / "ffprobe.exe")


def test_tier1_missing_dir_or_binary_falls_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from_path = "C:/from-path"
    monkeypatch.setattr(binaries.shutil, "which", lambda tool: f"{from_path}/{tool}")

    monkeypatch.setenv("CONTENTMAKER_FFMPEG_DIR", str(tmp_path / "no_such_dir"))
    assert resolve_ffmpeg() == f"{from_path}/ffmpeg"

    # Dir exists but lacks this tool's binary → the tier is skipped, not fatal.
    partial = _bindir(tmp_path, "ffmpeg.exe")
    monkeypatch.setenv("CONTENTMAKER_FFMPEG_DIR", str(partial))
    assert resolve_ffprobe() == f"{from_path}/ffprobe"


def test_tier2_bundled_meipass_wins_over_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bundled = tmp_path / "ffmpeg_bin"
    bundled.mkdir()
    (bundled / "ffmpeg.exe").write_bytes(b"fake")
    (bundled / "ffprobe.exe").write_bytes(b"fake")
    monkeypatch.delenv("CONTENTMAKER_FFMPEG_DIR", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert resolve_ffmpeg() == str(bundled / "ffmpeg.exe")
    assert resolve_ffprobe() == str(bundled / "ffprobe.exe")


def test_tier3_path_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CONTENTMAKER_FFMPEG_DIR", raising=False)
    # Bundled dir absent → tier 2 passes; PATH answers.
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "absent"), raising=False)
    monkeypatch.setattr(binaries.shutil, "which", lambda tool: "C:/from-path/" + tool)
    assert resolve("ffmpeg") == "C:/from-path/ffmpeg"


def test_tier3_real_path_integration():
    # This suite already requires real ffmpeg/ffprobe on PATH (spec §7): the
    # PATH tier must surface exactly what shutil.which finds.
    assert resolve_ffmpeg() == shutil.which("ffmpeg")
    assert resolve_ffprobe() == shutil.which("ffprobe")


def test_nothing_resolving_raises_packaging_error_naming_all_three_tiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("CONTENTMAKER_FFMPEG_DIR", raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "absent"), raising=False)
    monkeypatch.setattr(binaries.shutil, "which", lambda tool: None)
    with pytest.raises(PackagingError) as excinfo:
        resolve_ffmpeg()
    message = str(excinfo.value)
    assert "CONTENTMAKER_FFMPEG_DIR" in message  # tier 1 named
    assert "bundled" in message and "ffmpeg_bin" in message  # tier 2 named
    assert "PATH" in message  # tier 3 named
    with pytest.raises(PackagingError, match="ffprobe"):
        resolve_ffprobe()


def test_optional_resolution_returns_none_for_healthz(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("CONTENTMAKER_FFMPEG_DIR", raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "absent"), raising=False)
    monkeypatch.setattr(binaries.shutil, "which", lambda tool: None)
    assert resolve("ffmpeg", required=False) is None
    assert resolve("ffprobe", required=False) is None


def test_ffmpeg_wrapper_defaults_go_through_the_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fake = _bindir(tmp_path, "ffmpeg.exe", "ffprobe.exe")
    monkeypatch.setenv("CONTENTMAKER_FFMPEG_DIR", str(fake))
    runner = FFmpeg()
    assert runner.ffmpeg_bin == str(fake / "ffmpeg.exe")
    assert runner.ffprobe_bin == str(fake / "ffprobe.exe")
    # Explicitly passed bins still win (existing tests inject broken names).
    assert FFmpeg(ffmpeg_bin="ffmpeg-not-installed-xyz").ffmpeg_bin == "ffmpeg-not-installed-xyz"
