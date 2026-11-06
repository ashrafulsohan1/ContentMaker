"""Gemini video provider contract (BUILD_PLAN 4.1) — fully offline via MockTransport.

The provider is the third real Gemini adapter; it rides on the 2.2 client, so
these tests pin the adapter-level contract the job engine depends on, against
the task 4.0 research spike (research/gemini-video.md):

- **Submit → poll → download** (spike §2/§3/§4): one stateless
  ``POST /v1beta/interactions`` per beat with
  ``response_format = {type: video, aspect_ratio, resolution, delivery: uri}``
  (docs: URI delivery for videos larger than 4MB). The video URI is captured
  from the **creation response** (spike §3.2 gotcha: a refetch of
  ``GET /v1beta/interactions/{id}`` returns inline base64 even for URI
  deliveries — the URI is only guaranteed in the creation response). The file
  behind the URI is polled at ``GET /v1beta/files/{id}`` until ``state ==
  "ACTIVE"`` (docs' own 5 s cadence, bounded budget), then downloaded via the
  client, which attaches the key to the Gemini host only (§6.5).
- **Background polling** (spike §3.1): ``background=True`` sends the
  documented async alternative (``background: true`` + the required
  ``store: true``); the immediate response carries an id and a non-terminal
  status, and ``GET /v1beta/interactions/{id}`` is polled until a terminal
  state. Non-completed terminal states are permanent.
- **Clip bounds** (spike §6 [E]): 1.2 renders 3–15 s — beats outside that are
  rejected **before any traffic** with the remedy named (merge/split), never
  after spend.
- **Real durations** (BUILD_PLAN §1.2 "Durations"): nothing the response says
  about timing is trusted — the written file is ffprobed and the *probed*
  value is what ``VideoResult.duration_s`` carries.
- **Per-second cost model** (spike §5/§7.5): the estimate is
  ``cost.estimate_video_cost`` against the editable price table (fractional
  seconds ceiled up) — never a hardcoded number — and prices.json carries the
  resolution the provider actually requests (720p, $0.07/s).
- **Errors** propagate as ``GeminiError`` (a ``ProviderError``) and say
  exactly what is wrong; a key-less provider never puts a byte on the wire.

Fixtures are the documented response shapes from the research spike (§1
envelope, §3.2 file states) — no live API calls exist (no key), so the
contract is pinned offline against the spike's documented shapes.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.assembly.ffmpeg import FFmpegError
from app.core.cost import estimate_video_cost, load_prices
from app.core.keys import KeyStore
from app.engines.base import VideoRequest, VideoResult
from app.engines.registry import Registry
from app.providers.gemini.client import GeminiClient, GeminiError
from app.providers.gemini.video import (
    DEFAULT_MAX_POLL_S,
    DEFAULT_POLL_INTERVAL_S,
    GeminiVideoProvider,
    compose_video_prompt,
)

KEY = "AIza" + "0123456789abcdef" * 3  # realistic shape, never a real key
INTERACTIONS_PATH = "/v1beta/interactions"
FILE_ID = "abc123video"
FILE_URI = f"https://generativelanguage.googleapis.com/v1beta/files/{FILE_ID}:download?alt=media"
OUT_MP4 = Path("visuals/beat_01.mp4")  # rewritten into tmp_path by the autouse fixture


@pytest.fixture(autouse=True)
def out_path_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The provider really writes the MP4 — keep the default out_path inside
    this test's tmp_path (absolute, per-test, self-cleaning)."""
    monkeypatch.setattr(sys.modules[__name__], "OUT_MP4", tmp_path / "visuals" / "beat_01.mp4")


# --- fixtures: documented response shapes (research spike §1, §3.2) ---------------

#: A minimal-but-realistic MP4 header — model-delivered video bytes as the
#: Files API download would serve them (ftyp box first, per ISO-BMFF).
MP4_BYTES = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 8


def mp4_b64(payload: bytes = MP4_BYTES) -> str:
    return base64.b64encode(payload).decode("ascii")


_UNSET = object()  # sentinel: "argument not passed" (a default uri vs None)


def creation_response(
    *,
    uri: Any = _UNSET,
    data: str | None = None,
    status: str = "completed",
    blocks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The unary interaction envelope (spike §1): the video lives in
    ``steps[]`` blocks of ``type: "model_output"``. URI delivery carries the
    ``uri`` (and per the spike §3.2 gotcha it must be captured here — a
    refetch would return inline base64 instead); inline delivery carries
    base64 ``data``. ``data`` suppresses the default ``uri`` (one delivery
    mode per response); an explicit ``uri=`` always wins."""
    if blocks is None:
        block: dict[str, Any] = {"type": "video", "mime_type": "video/mp4"}
        if uri is _UNSET:
            uri = None if data is not None else FILE_URI
        if data is not None:
            block["data"] = data
        if uri is not None:
            block["uri"] = uri
        blocks = [block]
    return {
        "id": "v1_test_interaction",
        "object": "interaction",
        "status": status,
        "model": "gemini-omni-1.2-flash",
        "steps": [{"type": "model_output", "content": blocks}],
    }


def file_response(state: str) -> dict[str, Any]:
    """The Files API get shape (spike §3.2 [I]): ``state`` of
    PROCESSING / ACTIVE / FAILED plus output-only uri/downloadUri/expireTime."""
    return {
        "name": f"files/{FILE_ID}",
        "state": state,
        "uri": FILE_URI,
        "downloadUri": FILE_URI,
        "expireTime": "2026-11-08T00:00:00Z",
    }


def gemini_error_response(code: str, message: str, *, status: int) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message}})


# --- harness -----------------------------------------------------------------------


class FakeProbe:
    """Stands in for ffprobe offline; records every path it was asked about."""

    def __init__(self, duration_s: float = 8.0) -> None:
        self.duration_s = duration_s
        self.probed: list[Path] = []

    def __call__(self, path: Path | str) -> float:
        self.probed.append(Path(path))
        return self.duration_s


class FakeMonotonic:
    """Deterministic clock: advances by a fixed step per call."""

    def __init__(self, step_s: float) -> None:
        self.step_s = step_s
        self.now = 0.0

    def __call__(self) -> float:
        self.now += self.step_s
        return self.now


class FakeSleep:
    """Records sleep calls without actually sleeping."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


_FAKE = object()  # sentinel: inject FakeProbe (offline tests probe nothing real)


def make_provider(
    handler: Any,
    *,
    key_source: Any = KEY,
    probe: Any = _FAKE,
    **kwargs: Any,
) -> tuple[list[httpx.Request], GeminiVideoProvider]:
    requests: list[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    client = GeminiClient(key_source, transport=httpx.MockTransport(recording_handler))
    provider = GeminiVideoProvider(client, probe=FakeProbe() if probe is _FAKE else probe, **kwargs)
    return requests, provider


def make_request(**overrides: Any) -> VideoRequest:
    values: dict[str, Any] = {
        "prompt": "A lighthouse at dawn, waves crashing",
        "duration_s": 8.0,
        "width": 1920,
        "height": 1080,
        "out_path": OUT_MP4,
    }
    values.update(overrides)
    return VideoRequest(**values)


def generate_with(
    handler: Any,
    request: VideoRequest | None = None,
    probe: Any = _FAKE,
    **kwargs: Any,
) -> tuple[list[httpx.Request], VideoResult]:
    requests, provider = make_provider(handler, probe=probe, **kwargs)
    result = provider.generate(request or make_request())
    return requests, result


def request_body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content.decode("utf-8"))


def unary_handler(**response_kwargs: Any) -> Any:
    """A handler for the happy unary flow: the POST gets the given creation
    response, the files-metadata GET reports ACTIVE immediately, and the
    download GET serves the MP4 bytes."""
    response = creation_response(**response_kwargs)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=response)
        if request.url.path == f"/v1beta/files/{FILE_ID}":
            return httpx.Response(200, json=file_response("ACTIVE"))
        return httpx.Response(200, content=MP4_BYTES)

    return handler


# --- request shape (spike §2, §7.1) --------------------------------------------------


def test_request_shape_matches_the_research_spike():
    requests, _ = generate_with(unary_handler())
    # one POST per beat (per-beat video is a §2 constraint), then the two
    # follow-ups of the URI flow: files metadata (ACTIVE immediately) + download
    assert [r.method for r in requests] == ["POST", "GET", "GET"]
    sent = requests[0]
    assert sent.method == "POST"
    assert sent.url.host == "generativelanguage.googleapis.com"
    assert sent.url.path == INTERACTIONS_PATH
    assert sent.headers["x-goog-api-key"] == KEY  # §6.5: key only in the header
    assert "key" not in sent.url.params  # never in the query string

    body = request_body(sent)
    assert body["model"] == "gemini-omni-1.2-flash"
    assert body["input"] == [
        {
            "type": "user_input",
            "content": [
                {
                    "type": "text",
                    "text": compose_video_prompt(
                        "A lighthouse at dawn, waves crashing", duration_s=8.0
                    ),
                }
            ],
        }
    ]
    assert body["response_format"] == {
        "type": "video",
        "aspect_ratio": "16:9",
        "resolution": "720p",
        "delivery": "uri",
    }
    # Spike §7.1: prompt-first (no `task`), docs' defaults for
    # background/store/stream — no overrides on the v1 unary path.
    for absent in ("task", "background", "store", "stream", "generation_config"):
        assert absent not in body


def test_beat_prompt_is_sent_verbatim_with_the_documented_clauses_appended():
    """Spike §7.3: the beat prompt stays at the head verbatim; the fixed
    single-scene instruction and the no-dialogue audio description are
    appended (assembly discards the generated track in favor of per-beat
    TTS); clip length rides the documented natural-language timing surface."""
    text = compose_video_prompt("A red balloon drifts upward", duration_s=7.4)
    assert text.startswith("A red balloon drifts upward")
    assert "no scene cuts" in text.lower()  # single-scene (spike §2 prompting notes)
    assert "no dialogue" in text.lower()  # assembly uses per-beat TTS audio
    assert "7 seconds" in text  # duration_s=7.4 → "about 7 seconds"


def test_model_comes_from_the_price_table_not_a_hardcoded_id():
    """Model ids are config data (BUILD_PLAN §0 line 26) — the request must
    carry whatever prices.json says, and prices.json must be the shipped
    spike-4.0-verified video model."""
    prices = load_prices()
    configured = prices.price_for("gemini", "video").model
    assert configured, "prices.json must carry the verified video model id"

    requests, _ = generate_with(unary_handler())
    assert request_body(requests[0])["model"] == configured
    assert configured == "gemini-omni-1.2-flash"  # spike 4.0 §5 (verified 2026-11-06)


def test_explicit_model_override_wins_over_the_price_table():
    requests, _ = generate_with(unary_handler(), model="gemini-omni-1.1-flash")
    assert request_body(requests[0])["model"] == "gemini-omni-1.1-flash"


# --- aspect ratio mapping (spike §2: 16:9 / 9:16 only) -------------------------------


@pytest.mark.parametrize(
    ("width", "height", "expected"),
    [
        (1920, 1080, "16:9"),  # every catalog 16:9 video format
        (1080, 1920, "9:16"),  # every catalog 9:16 video format
        (1280, 720, "16:9"),  # exact reduction
        (720, 1280, "9:16"),
        (1024, 1024, "16:9"),  # off-table square → deterministic tie-break
        (1200, 630, "16:9"),  # off-table landscape → nearest
        (630, 1200, "9:16"),  # off-table portrait → nearest
    ],
)
def test_aspect_ratio_maps_onto_the_two_documented_ratios(width: int, height: int, expected: str):
    requests, _ = generate_with(unary_handler(), request=make_request(width=width, height=height))
    assert request_body(requests[0])["response_format"]["aspect_ratio"] == expected


# --- clip-duration bounds are enforced BEFORE any traffic (spike §6) -----------------


@pytest.mark.parametrize("duration_s", [2.9, 16.0, 100.0])
def test_beats_outside_the_3_to_15s_window_are_rejected_before_any_traffic(
    duration_s: float,
):
    requests, provider = make_provider(unary_handler())
    with pytest.raises(GeminiError, match="3.*15|15.*3") as exc_info:
        provider.generate(make_request(duration_s=duration_s))
    assert exc_info.value.retryable is False
    assert requests == []  # nothing hit the wire


def test_out_of_window_error_names_the_remedy():
    _, provider = make_provider(unary_handler())
    with pytest.raises(GeminiError) as exc_info:
        provider.generate(make_request(duration_s=16.0))
    assert "split" in str(exc_info.value.message).lower()
    with pytest.raises(GeminiError) as exc_info:
        provider.generate(make_request(duration_s=2.0))
    assert "merge" in str(exc_info.value.message).lower()


@pytest.mark.parametrize("duration_s", [3.0, 15.0])
def test_boundary_durations_are_accepted(duration_s: float):
    requests, _ = generate_with(unary_handler(), request=make_request(duration_s=duration_s))
    assert len(requests) >= 1


def test_non_finite_duration_is_rejected_before_any_traffic():
    _, provider = make_provider(unary_handler())
    for bad in (float("nan"), float("inf")):
        with pytest.raises(GeminiError):
            provider.generate(make_request(duration_s=bad))


# --- other before-traffic guards ------------------------------------------------------


def test_blank_prompt_is_rejected_before_any_traffic():
    requests, provider = make_provider(unary_handler())
    with pytest.raises(GeminiError, match="prompt"):
        provider.generate(make_request(prompt="   "))
    assert requests == []


def test_non_positive_dimensions_are_rejected_before_any_traffic():
    _, provider = make_provider(unary_handler())
    with pytest.raises(GeminiError):
        provider.generate(make_request(width=0))
    with pytest.raises(GeminiError):
        provider.generate(make_request(height=-5))


def test_non_mp4_output_path_is_rejected_before_any_traffic():
    requests, provider = make_provider(unary_handler())
    with pytest.raises(GeminiError, match="mp4"):
        provider.generate(make_request(out_path=OUT_MP4.with_suffix(".mov")))
    assert requests == []


def test_key_less_provider_fails_before_any_traffic():
    requests, provider = make_provider(unary_handler(), key_source="")
    with pytest.raises(GeminiError, match="Settings"):
        provider.generate(make_request())
    assert requests == []


# --- happy path: URI captured from creation, file polled to ACTIVE, downloaded -------


def test_uri_delivery_polls_files_to_active_then_downloads_and_probes(tmp_path: Path):
    out_path = tmp_path / "visuals" / "beat_01.mp4"
    states = iter(["PROCESSING", "PROCESSING", "ACTIVE"])
    sleep = FakeSleep()
    clock = FakeMonotonic(DEFAULT_POLL_INTERVAL_S)
    probe = FakeProbe(duration_s=8.0)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=creation_response())
        if request.url.path == f"/v1beta/files/{FILE_ID}":
            assert request.headers["x-goog-api-key"] == KEY
            return httpx.Response(200, json=file_response(next(states)))
        return httpx.Response(200, content=MP4_BYTES)  # the download

    requests, result = generate_with(
        handler,
        request=make_request(out_path=out_path),
        probe=probe,
        sleep=sleep,
        monotonic=clock,
    )
    paths = [(r.method, r.url.path) for r in requests]
    assert paths == [
        ("POST", INTERACTIONS_PATH),
        ("GET", f"/v1beta/files/{FILE_ID}"),
        ("GET", f"/v1beta/files/{FILE_ID}"),
        ("GET", f"/v1beta/files/{FILE_ID}"),
        ("GET", f"/v1beta/files/{FILE_ID}:download"),
    ]
    # the spike §3.2 gotcha: the URI is captured from the creation response —
    # the unary flow never refetches GET /v1beta/interactions/{id}
    assert all(not r.url.path.startswith("/v1beta/interactions/") for r in requests[1:])
    assert out_path.read_bytes() == MP4_BYTES
    assert isinstance(result, VideoResult)
    assert result.file == out_path
    assert result.duration_s == 8.0  # the probe's value, not the response's
    assert probe.probed == [out_path]
    assert sleep.calls == [5.0, 5.0]  # docs' own 5 s cadence between checks


def test_file_id_is_parsed_from_the_delivery_uri_with_its_query_suffix():
    """The creation URI carries ``:download?alt=media`` — the metadata poll
    must hit ``/v1beta/files/{id}`` without them."""
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.method == "POST":
            return httpx.Response(200, json=creation_response())
        if request.url.path == f"/v1beta/files/{FILE_ID}":
            return httpx.Response(200, json=file_response("ACTIVE"))
        return httpx.Response(200, content=MP4_BYTES)

    generate_with(handler)
    assert f"/v1beta/files/{FILE_ID}" in seen_paths


def test_inline_data_delivery_skips_the_files_poll_entirely(tmp_path: Path):
    """Small videos may arrive inline (base64) in the creation response — no
    URI, no files poll, no download call; the bytes decode straight to disk."""
    out_path = tmp_path / "beat_inline.mp4"
    requests, result = generate_with(
        unary_handler(data=mp4_b64()), request=make_request(out_path=out_path)
    )
    assert len(requests) == 1  # only the POST
    assert out_path.read_bytes() == MP4_BYTES
    assert result.file == out_path


def test_uri_is_captured_from_the_creation_response_even_when_refetch_returns_inline():
    """Spike §3.2 gotcha, pinned: the provider must NOT refetch the interaction
    to find the URI — a refetch returns inline base64 even for URI deliveries.
    The unary flow must go creation → files poll → download, with the file
    served from the captured URI."""
    interactions_gets = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal interactions_gets
        if request.method == "GET" and request.url.path.startswith("/v1beta/interactions/"):
            interactions_gets += 1  # a refetch would return inline data here
            return httpx.Response(200, json=creation_response(data=mp4_b64("bogus")))
        if request.method == "POST":
            return httpx.Response(200, json=creation_response())
        if request.url.path == f"/v1beta/files/{FILE_ID}":
            return httpx.Response(200, json=file_response("ACTIVE"))
        return httpx.Response(200, content=MP4_BYTES)

    _, result = generate_with(handler)
    assert interactions_gets == 0
    assert result.duration_s == 8.0  # probe ran → real bytes landed on disk


def test_failed_file_state_is_a_permanent_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=creation_response())
        if request.url.path == f"/v1beta/files/{FILE_ID}":
            return httpx.Response(200, json=file_response("FAILED"))
        raise AssertionError("no download should follow a FAILED file")

    requests, provider = make_provider(handler)
    with pytest.raises(GeminiError) as exc_info:
        provider.generate(make_request())
    assert exc_info.value.retryable is False
    assert FILE_ID in str(exc_info.value.message)
    assert len(requests) == 2  # POST + one files GET, no download


def test_poll_budget_exhaustion_is_a_retryable_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=creation_response())
        return httpx.Response(200, json=file_response("PROCESSING"))

    sleep = FakeSleep()
    clock = FakeMonotonic(step_s=10.0)
    requests, provider = make_provider(handler, sleep=sleep, monotonic=clock, max_poll_s=30.0)
    with pytest.raises(GeminiError) as exc_info:
        provider.generate(make_request())
    assert exc_info.value.retryable is True  # the file may still become ACTIVE
    assert FILE_ID in str(exc_info.value.message)
    assert sleep.calls  # the docs' cadence was honored between checks
    assert all(method == "GET" for method, *_ in [(r.method,) for r in requests[1:]])


def test_default_poll_settings_match_the_spike():
    assert DEFAULT_POLL_INTERVAL_S == 5.0  # docs' own cadence (spike §3.2)
    assert DEFAULT_MAX_POLL_S == 900.0  # spike §7.2: cap ~15 min until measured


# --- response-shape failures are permanent (nothing guessed or spliced) --------------


def test_completed_response_without_a_video_block_is_permanent():
    response = creation_response()
    response["steps"][0]["content"] = [{"type": "text", "text": "no video here"}]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    with pytest.raises(GeminiError, match="no video") as exc_info:
        generate_with(handler)
    assert exc_info.value.retryable is False


def test_multiple_video_blocks_are_refused_not_spliced():
    blocks = [
        {"type": "video", "mime_type": "video/mp4", "data": mp4_b64(MP4_BYTES)},
        {"type": "video", "mime_type": "video/mp4", "data": mp4_b64(MP4_BYTES)},
    ]
    response = creation_response(blocks=blocks)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    with pytest.raises(GeminiError, match="2 video blocks") as exc_info:
        generate_with(handler)
    assert exc_info.value.retryable is False


def test_undecodable_base64_video_data_is_permanent():
    response = creation_response(data="not-base64!!!")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    with pytest.raises(GeminiError, match="base64") as exc_info:
        generate_with(handler)
    assert exc_info.value.retryable is False


def test_uri_that_cannot_be_parsed_for_a_file_id_is_permanent():
    response = creation_response(uri="https://generativelanguage.googleapis.com/nope")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=response)
        raise AssertionError("no request should follow an unparseable URI")

    with pytest.raises(GeminiError, match="file id") as exc_info:
        generate_with(handler)
    assert exc_info.value.retryable is False


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_terminal_non_completed_creation_status_is_permanent(status: str):
    response = creation_response(status=status)
    response.pop("steps")  # terminal failures carry no video

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    with pytest.raises(GeminiError, match=status) as exc_info:
        generate_with(handler)
    assert exc_info.value.retryable is False


def test_requires_action_is_permanent_the_app_cannot_answer_it():
    response = creation_response(status="requires_action")
    response.pop("steps")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    with pytest.raises(GeminiError, match="requires_action") as exc_info:
        generate_with(handler)
    assert exc_info.value.retryable is False


def test_http_error_maps_through_the_client_error_taxonomy():
    def handler(request: httpx.Request) -> httpx.Response:
        return gemini_error_response("rate_limit_exceeded", "slow down", status=429)

    with pytest.raises(GeminiError) as exc_info:
        generate_with(handler)
    assert exc_info.value.retryable is True
    assert exc_info.value.http_status == 429


# --- background polling (spike §3.1: the documented async alternative) ---------------


def test_background_true_sends_the_documented_async_request_shape():
    """``background: true`` returns an id immediately; it requires
    ``store: true`` (combining it with store:false is a documented 400)."""
    requests, _ = generate_with(unary_handler(), background=True)
    body = request_body(requests[0])
    assert body["background"] is True
    assert body["store"] is True


def test_background_flow_polls_the_interaction_until_completed(tmp_path: Path):
    out_path = tmp_path / "visuals" / "bg_beat.mp4"
    states = iter(["in_progress", "in_progress", "completed"])
    sleep = FakeSleep()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = creation_response(status="in_progress")
            body.pop("steps")  # the immediate background response carries an id only
            return httpx.Response(200, json=body)
        if request.url.path == "/v1beta/interactions/v1_test_interaction":
            status = next(states)
            if status == "completed":
                return httpx.Response(200, json=creation_response())
            return httpx.Response(200, json={"id": "v1_test_interaction", "status": status})
        if request.url.path == f"/v1beta/files/{FILE_ID}":
            return httpx.Response(200, json=file_response("ACTIVE"))
        return httpx.Response(200, content=MP4_BYTES)

    requests, result = generate_with(
        handler,
        request=make_request(out_path=out_path),
        background=True,
        sleep=sleep,
        monotonic=FakeMonotonic(DEFAULT_POLL_INTERVAL_S),
    )
    interaction_polls = [r for r in requests if r.url.path.startswith("/v1beta/interactions/")]
    assert len(interaction_polls) == 3  # in_progress, in_progress, completed
    assert out_path.read_bytes() == MP4_BYTES
    assert result.file == out_path
    assert sleep.calls[:2] == [5.0, 5.0]  # docs' 5 s interaction cadence


def test_background_poll_budget_exhaustion_is_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = creation_response(status="in_progress")
            body.pop("steps")
            return httpx.Response(200, json=body)
        if request.url.path.startswith("/v1beta/interactions/"):
            return httpx.Response(200, json={"id": "v1_test_interaction", "status": "in_progress"})
        raise AssertionError("no download should follow a stuck interaction")

    _, provider = make_provider(
        handler,
        background=True,
        sleep=FakeSleep(),
        monotonic=FakeMonotonic(step_s=10.0),
        max_poll_s=30.0,
    )
    with pytest.raises(GeminiError) as exc_info:
        provider.generate(make_request())
    assert exc_info.value.retryable is True
    assert exc_info.value.provider_id == "gemini"


def test_background_polled_failure_state_is_permanent():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = creation_response(status="in_progress")
            body.pop("steps")
            return httpx.Response(200, json=body)
        return httpx.Response(200, json={"id": "v1_test_interaction", "status": "failed"})

    _, provider = make_provider(
        handler,
        background=True,
        sleep=FakeSleep(),
        monotonic=FakeMonotonic(step_s=1.0),
    )
    with pytest.raises(GeminiError, match="failed") as exc_info:
        provider.generate(make_request())
    assert exc_info.value.retryable is False


def test_interaction_without_an_id_cannot_be_polled_is_permanent():
    response = creation_response(status="in_progress")
    response.pop("steps")
    response.pop("id")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    with pytest.raises(GeminiError, match="id") as exc_info:
        generate_with(handler)
    assert exc_info.value.retryable is False


# --- durations: probed, never trusted (§1.2) ------------------------------------------


def test_provider_reported_timing_fields_in_the_response_are_ignored():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = creation_response()
            body["usage_metadata"] = {"total_video_seconds": 99}
            body["steps"][0]["content"][0]["duration"] = "99s"
            return httpx.Response(200, json=body)
        return httpx.Response(200, json=file_response("ACTIVE"))

    probe = FakeProbe(duration_s=7.999)
    _, result = generate_with(handler, probe=probe)
    assert result.duration_s == 7.999


def test_unplayable_file_probe_failure_is_permanent(tmp_path: Path):
    out_path = tmp_path / "visuals" / "beat_01.mp4"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=creation_response(data=mp4_b64()))
        return httpx.Response(200, content=MP4_BYTES)

    def bad_probe(path: Path | str) -> float:
        raise FFmpegError("ffprobe exploded")

    _, provider = make_provider(handler, probe=bad_probe)
    with pytest.raises(GeminiError, match="playable") as exc_info:
        provider.generate(make_request(out_path=out_path))
    assert exc_info.value.retryable is False


def test_non_positive_probed_duration_is_permanent(tmp_path: Path):
    out_path = tmp_path / "visuals" / "beat_01.mp4"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=creation_response(data=mp4_b64()))
        return httpx.Response(200, content=MP4_BYTES)

    _, provider = make_provider(handler, probe=FakeProbe(duration_s=0.0))
    with pytest.raises(GeminiError, match="probes as") as exc_info:
        provider.generate(make_request(out_path=out_path))
    assert exc_info.value.retryable is False


# --- per-second cost model (spike §5/§7.5) --------------------------------------------


def test_estimate_is_the_price_table_per_second_math_never_hardcoded():
    request = make_request(duration_s=10.0)
    _, provider = make_provider(unary_handler())
    expected = estimate_video_cost(load_prices(), "gemini", duration_s=10.0)
    assert provider.estimate_cost_usd(request) == expected


def test_estimate_ceils_fractional_seconds_and_matches_the_documented_10s_price():
    """Spike §5: docs price 720p at ~$0.07/s — '~$0.70 for a 10s clip'. The
    estimate for exactly 10 s must be that number; 5.2 s bills as 6 s."""
    _, provider = make_provider(unary_handler())
    assert provider.estimate_cost_usd(make_request(duration_s=10.0)) == 0.70
    assert provider.estimate_cost_usd(make_request(duration_s=5.2)) == pytest.approx(0.42)
    assert provider.estimate_cost_usd(make_request(duration_s=0.4)) == 0.07


def test_price_table_carries_the_resolution_the_provider_requests():
    """Spike §7.5: the estimate must be derived from the resolution actually
    requested — the provider always requests 720p, and prices.json prices
    720p ($12.00/1M × 5,792 tok/s ≈ $0.0695 → 0.07). If the provider ever
    offers another resolution, the table and this pin must move with it."""
    from app.providers.gemini.video import DEFAULT_RESOLUTION

    assert DEFAULT_RESOLUTION == "720p"
    price = load_prices().price_for("gemini", "video")
    assert price.unit == "usd_per_second"
    assert price.price == 0.07
    assert price.model == "gemini-omni-1.2-flash"


# --- wiring ---------------------------------------------------------------------------


def test_provider_satisfies_the_videoprovider_protocol_and_registry_slot():
    _, provider = make_provider(unary_handler())
    assert provider.provider_id == "gemini"
    registry = Registry()
    registry.register("video", provider)
    assert registry.get("video", "gemini") is provider


def test_provider_needs_a_gemini_client():
    with pytest.raises(ValueError, match="GeminiClient"):
        GeminiVideoProvider("not-a-client")  # type: ignore[arg-type]


def test_keystore_backed_client_reads_the_key_at_call_time(tmp_path: Path):
    """The KeyStore-backed client resolves the key per call (§6.5) — a store
    with no key yet fails with the add-your-key message before any traffic."""
    store = KeyStore(tmp_path / "keys.json", provider_engines={"gemini": ("video",)})
    requests, provider = make_provider(unary_handler(), key_source=store)
    with pytest.raises(GeminiError, match="Settings"):
        provider.generate(make_request())
    assert requests == []


def test_invalid_poll_settings_are_rejected_at_wiring_time():
    with pytest.raises(ValueError, match="interval"):
        make_provider(unary_handler(), poll_interval_s=0)
    with pytest.raises(ValueError, match="budget"):
        make_provider(unary_handler(), max_poll_s=-1)
