"""Engine protocols + request/result dataclasses + ProviderError (BUILD_PLAN 1.4).

Every engine (TTS / image / video) sits behind a provider protocol so BYOK
HTTP adapters (M2+) and the zero-network mocks (engines/mocks.py) are
interchangeable through engines/registry.py. Durations reported in results are
informational only — assembly always ffprobes real file durations and the
timing manifest is written from probed values (BUILD_PLAN §1.2 "Durations").
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class ProviderError(Exception):
    """A provider call failed; carries the context jobs/UI need (§6.5–6.6).

    ``provider_id`` names which provider failed, ``message`` says exactly what
    went wrong or is missing, ``retryable`` drives the bounded-retry policy.
    """

    def __init__(self, provider_id: str, message: str, retryable: bool = True) -> None:
        super().__init__(message)
        self.provider_id = provider_id
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True, kw_only=True)
class TTSRequest:
    """One per-beat speech synthesis call (per-beat TTS is a §2 constraint)."""

    text: str
    voice: str
    pace: float = 1.0
    tone: str | None = None
    out_path: Path


@dataclass(frozen=True)
class TTSResult:
    file: Path
    duration_s: float  # provider-reported; not authoritative for the manifest


@dataclass(frozen=True, kw_only=True)
class ImageRequest:
    """One image generation call; width/height come from platform specs (§5)."""

    prompt: str
    style_hint: str | None = None
    seed: int | None = None
    width: int = 1024
    height: int = 1024
    out_path: Path


@dataclass(frozen=True)
class ImageResult:
    file: Path


@dataclass(frozen=True, kw_only=True)
class VideoRequest:
    """One video-clip generation call (Veo-style, M4); dims from platform specs."""

    prompt: str
    duration_s: float
    width: int = 1024
    height: int = 1024
    out_path: Path


@dataclass(frozen=True)
class VideoResult:
    file: Path
    duration_s: float  # provider-reported; not authoritative for the manifest


class TTSProvider(Protocol):
    """Text-to-speech provider surface; mocks and BYOK adapters implement it."""

    provider_id: str

    def generate(self, request: TTSRequest) -> TTSResult: ...

    def estimate_cost_usd(self, request: TTSRequest) -> float: ...


class ImageProvider(Protocol):
    """Image provider surface; mocks and BYOK adapters implement it."""

    provider_id: str

    def generate(self, request: ImageRequest) -> ImageResult: ...

    def estimate_cost_usd(self, request: ImageRequest) -> float: ...


class VideoProvider(Protocol):
    """Video provider surface; mocks and BYOK adapters implement it."""

    provider_id: str

    def generate(self, request: VideoRequest) -> VideoResult: ...

    def estimate_cost_usd(self, request: VideoRequest) -> float: ...
