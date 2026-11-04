"""Seams: app.engines.registry + app.engines.mocks.

Registry catches: lost providers, unclear lookup failures.
Mocks catch: fake assets that aren't real files (dry-run must feed real
ffmpeg assembly later); costs must be exactly zero (§7 cost awareness).
Provider HTTP adapters arrive in M2+ behind the same protocols.
"""
import struct
import wave
from pathlib import Path

import pytest

from app.engines.base import ImageRequest, ProviderError, TTSRequest, VideoRequest
from app.engines.mocks import MockImageProvider, MockTTSProvider, MockVideoProvider
from app.engines.registry import Registry


def test_register_and_lookup_roundtrip(tmp_path: Path):
    reg = Registry()
    tts = MockTTSProvider()
    reg.register("tts", tts)
    assert reg.get("tts", "mock") is tts
    assert reg.providers_for("tts") == [tts]


def test_unknown_engine_or_provider_raises_clear_errors():
    reg = Registry()
    with pytest.raises(ValueError, match="unknown engine 'subtitles'"):
        reg.providers_for("subtitles")
    reg.register("tts", MockTTSProvider())
    with pytest.raises(ValueError, match="no 'nope' provider for engine 'tts'"):
        reg.get("tts", "nope")


def test_mock_tts_writes_real_silent_wav(tmp_path: Path):
    out = tmp_path / "beat_01.wav"
    result = MockTTSProvider().generate(
        TTSRequest(text="a" * 150, voice="alloy", pace=1.0, tone=None, out_path=out)
    )
    assert result.file == out and out.exists()
    with wave.open(str(out), "rb") as w:
        duration = w.getnframes() / w.getframerate()
    assert abs(duration - 10.0) < 0.1  # 150 chars at 15 chars/s


def test_mock_tts_minimum_duration_is_one_second(tmp_path: Path):
    out = tmp_path / "tiny.wav"
    MockTTSProvider().generate(TTSRequest(text="hi", voice="v", pace=1.0, tone=None, out_path=out))
    with wave.open(str(out), "rb") as w:
        assert w.getnframes() / w.getframerate() >= 1.0


def test_mock_image_writes_valid_png_at_requested_size(tmp_path: Path):
    out = tmp_path / "beat_01.png"
    MockImageProvider().generate(
        ImageRequest(
            prompt="a castle", style_hint=None, seed=7, width=320, height=200, out_path=out
        )
    )
    data = out.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (320, 200)


def test_mock_image_is_deterministic_per_seed(tmp_path: Path):
    mk = lambda p: MockImageProvider().generate(  # noqa: E731
        ImageRequest(prompt="same", style_hint=None, seed=3, width=64, height=64, out_path=p)
    )
    a, b = mk(tmp_path / "a.png"), mk(tmp_path / "b.png")
    assert a.file.read_bytes() == b.file.read_bytes()


def test_mock_video_writes_real_mp4(tmp_path: Path):
    out = tmp_path / "beat_01.mp4"
    MockVideoProvider().generate(
        VideoRequest(prompt="clip", duration_s=1.0, width=320, height=200, out_path=out)
    )
    data = out.read_bytes()
    assert len(data) > 1000
    assert b"ftyp" in data[:32]  # real MP4 container


def test_mock_costs_are_exactly_zero(tmp_path: Path):
    tts = TTSRequest(text="x", voice="v", pace=1.0, tone=None, out_path=tmp_path / "a.wav")
    img = ImageRequest(
        prompt="p", style_hint=None, seed=None, width=64, height=64, out_path=tmp_path / "b.png"
    )
    vid = VideoRequest(prompt="p", duration_s=5.0, width=64, height=64, out_path=tmp_path / "c.mp4")
    assert MockTTSProvider().estimate_cost_usd(tts) == 0.0
    assert MockImageProvider().estimate_cost_usd(img) == 0.0
    assert MockVideoProvider().estimate_cost_usd(vid) == 0.0


def test_provider_error_carries_context():
    err = ProviderError("mock", "quota exhausted", retryable=False)
    assert err.provider_id == "mock"
    assert err.retryable is False
    assert "quota" in str(err)
