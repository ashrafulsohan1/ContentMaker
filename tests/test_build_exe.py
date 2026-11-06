"""Seam: scripts/build_exe.py + the checked-in ContentMaker.spec (v1.0.1).

The build script is exercised only at its seams (arg parsing, the ffmpeg-bin
staging step, the PyInstaller command it shells out to) — tests NEVER run a
PyInstaller build; the real build is done manually via ``python
scripts/build_exe.py`` and verified by the exe smoke, not here.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from app.core import binaries
from app.core.binaries import PackagingError

_REPO = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / "scripts" / "build_exe.py"
_SPEC = _REPO / "ContentMaker.spec"


def _load_build_exe() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_exe_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def build_exe() -> ModuleType:
    return _load_build_exe()


def test_build_script_is_checked_in(build_exe: ModuleType):
    assert _SCRIPT.is_file()


def test_parse_args_default_does_not_bundle(build_exe: ModuleType):
    assert build_exe.parse_args([]).bundle_ffmpeg is False


def test_parse_args_bundle_ffmpeg_flag(build_exe: ModuleType):
    assert build_exe.parse_args(["--bundle-ffmpeg"]).bundle_ffmpeg is True


def test_pyinstaller_command_drives_the_checked_in_spec(build_exe: ModuleType):
    cmd = build_exe.pyinstaller_command()
    assert cmd[:3] == [sys.executable, "-m", "PyInstaller"]
    assert str(build_exe.SPEC_FILE) in cmd
    assert "--noconfirm" in cmd  # never prompt mid-build


def test_checked_in_spec_pins_name_and_datas(build_exe: ModuleType):
    assert _SPEC.is_file()
    text = _SPEC.read_text(encoding="utf-8")
    assert 'name="ContentMaker"' in text
    for entry in ("app/web/templates", "app/web/static", "app/data", "ffmpeg_bin"):
        assert entry in text


def test_stage_ffmpeg_bin_copies_resolved_binaries(
    build_exe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    src = tmp_path / "src"
    src.mkdir()
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        (src / name).write_bytes(b"fake binary")
    monkeypatch.setattr(binaries, "resolve", lambda tool, **kwargs: str(src / f"{tool}.exe"))
    staging = tmp_path / "staging" / "ffmpeg_bin"
    build_exe.stage_ffmpeg_bin(staging)
    assert (staging / "ffmpeg.exe").read_bytes() == b"fake binary"
    assert (staging / "ffprobe.exe").read_bytes() == b"fake binary"


def test_stage_ffmpeg_bin_is_loud_when_nothing_resolves(
    build_exe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    def boom(tool: str, **kwargs: object) -> str:
        raise PackagingError("no ffmpeg anywhere")

    monkeypatch.setattr(binaries, "resolve", boom)
    with pytest.raises(PackagingError):
        build_exe.stage_ffmpeg_bin(tmp_path / "staging")


def test_prepare_staging_without_flag_removes_stale_bundled_bin(
    build_exe: ModuleType,
):
    # A previous --bundle-ffmpeg run may have left binaries staged: a plain
    # build must never ship them by accident.
    build_exe.FFMPEG_BIN_STAGING.mkdir(parents=True, exist_ok=True)
    (build_exe.FFMPEG_BIN_STAGING / "ffmpeg.exe").write_bytes(b"stale")
    build_exe.prepare_staging(bundle_ffmpeg=False)
    assert not build_exe.FFMPEG_BIN_STAGING.exists()
