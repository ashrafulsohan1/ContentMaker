"""Gemini TTS provider — per-beat synthesis on the Interactions API (BUILD_PLAN 2.3).

One :class:`GeminiTTSProvider` per job engine; it rides on the 2.2
:class:`~app.providers.gemini.client.GeminiClient` (auth, timeouts, error
mapping live there and nowhere else) and implements the
``TTSProvider`` protocol from ``app.engines.base``, so BYOK TTS is
interchangeable with the zero-network mocks.

Contract, per the research spike (research/gemini-api.md):

- **Per-beat synthesis** (§2 constraint): one ``POST /v1beta/interactions``
  per beat. The beat text goes in **verbatim** — the text field is a
  transcript, never a prompt (spike §2.3) — the voice in
  ``generation_config.speech_config``, and the output format is unary WAV
  (``audio/wav``, 24 kHz — spike §2.4) written straight to the asset path.
- **Voice / pace / tone** (spike §2.3): Gemini has **no numeric rate/pitch
  parameters** — control is natural language. The voice id passes through as
  configured (prebuilt studio voice or ``voice_…`` custom id); ``pace`` (the
  app's speed multiplier, 1.0 = provider default) maps to a short graded
  style phrase (:func:`pace_style_phrase`); ``tone`` passes through verbatim.
  Both merge into ONE short ``speech_metadata.style`` string — long constant
  style blocks cause voice drift (spike §2.3) — and a neutral beat carries no
  style annotation at all.
- **Model id from config** (BUILD_PLAN §0 line 26): the model comes from
  ``app/data/prices.json`` (spike-verified ``gemini-3.8-flash-tts``), never
  hardcoded; an explicit ``model=`` override wins for tests/fallbacks.
- **Real durations** (BUILD_PLAN §1.2 "Durations"; task 2.4): nothing in the
  response about timing is trusted — after the bytes land on disk the file is
  ffprobed and the **probed** value is what ``TTSResult.duration_s`` carries
  (and later the timing manifest, via ``scripting.manifest``). A failed probe
  means the "audio" is not a playable file: permanent error naming the file.
- **Errors** are the client's ``GeminiError`` (a ``ProviderError``): jobs.py's
  bounded retry sees ``retryable``, the UI sees a message saying exactly what
  is wrong — without ever naming the key. A completed response that carries
  no audio is permanent (retrying the same request cannot add audio).

All HTTP runs through the client (``httpx.MockTransport`` in tests) — fully
offline contract tests in ``tests/test_gemini_tts.py``.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.assembly.ffmpeg import FFmpegError
from app.assembly.ffmpeg import probe_duration as _default_probe
from app.core.cost import estimate_tts_cost, load_prices
from app.engines.base import TTSRequest, TTSResult
from app.providers.gemini.client import GeminiClient, GeminiError

PROVIDER_ID = "gemini"
INTERACTIONS_PATH = "/v1beta/interactions"

#: Unary WAV output (spike §2.4): RIFF/WAVE, 24 kHz mono 16-bit LE PCM.
RESPONSE_FORMAT = {"type": "audio", "mime_type": "audio/wav", "sample_rate": 24000}


def pace_style_phrase(pace: float) -> str:
    """The natural-language pace clause for the app's speed multiplier.

    The multiplier is the app's knob (presets: 0.1–3.0, 1.0 = provider
    default; higher = faster speech, as the mock's chars-per-second semantics
    establish). Gemini only understands prose (spike §2.3), so the multiplier
    degrades honestly into graded phrases around a neutral band.
    """
    if pace <= 0.5:
        return "speaking very slowly"
    if pace <= 0.85:
        return "speaking slowly"
    if pace < 1.15:
        return ""
    if pace < 1.5:
        return "speaking somewhat rapidly"
    return "speaking rapidly"


def _configured_model() -> str:
    """The spike-verified TTS model id from the editable price table."""
    model = load_prices().price_for(PROVIDER_ID, "tts").model
    if not model:
        raise ValueError(
            "app/data/prices.json has no 'model' for the gemini tts engine —"
            " the Gemini TTS provider refuses to guess a model id"
            " (see research/gemini-api.md §2.1)."
        )
    return model


class GeminiTTSProvider:
    """Per-beat text-to-speech via the Gemini Interactions API.

    ``client`` is a :class:`GeminiClient` bound to a ``KeyStore`` (key read at
    call time — adding/replacing the key in Settings applies to the next
    beat) or a literal key string for probes/tests. ``probe`` injects the
    duration probe for offline tests; the default is the real
    :func:`app.assembly.ffmpeg.probe_duration`.
    """

    provider_id = PROVIDER_ID

    def __init__(
        self,
        client: GeminiClient,
        *,
        model: str | None = None,
        probe: Callable[[Path | str], float] | None = None,
    ) -> None:
        if not isinstance(client, GeminiClient):
            raise ValueError(
                "GeminiTTSProvider needs a GeminiClient (it owns auth, timeouts"
                f" and error mapping), got {type(client).__name__}."
            )
        self.client = client  # public: the key stays readable at call time (§6.5)
        self._model = model if model is not None else _configured_model()
        self._probe = probe if probe is not None else _default_probe

    def estimate_cost_usd(self, request: TTSRequest) -> float:
        """The conservative confirmation-flow estimate (whole 1k-char blocks)."""
        return estimate_tts_cost(load_prices(), PROVIDER_ID, text=request.text)

    def generate(self, request: TTSRequest) -> TTSResult:
        """Synthesize one beat; write the WAV; return its **probed** duration."""
        _require_text_and_voice(request)
        audio = self._synthesize(request)
        request.out_path.parent.mkdir(parents=True, exist_ok=True)
        request.out_path.write_bytes(audio)
        return TTSResult(file=request.out_path, duration_s=self._probed_duration(request))

    # --- synthesis ------------------------------------------------------------------

    def _synthesize(self, request: TTSRequest) -> bytes:
        """One interactions call → the beat's WAV bytes."""
        interaction = self.client.post_json(INTERACTIONS_PATH, json=self._body(request))
        _require_completed(interaction)
        return self._extract_audio(interaction)

    def _body(self, request: TTSRequest) -> dict[str, Any]:
        content: dict[str, Any] = {"type": "text", "text": request.text}
        style = _style(request)
        if style:
            content["annotations"] = [{"type": "speech_metadata", "style": style}]
        return {
            "model": self._model,
            "input": [{"type": "user_input", "content": [content]}],
            "response_format": dict(RESPONSE_FORMAT),
            "generation_config": {"speech_config": [{"voice": request.voice}]},
        }

    def _extract_audio(self, interaction: dict[str, Any]) -> bytes:
        """Walk the REST envelope (spike §1.1): ``steps[]`` entries of type
        ``model_output`` carry ``content[]`` audio blocks. Inline blocks
        arrive base64 in ``data`` (decoded and concatenated in order);
        URI-delivered blocks are downloaded via the client, which attaches
        the key to the Gemini API host only (§6.5)."""
        chunks: list[bytes] = []
        for step in _as_list(interaction.get("steps")):
            if not isinstance(step, dict) or step.get("type") != "model_output":
                continue
            for block in _as_list(step.get("content")):
                if not isinstance(block, dict) or block.get("type") != "audio":
                    continue
                data = block.get("data")
                if isinstance(data, str) and data.strip():
                    chunks.append(_decode_audio_chunk(data))
                    continue
                uri = block.get("uri") or block.get("file_uri")
                if isinstance(uri, str) and uri.strip():
                    chunks.append(self.client.download(uri.strip()))
        if not chunks:
            raise GeminiError(
                "Gemini returned a completed response but it contains no audio"
                " (no audio block under steps[].content) — nothing was written;"
                " regenerate the asset. If this keeps happening, the model"
                " response shape may have drifted (research/gemini-api.md §1.1).",
                retryable=False,
            )
        return b"".join(chunks)

    # --- durations: probed, never trusted --------------------------------------------

    def _probed_duration(self, request: TTSRequest) -> float:
        """ffprobe the written file — the authoritative duration (§1.2)."""
        try:
            duration = float(self._probe(request.out_path))
        except FFmpegError as exc:
            raise GeminiError(
                f"The synthesized audio for {request.out_path.name} is not a"
                f" playable file (ffprobe failed: {exc}) — regenerate the asset.",
                retryable=False,
            ) from exc
        except (TypeError, ValueError) as exc:
            raise GeminiError(
                f"The duration probe for {request.out_path.name} returned a"
                f" non-numeric value: {exc!r}",
                retryable=False,
            ) from exc
        if duration <= 0:
            raise GeminiError(
                f"The synthesized audio for {request.out_path.name} probes as"
                f" {duration:g}s — an empty/invalid file; regenerate the asset.",
                retryable=False,
            )
        return duration


# --- request/response helpers -------------------------------------------------------


def _require_text_and_voice(request: TTSRequest) -> None:
    """Fail before any traffic when the beat cannot be synthesized at all."""
    if not request.text.strip():
        raise GeminiError(
            "The TTS beat has no text to speak — fix or remove the empty beat"
            " in the script review step, then regenerate.",
            retryable=False,
        )
    if not request.voice.strip():
        raise GeminiError(
            "The TTS beat has no voice configured — pick a voice (or preset)"
            " in the options step, then regenerate.",
            retryable=False,
        )


def _style(request: TTSRequest) -> str:
    """One short style string: tone (verbatim) + the pace clause (spike §2.3)."""
    parts = [part for part in (request.tone, pace_style_phrase(request.pace)) if part]
    return ", ".join(parts)


def _require_completed(interaction: dict[str, Any]) -> None:
    """A unary 200 whose interaction did not complete carries no audio."""
    status = interaction.get("status")
    if isinstance(status, str) and status and status != "completed":
        raise GeminiError(
            f"The Gemini TTS interaction ended with status {status!r} instead of"
            " 'completed' — no audio was produced; regenerate the asset.",
            retryable=False,
        )


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _decode_audio_chunk(data: str) -> bytes:
    try:
        return base64.b64decode(data, validate=True)
    except (ValueError, TypeError) as exc:
        raise GeminiError(
            "Gemini returned an audio block whose base64 payload could not be"
            f" decoded ({exc}) — nothing was written; regenerate the asset.",
            retryable=False,
        ) from exc
