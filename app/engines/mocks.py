"""Zero-network mock providers (BUILD_PLAN 1.4; dry-run mode §1.2).

Mocks write real media files so assembly (task 1.12) is real ffmpeg from M1:
- TTS: silent WAV via stdlib ``wave`` — 15 chars/s at pace 1.0, minimum 1s.
- Image: deterministic PNG via ``zlib``/``struct`` — real IHDR dimensions, a
  seed+prompt-derived gradient. No PIL dependency; text labels/overlays belong
  to assembly/images.py (task 1.12) via ffmpeg drawtext.
- Video: real MP4 via the ffmpeg lavfi color source (ffmpeg is a named system
  dependency; the binary goes through the app.core.binaries 3-tier resolver so
  dry-run works in the packaged exe too).

Costs are exactly zero (§7 cost awareness) and no network is touched.
"""
from __future__ import annotations

import subprocess
import wave
import zlib
from struct import pack

from app.core.binaries import resolve_ffmpeg
from app.engines.base import (
    ImageRequest,
    ImageResult,
    ProviderError,
    TTSRequest,
    TTSResult,
    VideoRequest,
    VideoResult,
)

PROVIDER_ID = "mock"

_CHARS_PER_SECOND = 15.0
_MIN_DURATION_S = 1.0
_SAMPLE_RATE = 24_000  # Hz, mono 16-bit — typical TTS output shape
_FPS = 24
_FFMPEG_TIMEOUT_S = 60
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class MockTTSProvider:
    """Silent WAV sized to the text: len(text)/15 seconds at pace 1.0, min 1s."""

    provider_id = PROVIDER_ID

    def generate(self, request: TTSRequest) -> TTSResult:
        chars_per_second = _CHARS_PER_SECOND * max(request.pace, 0.1)
        seconds = max(_MIN_DURATION_S, len(request.text) / chars_per_second)
        frames = round(seconds * _SAMPLE_RATE)
        request.out_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(request.out_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)  # 16-bit PCM
            wav.setframerate(_SAMPLE_RATE)
            wav.writeframes(b"\x00\x00" * frames)
        return TTSResult(file=request.out_path, duration_s=frames / _SAMPLE_RATE)

    def estimate_cost_usd(self, request: TTSRequest) -> float:
        return 0.0


class MockImageProvider:
    """Deterministic PNG at the requested size; same prompt/seed → same bytes."""

    provider_id = PROVIDER_ID

    def generate(self, request: ImageRequest) -> ImageResult:
        seed = zlib.crc32(f"{request.prompt}|{request.style_hint}|{request.seed}".encode())
        request.out_path.parent.mkdir(parents=True, exist_ok=True)
        request.out_path.write_bytes(_png_bytes(request.width, request.height, seed))
        return ImageResult(file=request.out_path)

    def estimate_cost_usd(self, request: ImageRequest) -> float:
        return 0.0


def _chunk(chunk_type: bytes, payload: bytes) -> bytes:
    return pack(">I", len(payload)) + chunk_type + payload + pack(
        ">I", zlib.crc32(chunk_type + payload) & 0xFFFFFFFF
    )


def _png_bytes(width: int, height: int, seed: int) -> bytes:
    """Minimal valid PNG (8-bit RGB): horizontal red gradient × vertical g/b gradient."""
    ihdr = pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # depth 8, color type 2 (RGB)
    fx_step = 112 / max(width - 1, 1)
    fy_step = 112 / max(height - 1, 1)
    r_cols = bytes(((seed >> 16) + round(x * fx_step)) % 256 for x in range(width))
    raw = bytearray()
    for y in range(height):
        g = ((seed >> 8) + round(y * fy_step)) % 256
        b = (seed + round(y * fy_step) * 2) % 256
        row = bytearray(3 * width)
        row[0::3] = r_cols
        row[1::3] = bytes([g]) * width
        row[2::3] = bytes([b]) * width
        raw.append(0)  # scanline filter type 0 (None)
        raw += row
    idat = zlib.compress(bytes(raw), 9)
    return _PNG_SIGNATURE + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


class MockVideoProvider:
    """Solid-color MP4 via ffmpeg lavfi — real container/encoder, zero cost."""

    provider_id = PROVIDER_ID

    def generate(self, request: VideoRequest) -> VideoResult:
        request.out_path.parent.mkdir(parents=True, exist_ok=True)
        color = f"0x{zlib.crc32(request.prompt.encode()) & 0xFFFFFF:06x}"
        source = (
            f"color=c={color}:s={request.width}x{request.height}"
            f":r={_FPS}:d={request.duration_s}"
        )
        cmd = [
            resolve_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", source,
            # veryfast: mocks encode at platform dims on every dry-run job and
            # in tests — x264's default (medium) preset dominates that cost
            # for no visual gain on solid-color clips.
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(request.out_path),
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_FFMPEG_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProviderError(
                PROVIDER_ID, f"ffmpeg unavailable or timed out: {exc}", retryable=False
            ) from exc
        if proc.returncode != 0:
            raise ProviderError(
                PROVIDER_ID, f"ffmpeg failed: {_stderr_tail(proc)}", retryable=False
            )
        return VideoResult(file=request.out_path, duration_s=request.duration_s)

    def estimate_cost_usd(self, request: VideoRequest) -> float:
        return 0.0


def _stderr_tail(proc: subprocess.CompletedProcess[str]) -> str:
    lines = [line for line in (proc.stderr or "").strip().splitlines() if line.strip()]
    return lines[-1] if lines else f"no stderr (exit {proc.returncode})"
