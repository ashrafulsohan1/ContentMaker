"""Seams: app.assembly.ffmpeg — real ffmpeg/ffprobe integration (BUILD_PLAN 1.8).

Tests run against the real ffmpeg/ffprobe on PATH (named system dependency,
spec §7): the wrapper must encode and probe real media through absolute
Windows-safe paths (spaces/CJK filenames survive; no cwd dependence), extract
ffprobe durations for the manifest (§1.2 "Durations"), and fail loudly —
missing binary, bad args, hung encode (timeout), no-duration file — with
FFmpegError carrying the exit code and stderr tail, never silently.
"""
import wave
from pathlib import Path

import pytest

from app.assembly.ffmpeg import FFmpeg, FFmpegError, probe_duration
from app.engines.base import ImageRequest, VideoRequest
from app.engines.mocks import MockImageProvider, MockVideoProvider


def _silent_wav(path: Path, seconds: float, rate: int = 8000) -> None:
    frames = round(seconds * rate)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * frames)


def test_run_encodes_real_mp4_and_returns_process(tmp_path: Path):
    out = tmp_path / "out.mp4"
    proc = FFmpeg().run(["-f", "lavfi", "-i", "color=c=red:s=64x64:d=0.3:r=10", "-y", out])
    assert proc.returncode == 0
    assert out.exists() and out.stat().st_size > 0
    assert isinstance(proc.stderr, str)


def test_run_absolutizes_path_arguments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # A Path arg must reach ffmpeg as an absolute path: a missing input shows
    # up in ffmpeg's stderr at its resolved absolute location, not as typed.
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FFmpegError) as excinfo:
        FFmpeg().run(["-i", Path("missing_input.wav"), str(tmp_path / "out.mp4")])
    assert str(tmp_path) in excinfo.value.stderr_tail


def test_run_and_probe_survive_spaces_and_unicode_paths(tmp_path: Path):
    out_dir = tmp_path / "dir with spaces" / "ビデオ出力"
    out_dir.mkdir(parents=True)
    out = out_dir / "beat 01 – 名前.mp4"
    FFmpeg().run(["-f", "lavfi", "-i", "color=c=blue:s=64x64:d=0.3:r=10", "-y", out])
    assert out.exists()
    assert abs(FFmpeg().probe_duration(out) - 0.3) < 0.1


def test_probe_duration_reads_exact_wav_length(tmp_path: Path):
    wav = tmp_path / "beat_01.wav"
    _silent_wav(wav, 1.5)
    assert abs(FFmpeg().probe_duration(wav) - 1.5) < 0.02


def test_probe_duration_of_mock_video_mp4(tmp_path: Path):
    # The M1 dry-run chain: mock assets must be probeable by the wrapper —
    # assembly (1.12) reads timing from probed values, never reported ones.
    out = tmp_path / "beat_01.mp4"
    MockVideoProvider().generate(
        VideoRequest(prompt="clip", duration_s=2.0, width=320, height=200, out_path=out)
    )
    assert abs(FFmpeg().probe_duration(out) - 2.0) < 0.15


def test_probe_returns_format_and_streams(tmp_path: Path):
    wav = tmp_path / "beat_01.wav"
    _silent_wav(wav, 0.5)
    data = FFmpeg().probe(wav)
    assert abs(float(data["format"]["duration"]) - 0.5) < 0.02
    assert data["streams"] and data["streams"][0]["codec_name"] == "pcm_s16le"


def test_probe_duration_missing_file_raises_loudly(tmp_path: Path):
    missing = tmp_path / "nope.wav"
    with pytest.raises(FFmpegError, match="nope") as excinfo:
        FFmpeg().probe_duration(missing)
    assert excinfo.value.returncode not in (None, 0)


def test_probe_duration_of_image_raises_clear_no_duration_error(tmp_path: Path):
    png = tmp_path / "beat_01.png"
    MockImageProvider().generate(
        ImageRequest(prompt="a castle", style_hint=None, seed=1, width=64, height=64, out_path=png)
    )
    with pytest.raises(FFmpegError, match="duration"):
        FFmpeg().probe_duration(png)


def test_run_failure_raises_ffmpeg_error_with_stderr_tail(tmp_path: Path):
    out = tmp_path / "out.mp4"
    with pytest.raises(FFmpegError) as excinfo:
        FFmpeg().run(["-f", "lavfi", "-i", "definitely_not_a_source", "-y", out])
    err = excinfo.value
    # Nonzero — Windows reports the raw DWORD (observed 3015096584, not 1),
    # so don't pin an exact value; the stderr tail must carry the objection.
    assert err.returncode
    assert "No such filter" in err.stderr_tail
    assert not out.exists()


def test_missing_binary_raises_clear_setup_error(tmp_path: Path):
    with pytest.raises(FFmpegError, match="PATH") as excinfo:
        FFmpeg(ffmpeg_bin="ffmpeg-not-installed-xyz").run(["-y", str(tmp_path / "x.mp4")])
    assert excinfo.value.returncode is None  # never spawned
    with pytest.raises(FFmpegError, match="PATH"):
        FFmpeg(ffprobe_bin="ffprobe-not-installed-xyz").probe(tmp_path / "x.wav")


def test_hung_ffmpeg_hits_timeout_and_raises_ffmpeg_error():
    # Infinite lavfi source into the null muxer: never finishes, writes
    # nothing — a hung encode must surface as FFmpegError, never hang the
    # job worker (§6.6).
    with pytest.raises(FFmpegError, match="timed out"):
        FFmpeg(timeout_s=2.0).run(["-f", "lavfi", "-i", "anullsrc", "-f", "null", "-"])


def test_module_level_probe_duration_shortcut(tmp_path: Path):
    wav = tmp_path / "beat_01.wav"
    _silent_wav(wav, 1.0)
    assert abs(probe_duration(wav) - 1.0) < 0.02
