"""Gemini TTS provider contract (BUILD_PLAN 2.3) — fully offline via MockTransport.

The provider is the first real Gemini adapter; it rides on the 2.2 client, so
these tests pin the adapter-level contract the job engine depends on:

- **Per-beat request shape** (research spike §2.2, research/gemini-api.md):
  one POST /v1beta/interactions per beat — the beat text goes in **verbatim**
  (stage directions never belong in the text), the voice in
  ``generation_config.speech_config``, and pace/tone — which Gemini controls
  only through natural language, there are **no numeric rate/pitch params**
  (spike §2.3) — as one short ``speech_metadata.style`` string. The model id
  comes from ``app/data/prices.json`` (config data, never hardcoded —
  BUILD_PLAN §0 line 26).
- **Real durations** (BUILD_PLAN §1.2 "Durations"; task 2.4): nothing the
  response says about timing is trusted — the written file is ffprobed and
  the *probed* value is what ``TTSResult.duration_s`` carries. The manifest
  itself is written from probed values in ``scripting.manifest`` (2.4).
- **Errors** propagate as ``GeminiError`` (a ``ProviderError``, so jobs.py's
  bounded retry and per-asset messages work unchanged) and say exactly what
  is missing; a key-less provider never puts a single byte on the wire.
- **Costs** come from the conservative ``cost.estimate_tts_cost`` estimator
  against the editable price table — never a hardcoded number.

Fixtures are the recorded response shapes from the research spike (§1.1
envelope, §2.4 WAV output) — no live API calls exist (no key), so the
contract is pinned against the spike's documented shapes, offline.
"""

from __future__ import annotations

import base64
import io
import sys
import wave
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.assembly.ffmpeg import FFmpegError
from app.core.cost import load_prices
from app.core.keys import KeyStore
from app.engines.base import TTSRequest, TTSResult
from app.providers.gemini.client import GeminiClient, GeminiError
from app.providers.gemini.tts import GeminiTTSProvider, pace_style_phrase

KEY = "AIza" + "0123456789abcdef" * 3  # realistic shape, never a real key
INTERACTIONS_PATH = "/v1beta/interactions"
OUT_WAV = Path("beat_01.wav")  # rewritten into tmp_path by the autouse fixture below


@pytest.fixture(autouse=True)
def out_path_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The provider really writes the WAV — keep the default out_path inside
    this test's tmp_path (absolute, per-test, self-cleaning)."""
    monkeypatch.setattr(sys.modules[__name__], "OUT_WAV", tmp_path / "audio" / "beat_01.wav")

# --- fixtures: recorded response shapes (research spike §1.1, §2.4) --------------

#: A tiny well-formed RIFF/WAVE header + payload — audio bytes as Google's
#: unary WAV default would deliver them (spike §2.4: 24 kHz mono 16-bit LE PCM).
WAV_BYTES = b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00" + b"\x00" * 16


def wav_b64(payload: bytes = WAV_BYTES) -> str:
    return base64.b64encode(payload).decode("ascii")


def interaction_response(
    audio_b64: str,
    *,
    model: str = "gemini-3.8-flash-tts",
    status: str = "completed",
    steps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The unary interaction envelope from spike §1.1: audio lives in
    ``steps[]`` blocks of ``type: "model_output"`` — the SDK-only
    ``output_audio`` convenience field is absent on the REST surface."""
    if steps is None:
        steps = [
            {
                "type": "model_output",
                "content": [{"type": "audio", "mime_type": "audio/wav", "data": audio_b64}],
            }
        ]
    return {
        "id": "v1_test_interaction",
        "object": "interaction",
        "status": status,
        "model": model,
        "steps": steps,
    }


def gemini_error_response(code: str, message: str, *, status: int) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message}})


# --- harness -----------------------------------------------------------------------


class FakeProbe:
    """Stands in for ffprobe offline; records every path it was asked about."""

    def __init__(self, duration_s: float = 2.345) -> None:
        self.duration_s = duration_s
        self.probed: list[Path] = []

    def __call__(self, path: Path | str) -> float:
        self.probed.append(Path(path))
        return self.duration_s


_FAKE = object()  # sentinel: inject FakeProbe (offline tests probe nothing real)


def make_provider(
    handler: Any,
    *,
    key_source: Any = KEY,
    probe: Any = _FAKE,
    **kwargs: Any,
) -> tuple[list[httpx.Request], GeminiTTSProvider]:
    requests: list[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    client = GeminiClient(key_source, transport=httpx.MockTransport(recording_handler))
    provider = GeminiTTSProvider(client, probe=FakeProbe() if probe is _FAKE else probe, **kwargs)
    return requests, provider


def make_request(**overrides: Any) -> TTSRequest:
    values: dict[str, Any] = {
        "text": "Have a wonderful day!",
        "voice": "Kore",
        "out_path": OUT_WAV,
    }
    values.update(overrides)
    return TTSRequest(**values)


def generate_with(
    handler: Any,
    probe: Any = _FAKE,
    request: TTSRequest | None = None,
    **kwargs: Any,
) -> tuple[list[httpx.Request], TTSResult]:
    requests, provider = make_provider(handler, probe=probe, **kwargs)
    result = provider.generate(request or make_request())
    return requests, result


def request_body(request: httpx.Request) -> dict[str, Any]:
    import json

    return json.loads(request.content.decode("utf-8"))


# --- request shape (spike §2.2) ------------------------------------------------------


def test_request_shape_matches_the_research_spike():
    requests, _ = generate_with(
        lambda request: httpx.Response(200, json=interaction_response(wav_b64()))
    )
    assert len(requests) == 1  # one HTTP call per beat — per-beat TTS is a §2 constraint
    sent = requests[0]
    assert sent.method == "POST"
    assert sent.url.host == "generativelanguage.googleapis.com"
    assert sent.url.path == INTERACTIONS_PATH
    assert sent.headers["x-goog-api-key"] == KEY  # §6.5: key only in the header
    assert "key" not in sent.url.params  # never in the query string

    body = request_body(sent)
    assert body["model"] == "gemini-3.8-flash-tts"
    assert body["input"] == [
        {
            "type": "user_input",
            "content": [{"type": "text", "text": "Have a wonderful day!"}],
        }
    ]
    assert body["generation_config"] == {"speech_config": [{"voice": "Kore"}]}
    assert body["response_format"] == {
        "type": "audio",
        "mime_type": "audio/wav",
        "sample_rate": 24000,
    }


def test_model_comes_from_the_price_table_not_a_hardcoded_id():
    """Model ids are config data (BUILD_PLAN §0 line 26) — the request must
    carry whatever prices.json says, and prices.json must be the shipped
    spike-verified TTS model."""
    prices = load_prices()
    configured = prices.price_for("gemini", "tts").model
    assert configured, "prices.json must carry the verified TTS model id"

    requests, _ = generate_with(
        lambda request: httpx.Response(200, json=interaction_response(wav_b64()))
    )
    assert request_body(requests[0])["model"] == configured
    assert configured == "gemini-3.8-flash-tts"  # spike §2.1 (verified 2026-11-06)


def test_explicit_model_override_wins_over_the_price_table():
    requests, _ = generate_with(
        lambda request: httpx.Response(200, json=interaction_response(wav_b64())),
        model="gemini-3.8-flash-lite-tts",
    )
    assert request_body(requests[0])["model"] == "gemini-3.8-flash-lite-tts"


def test_beat_text_is_sent_verbatim_with_no_stage_directions_mixed_in():
    """Spike §2.3: the text field is a verbatim transcript — the adapter must
    never append pacing/style instructions to it."""
    text = "Three... two... one. LIFTOFF!"
    requests, _ = generate_with(
        lambda request: httpx.Response(200, json=interaction_response(wav_b64())),
        request=make_request(text=text, pace=2.0, tone="excited"),
    )
    content = request_body(requests[0])["input"][0]["content"]
    assert content[0]["text"] == text


# --- pace/tone → natural-language style (spike §2.3) ---------------------------------


@pytest.mark.parametrize(
    ("pace", "phrase"),
    [
        (0.1, "speaking very slowly"),
        (0.5, "speaking very slowly"),
        (0.6, "speaking slowly"),
        (0.85, "speaking slowly"),
        (1.0, ""),
        (1.1, ""),
        (1.2, "speaking somewhat rapidly"),
        (1.49, "speaking somewhat rapidly"),
        (1.5, "speaking rapidly"),
        (3.0, "speaking rapidly"),
    ],
)
def test_pace_maps_to_graded_natural_language_phrases(pace: float, phrase: str):
    assert pace_style_phrase(pace) == phrase


def test_tone_is_passed_verbatim_as_the_speech_metadata_style():
    requests, _ = generate_with(
        lambda request: httpx.Response(200, json=interaction_response(wav_b64())),
        request=make_request(tone="warm and conversational"),
    )
    content = request_body(requests[0])["input"][0]["content"][0]
    assert content["annotations"] == [
        {"type": "speech_metadata", "style": "warm and conversational"}
    ]


def test_pace_and_tone_combine_into_one_short_style_string():
    requests, _ = generate_with(
        lambda request: httpx.Response(200, json=interaction_response(wav_b64())),
        request=make_request(pace=1.3, tone="authoritative"),
    )
    content = request_body(requests[0])["input"][0]["content"][0]
    assert content["annotations"] == [
        {"type": "speech_metadata", "style": "authoritative, speaking somewhat rapidly"}
    ]


def test_neutral_pace_and_no_tone_send_no_style_annotation_at_all():
    """Spike §2.3: recommended workflow is an empty or one-short-string style —
    a neutral beat must not carry a constant style block."""
    requests, _ = generate_with(
        lambda request: httpx.Response(200, json=interaction_response(wav_b64()))
    )
    content = request_body(requests[0])["input"][0]["content"][0]
    assert "annotations" not in content


# --- success: file written, duration probed (never trusted) ---------------------------


def test_audio_bytes_are_written_and_duration_is_the_probed_value(tmp_path: Path):
    out_path = tmp_path / "audio" / "beat_01.wav"
    probe = FakeProbe(duration_s=2.345)
    _, result = generate_with(
        lambda request: httpx.Response(200, json=interaction_response(wav_b64())),
        probe=probe,
        request=make_request(out_path=out_path),
    )
    assert out_path.read_bytes() == WAV_BYTES
    assert isinstance(result, TTSResult)
    assert result.file == out_path
    assert result.duration_s == 2.345  # the probe's value, not the response's
    assert probe.probed == [out_path]  # the written file is what got probed


def test_provider_reported_timing_fields_in_the_response_are_ignored():
    """BUILD_PLAN §1.2 'Durations': never trust API-reported durations. Even a
    response that claims a duration must not influence TTSResult.duration_s."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = interaction_response(wav_b64())
        body["usage_metadata"] = {"total_audio_seconds": 99}
        body["steps"][0]["content"][0]["duration"] = "99s"
        return httpx.Response(200, json=body)

    probe = FakeProbe(duration_s=1.234)
    _, result = generate_with(handler, probe=probe)
    assert result.duration_s == 1.234


def test_audio_chunks_arrive_concatenated_in_order(tmp_path: Path):
    """Multiple audio blocks (chunked delivery) decode and join in order."""
    first, second = b"RIFF-first-chunk", b"second-chunk-tail"
    steps = [
        {"type": "user_input", "content": [{"type": "text", "text": "ignored"}]},
        {
            "type": "model_output",
            "content": [
                {"type": "audio", "mime_type": "audio/wav", "data": wav_b64(first)},
                {"type": "audio", "mime_type": "audio/wav", "data": wav_b64(second)},
            ],
        },
    ]
    out_path = tmp_path / "beat.wav"
    _, result = generate_with(
        lambda request: httpx.Response(200, json=interaction_response("", steps=steps)),
        request=make_request(out_path=out_path),
    )
    assert out_path.read_bytes() == first + second
    assert result.file == out_path


def test_uri_delivered_audio_is_downloaded_without_credentials():
    """Delivery by URI must download via the client — which attaches the key
    only to the Gemini API host, never to a third-party file host (§6.5)."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "generativelanguage.googleapis.com":
            steps = [
                {
                    "type": "model_output",
                    "content": [
                        {
                            "type": "audio",
                            "mime_type": "audio/wav",
                            "uri": "https://files.example.com/delivery/beat.wav",
                        }
                    ],
                }
            ]
            return httpx.Response(200, json=interaction_response("", steps=steps))
        return httpx.Response(200, content=WAV_BYTES)

    probe = FakeProbe()
    _, result = generate_with(handler, probe=probe)
    download = seen[-1]
    assert download.url.host == "files.example.com"
    assert "x-goog-api-key" not in download.headers
    assert probe.probed, "the downloaded file must still be probed"
    assert result.duration_s == probe.duration_s


# --- guards and errors -----------------------------------------------------------------


def test_missing_key_fails_before_any_traffic(tmp_path: Path):
    store = KeyStore(tmp_path / "keys.json", provider_engines={"gemini": ("tts",)})
    requests, provider = make_provider(
        lambda request: httpx.Response(200, json=interaction_response(wav_b64())),
        key_source=store,
    )
    with pytest.raises(GeminiError) as exc_info:
        provider.generate(make_request())
    assert requests == []  # zero bytes left the machine
    error = exc_info.value
    assert error.retryable is False
    assert "no gemini api key" in error.message.lower()
    assert "settings" in error.message.lower()


@pytest.mark.parametrize("field", ["text", "voice"])
def test_blank_text_or_voice_fails_before_any_traffic(field: str):
    requests, provider = make_provider(
        lambda request: httpx.Response(200, json=interaction_response(wav_b64()))
    )
    with pytest.raises(GeminiError) as exc_info:
        provider.generate(make_request(**{field: "   "}))
    assert requests == []
    error = exc_info.value
    assert error.retryable is False
    assert field in error.message.lower()


def test_completed_response_without_audio_names_what_is_missing():
    steps = [{"type": "model_output", "content": [{"type": "text", "text": "oops"}]}]
    requests, provider = make_provider(
        lambda request: httpx.Response(
            200, json=interaction_response("", steps=steps)
        )
    )
    with pytest.raises(GeminiError) as exc_info:
        provider.generate(make_request())
    assert requests == [requests[0]]  # the synthesis call itself went out
    error = exc_info.value
    assert error.retryable is False  # retrying the same request won't add audio
    assert "audio" in error.message.lower()
    assert "steps" in error.message.lower()  # names where the adapter looked


def test_non_completed_status_is_an_error():
    requests, provider = make_provider(
        lambda request: httpx.Response(
            200, json=interaction_response(wav_b64(), status="failed")
        )
    )
    with pytest.raises(GeminiError) as exc_info:
        provider.generate(make_request())
    error = exc_info.value
    assert error.retryable is False
    assert "failed" in error.message.lower()


@pytest.mark.parametrize(
    ("code", "status", "retryable"),
    [("authentication", 401, False), ("rate_limit_exceeded", 429, True), ("api_error", 500, True)],
)
def test_http_errors_propagate_with_their_retryability(code: str, status: int, retryable: bool):
    _, provider = make_provider(
        lambda request, code=code, status=status: gemini_error_response(
            code, "Google's own sentence.", status=status
        )
    )
    with pytest.raises(GeminiError) as exc_info:
        provider.generate(make_request())
    error = exc_info.value
    assert error.retryable is retryable
    assert error.http_status == status
    assert error.api_code == code
    assert str(status) in error.message
    assert KEY not in error.message


def test_unprobeable_audio_is_a_permanent_error_naming_the_file(tmp_path: Path):
    out_path = tmp_path / "beat.wav"

    def probe_raises(path: Path | str) -> float:
        raise FFmpegError(f"ffprobe failed for {path} (exit 1): Invalid data")

    requests, provider = make_provider(
        lambda request: httpx.Response(200, json=interaction_response(wav_b64())),
        probe=probe_raises,
    )
    with pytest.raises(GeminiError) as exc_info:
        provider.generate(make_request(out_path=out_path))
    error = exc_info.value
    assert error.retryable is False
    assert out_path.name in error.message
    assert "audio" in error.message.lower()  # says the synthesized audio is the problem
    assert "Invalid data" in error.message  # carries the probe's own evidence


# --- cost estimate -----------------------------------------------------------------------


def test_estimate_cost_uses_the_conservative_1k_char_estimator():
    """Whole 1k-char blocks, ceiled up, at the prices.json unit price — the
    same estimator the confirmation flow shows the user (§6.6)."""
    requests, provider = make_provider(lambda request: httpx.Response(200, json={}))
    price = load_prices().price_for("gemini", "tts").price

    assert provider.estimate_cost_usd(make_request(text="x" * 999)) == pytest.approx(price)
    assert provider.estimate_cost_usd(make_request(text="x" * 1001)) == pytest.approx(price * 2)


# --- default probe wiring (real ffprobe, still offline network-wise) ----------------------


def test_default_probe_is_real_ffprobe_on_a_real_wav(tmp_path: Path):
    """End-to-end wiring check with the real ffprobe binary: a real WAV goes
    through the provider, and duration_s is the file's true duration."""
    sample_rate = 8000
    frames = sample_rate // 2  # exactly 0.5 s of silence
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\x00\x00" * frames)
    real_wav = buffer.getvalue()

    out_path = tmp_path / "beat.wav"
    _, result = generate_with(
        lambda request: httpx.Response(
            200, json=interaction_response(base64.b64encode(real_wav).decode("ascii"))
        ),
        probe=None,  # the real default: app.assembly.ffmpeg.probe_duration
        request=make_request(out_path=out_path),
    )
    assert result.duration_s == pytest.approx(0.5, abs=0.01)


# --- registration (real TTS wiring; ledger decision from tasks 2.1/2.2) --------------------


def test_default_registry_registers_the_gemini_tts_provider_with_a_key_store(tmp_path: Path):
    from app.main import default_registry

    store = KeyStore(tmp_path / "keys.json", provider_engines={"gemini": ("tts",)})
    registry = default_registry(key_store=store)
    provider = registry.get("tts", "gemini")
    assert isinstance(provider, GeminiTTSProvider)
    assert provider.provider_id == "gemini"
    # the key is read at call time from the store — never baked in
    assert provider.client.api_key() is None


def test_default_registry_without_a_key_store_stays_mock_only():
    """Dry-run gate (§1.2): without a key store there is no Gemini adapter to
    reach the network with."""
    from app.main import default_registry

    registry = default_registry()
    assert [p.provider_id for p in registry.providers_for("tts")] == ["mock"]
