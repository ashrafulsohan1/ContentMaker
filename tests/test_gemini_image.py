"""Gemini image provider contract (BUILD_PLAN 3.1) — fully offline via MockTransport.

The provider is the second real Gemini adapter; it rides on the 2.2 client, so
these tests pin the adapter-level contract the job engine depends on. Every
rule traces to the task 3.0 research spike (research/gemini-image.md):

- **Request shape** (spike §2.1, §5.2): one stateless POST /v1beta/interactions
  per beat — ``response_format: {type: "image", mime_type, aspect_ratio,
  image_size}`` with **both dimension fields always explicit** (silent
  defaults are 1:1 squares / 1K — wrong for every platform format we ship);
  ``image_size`` is ``1K``/``2K``/``4K`` and **uppercase K is mandatory**
  (lowercase rejected by the API). ``image_size: "2K"`` on every render:
  1K is undersized on every platforms.json target (spike §2.3); assembly
  downscales 2K → platform dims (task 3.2).
- **Platform-correct dimensions** (spike §2.2, §5.3): the platform resolution
  maps to a supported aspect ratio by gcd reduction against the fixed
  per-model ratio table (14 ratios on Nano Banana 2.1, 10 on 3 Pro Image —
  no 1:4/4:1/1:8/8:1); pixel sizes are not free-form, so an off-table
  resolution takes the nearest supported ratio (assembly crops).
- **Style anchoring** (spike §4.2, §4.4, §5.4): a fixed ``<style>`` block from
  the preset is prepended verbatim to every beat prompt (the docs' own
  consistent-results pattern), and the first successfully rendered image of a
  visuals directory becomes the **anchor**: every later beat re-attaches it as
  a reference image with an instruction to match its style/palette/lighting
  ("provided-image edits match the original image's style, lighting, and
  perspective"). The anchor is re-read from disk each time, so a regenerated
  beat 1 re-anchors later beats, and per-asset retries stay stateless re-POSTs.
- **No seed** (spike §4.1): no ``seed`` parameter is documented anywhere on
  the current image path — the adapter must not send one and must not pretend
  the ``ImageRequest.seed`` field does anything here.
- **Model id from config** (BUILD_PLAN §0 line 26): from ``prices.json``
  (spike-verified ``gemini-nano-banana-2.1``); never the deprecated
  ``gemini-2.5-flash-image``. An explicit ``model=`` override wins.
- **Errors** propagate as ``GeminiError`` (a ``ProviderError`` — jobs.py's
  bounded retry and §6.5 messages work unchanged); guards fail **before any
  traffic**; a completed response with no image block is permanent.

Fixtures are the recorded response shapes from the research spike (§1.1
envelope via research/gemini-api.md, §2.1 image blocks) — no live API calls
exist (no image-capable key; the spike is zero-spend), so the contract is
pinned against the spike's documented shapes, offline.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.cost import load_prices
from app.core.keys import KeyStore
from app.engines.base import ImageRequest, ImageResult
from app.providers.gemini.client import GeminiClient, GeminiError
from app.providers.gemini.image import GeminiImageProvider, aspect_ratio_for, supported_ratios

KEY = "AIza" + "0123456789abcdef" * 3  # realistic shape, never a real key
INTERACTIONS_PATH = "/v1beta/interactions"
OUT_PNG = Path("beat_01.png")  # rewritten into tmp_path by the autouse fixture below


@pytest.fixture(autouse=True)
def out_path_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The provider really writes the PNG — keep the default out_path inside
    this test's tmp_path (absolute, per-test, self-cleaning)."""
    monkeypatch.setattr(sys.modules[__name__], "OUT_PNG", tmp_path / "visuals" / "beat_01.png")


# --- fixtures: recorded response shapes (research/gemini-image.md §2.1) -------------

#: Recognizable image bytes (PNG magic + payload). The adapter transports
#: bytes; it does not parse them — assembly (3.2) owns pixel dimensions.
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"image-payload-for-tests"


def png_b64(payload: bytes = PNG_BYTES) -> str:
    return base64.b64encode(payload).decode("ascii")


def interaction_response(
    image_b64: str,
    *,
    model: str = "gemini-nano-banana-2.1",
    status: str = "completed",
    steps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The unary interaction envelope (research/gemini-api.md §1.1): the image
    lives in ``steps[]`` blocks of ``type: "model_output"`` as ``type: "image"``
    content with base64 ``data`` (spike §3) — the SDK-only ``output_image``
    convenience field is absent on the REST surface."""
    if steps is None:
        steps = [
            {
                "type": "model_output",
                "content": [{"type": "image", "mime_type": "image/png", "data": image_b64}],
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


def make_provider(
    handler: Any,
    *,
    key_source: Any = KEY,
    **kwargs: Any,
) -> tuple[list[httpx.Request], GeminiImageProvider]:
    requests: list[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    client = GeminiClient(key_source, transport=httpx.MockTransport(recording_handler))
    provider = GeminiImageProvider(client, **kwargs)
    return requests, provider


def make_request(**overrides: Any) -> ImageRequest:
    """A platform-shaped request: TikTok/Reels/Shorts 9:16 target (§5)."""
    values: dict[str, Any] = {
        "prompt": "A lighthouse on a cliff at dawn",
        "width": 1080,
        "height": 1920,
        "out_path": OUT_PNG,
    }
    values.update(overrides)
    return ImageRequest(**values)


def generate_with(
    handler: Any,
    request: ImageRequest | None = None,
    **kwargs: Any,
) -> tuple[list[httpx.Request], ImageResult]:
    requests, provider = make_provider(handler, **kwargs)
    result = provider.generate(request or make_request())
    return requests, result


def request_body(request: httpx.Request) -> dict[str, Any]:
    import json

    return json.loads(request.content.decode("utf-8"))


def text_block(body: dict[str, Any]) -> dict[str, Any]:
    content = body["input"][0]["content"]
    return next(block for block in content if block.get("type") == "text")


def image_blocks(body: dict[str, Any]) -> list[dict[str, Any]]:
    content = body["input"][0]["content"]
    return [block for block in content if block.get("type") == "image"]


# --- request shape (spike §2.1, §5.2) -------------------------------------------------


def test_request_shape_matches_the_research_spike():
    requests, _ = generate_with(
        lambda request: httpx.Response(200, json=interaction_response(png_b64()))
    )
    assert len(requests) == 1  # one stateless POST per beat — no interaction chaining
    sent = requests[0]
    assert sent.method == "POST"
    assert sent.url.host == "generativelanguage.googleapis.com"
    assert sent.url.path == INTERACTIONS_PATH
    assert sent.headers["x-goog-api-key"] == KEY  # §6.5: key only in the header
    assert "key" not in sent.url.params  # never in the query string

    body = request_body(sent)
    assert body["model"] == "gemini-nano-banana-2.1"
    assert body["input"] == [
        {
            "type": "user_input",
            "content": [{"type": "text", "text": "A lighthouse on a cliff at dawn"}],
        }
    ]
    # Both dimension fields ALWAYS explicit (spike §2.1: defaults are 1:1/1K);
    # 2K covers every platform target (spike §2.3); single-entry image format
    # suppresses the conversational text (spike §2.1/§2.4).
    assert body["response_format"] == {
        "type": "image",
        "mime_type": "image/png",
        "aspect_ratio": "9:16",
        "image_size": "2K",
    }
    assert "generation_config" not in body  # thinking default, no override asked
    assert "previous_interaction_id" not in body  # stateless per spike §5.2
    assert "tools" not in body  # no grounding tools on the image path (spike §5.2)


def test_model_comes_from_the_price_table_not_a_hardcoded_id():
    """Model ids are config data (BUILD_PLAN §0 line 26) — the request must
    carry whatever prices.json says, and prices.json must be the shipped
    spike-verified image model (never the deprecated 2.5-flash-image)."""
    prices = load_prices()
    configured = prices.price_for("gemini", "image").model
    assert configured, "prices.json must carry the verified image model id"

    requests, _ = generate_with(
        lambda request: httpx.Response(200, json=interaction_response(png_b64()))
    )
    assert request_body(requests[0])["model"] == configured
    assert configured == "gemini-nano-banana-2.1"  # spike §1 (verified 2026-11-06)
    assert configured != "gemini-2.5-flash-image"  # shuts down 2027-03-15 (spike §1)


def test_explicit_model_override_wins_over_the_price_table():
    requests, _ = generate_with(
        lambda request: httpx.Response(200, json=interaction_response(png_b64())),
        model="gemini-3-pro-image",
    )
    assert request_body(requests[0])["model"] == "gemini-3-pro-image"


def test_image_size_defaults_to_2k_and_an_override_is_sent_verbatim():
    requests, _ = generate_with(
        lambda request: httpx.Response(200, json=interaction_response(png_b64())),
        image_size="4K",
    )
    assert request_body(requests[0])["response_format"]["image_size"] == "4K"


def test_lowercase_size_tier_is_rejected_at_wiring_time():
    """Spike §2.1: 'You must use an uppercase K … lowercase parameters will be
    rejected' — the adapter refuses them when wired, never mid-job via a 400."""
    with pytest.raises(ValueError, match="2K"):
        make_provider(
            lambda request: httpx.Response(200, json=interaction_response(png_b64())),
            image_size="2k",
        )


def test_unknown_size_tier_is_rejected_at_wiring_time():
    with pytest.raises(ValueError, match="image_size"):
        make_provider(
            lambda request: httpx.Response(200, json=interaction_response(png_b64())),
            image_size="3K",
        )


# --- platform-correct dimensions (spike §2.2, §2.3, §5.3) -----------------------------


@pytest.mark.parametrize(
    ("width", "height", "ratio"),
    [
        # Every platforms.json resolution that reduces onto the API table:
        (1920, 1080, "16:9"),  # youtube/x/facebook/linkedin video frames
        (1080, 1920, "9:16"),  # shorts/tiktok/reels/stories
        (1080, 1080, "1:1"),  # instagram feed square
        (1080, 1350, "4:5"),  # instagram portrait/carousel
        (1200, 675, "16:9"),  # x images
        (1200, 1200, "1:1"),  # linkedin carousel
        (1000, 1500, "2:3"),  # pinterest pin
        # The rest of the documented 14-ratio table (spike §2.2):
        (1350, 1080, "5:4"),
        (1350, 1800, "3:4"),
        (1800, 1350, "4:3"),
        (1350, 2025, "2:3"),
        (2025, 1350, "3:2"),
        (2520, 1080, "21:9"),
        (270, 1080, "1:4"),
        (1080, 270, "4:1"),
        (135, 1080, "1:8"),
        (1080, 135, "8:1"),
    ],
)
def test_platform_resolutions_map_exactly_onto_the_api_ratio_table(
    width: int, height: int, ratio: str
):
    assert aspect_ratio_for(width, height, model="gemini-nano-banana-2.1") == ratio


@pytest.mark.parametrize(
    ("width", "height"),
    [(1200, 630), (1200, 627)],  # facebook_image 40:21, linkedin_image 400:209
)
def test_off_table_resolution_takes_the_nearest_supported_ratio(width: int, height: int):
    """Two shipped platform targets don't reduce onto the table — the API has
    no free-form pixel sizes (spike §2.1), so the nearest supported ratio wins
    and assembly (3.2) crops to the platform resolution."""
    assert aspect_ratio_for(width, height, model="gemini-nano-banana-2.1") == "16:9"


def test_pro_model_restricted_table_excludes_the_extreme_ratios():
    """3 Pro Image supports only the 10 standard ratios — no 1:4/4:1/1:8/8:1
    (spike §2.2). A 1:8 request on Pro falls back to the nearest standard."""
    assert "1:8" not in supported_ratios("gemini-3-pro-image")
    assert "1:8" in supported_ratios("gemini-nano-banana-2.1")
    assert aspect_ratio_for(135, 1080, model="gemini-3-pro-image") == "9:16"


def test_mapped_ratio_reaches_the_wire():
    requests, _ = generate_with(
        lambda request: httpx.Response(200, json=interaction_response(png_b64())),
        request=make_request(width=1000, height=1500),  # pinterest pin 2:3
    )
    assert request_body(requests[0])["response_format"]["aspect_ratio"] == "2:3"


@pytest.mark.parametrize(("width", "height"), [(0, 1080), (1080, 0), (-1, 1920), (1080, -5)])
def test_non_positive_dimensions_fail_before_any_traffic(width: int, height: int):
    requests, provider = make_provider(
        lambda request: httpx.Response(200, json=interaction_response(png_b64()))
    )
    with pytest.raises(GeminiError, match="dimensions"):
        provider.generate(make_request(width=width, height=height))
    assert requests == []


def test_jpeg_out_path_maps_to_the_jpeg_mime_type(tmp_path: Path):
    requests, _ = generate_with(
        lambda request: httpx.Response(200, json=interaction_response(png_b64())),
        request=make_request(out_path=tmp_path / "visuals" / "beat_01.jpg"),
    )
    assert request_body(requests[0])["response_format"]["mime_type"] == "image/jpeg"


def test_unsupported_out_path_suffix_fails_before_any_traffic(tmp_path: Path):
    requests, provider = make_provider(
        lambda request: httpx.Response(200, json=interaction_response(png_b64()))
    )
    with pytest.raises(GeminiError, match=r"\.gif"):
        provider.generate(make_request(out_path=tmp_path / "visuals" / "beat_01.gif"))
    assert requests == []


# --- style block + seed honesty (spike §4.1, §4.4) ------------------------------------


def test_style_hint_is_prepended_as_a_fixed_style_block():
    """Spike §4.4: a fixed, hyper-specific style preamble at the head of every
    prompt is the docs' own consistent-results pattern; the beat prompt stays
    intact after it."""
    style = "Warm hand-painted gouache illustrations, muted dusk palette, soft grain."
    requests, _ = generate_with(
        lambda request: httpx.Response(200, json=interaction_response(png_b64())),
        request=make_request(style_hint=style),
    )
    text = text_block(request_body(requests[0]))["text"]
    assert text == (f"<style>\n{style}\n</style>\n\nA lighthouse on a cliff at dawn")


def test_blank_style_hint_sends_the_bare_prompt():
    requests, _ = generate_with(
        lambda request: httpx.Response(200, json=interaction_response(png_b64())),
        request=make_request(style_hint="   "),
    )
    assert text_block(request_body(requests[0]))["text"] == "A lighthouse on a cliff at dawn"


def test_seed_is_never_sent_the_api_documents_none():
    """Spike §4.1: no seed parameter exists on the current image path — sending
    ImageRequest.seed would be an undocumented extra, not a contract. The
    adapter must not put one on the wire (and must not pretend it does)."""
    requests, _ = generate_with(
        lambda request: httpx.Response(200, json=interaction_response(png_b64())),
        request=make_request(seed=42),
    )
    assert "seed" not in request_body(requests[0])
    assert "seed" not in sent_json(requests[0])


def sent_json(request: httpx.Request) -> str:
    return request.content.decode("utf-8")


# --- style anchor: first render anchors the directory (spike §4.2, §5.4) --------------


def test_first_render_in_a_directory_carries_no_reference_image():
    requests, _ = generate_with(
        lambda request: httpx.Response(200, json=interaction_response(png_b64()))
    )
    assert image_blocks(request_body(requests[0])) == []


def test_later_renders_anchor_to_the_first_image_of_their_directory(tmp_path: Path):
    requests: list[httpx.Request] = []
    provider = make_provider(
        lambda request: (
            requests.append(request),
            httpx.Response(200, json=interaction_response(png_b64())),
        )[1]
    )[1]
    visuals = tmp_path / "visuals"
    provider.generate(make_request(out_path=visuals / "beat_01.png"))
    provider.generate(make_request(out_path=visuals / "beat_02.png"))

    assert len(requests) == 2
    first, second = (request_body(r) for r in requests)
    assert image_blocks(first) == []  # beat 1 is the anchor — it references nothing
    refs = image_blocks(second)
    assert len(refs) == 1
    assert refs[0]["mime_type"] == "image/png"
    assert base64.b64decode(refs[0]["data"]) == (visuals / "beat_01.png").read_bytes()

    text = text_block(second)["text"]
    assert "reference image" in text.lower()  # the match-its-style instruction
    assert "style" in text.lower()
    assert "A lighthouse on a cliff at dawn" in text  # the new beat content survives


def test_later_renders_also_carry_the_fixed_style_block(tmp_path: Path):
    style = "Warm hand-painted gouache illustrations, muted dusk palette, soft grain."
    requests, provider = make_provider(
        lambda request: httpx.Response(200, json=interaction_response(png_b64()))
    )
    visuals = tmp_path / "visuals"
    provider.generate(make_request(out_path=visuals / "beat_01.png", style_hint=style))
    provider.generate(make_request(out_path=visuals / "beat_02.png", style_hint=style))
    text = text_block(request_body(requests[1]))["text"]
    assert text.startswith(f"<style>\n{style}\n</style>\n\n")


def test_regenerated_anchor_is_reread_from_disk_not_cached_bytes(tmp_path: Path):
    """The anchor is a path, re-read per call: after beat 1 is regenerated, the
    next beat must reference the NEW bytes — never a stale copy (§6.6 per-asset
    regen must be able to re-anchor the set)."""
    requests, provider = make_provider(
        lambda request: httpx.Response(200, json=interaction_response(png_b64()))
    )
    visuals = tmp_path / "visuals"
    beat_01 = visuals / "beat_01.png"
    provider.generate(make_request(out_path=beat_01))
    beat_01.write_bytes(PNG_BYTES + b"-regenerated")  # per-asset regen of beat 1
    provider.generate(make_request(out_path=visuals / "beat_02.png"))

    refs = image_blocks(request_body(requests[1]))
    assert base64.b64decode(refs[0]["data"]) == PNG_BYTES + b"-regenerated"


def test_regenerating_the_anchor_does_not_reference_itself(tmp_path: Path):
    """Beat 1 re-rendered later is a fresh render — attaching its own previous
    file as its reference would condition the new anchor on the old one."""
    requests, provider = make_provider(
        lambda request: httpx.Response(200, json=interaction_response(png_b64()))
    )
    visuals = tmp_path / "visuals"
    beat_01 = visuals / "beat_01.png"
    provider.generate(make_request(out_path=beat_01))
    provider.generate(make_request(out_path=visuals / "beat_02.png"))
    provider.generate(make_request(out_path=beat_01))  # per-asset regen of beat 1

    assert image_blocks(request_body(requests[2])) == []
    assert provider.anchor_for(visuals) == beat_01  # still the anchor for the set


def test_missing_anchor_file_degrades_to_style_block_only(tmp_path: Path):
    """The anchor is a consistency aid, never a dependency: a vanished file
    (user cleanup, maintenance sweep) must not fail the beat."""
    requests, provider = make_provider(
        lambda request: httpx.Response(200, json=interaction_response(png_b64()))
    )
    visuals = tmp_path / "visuals"
    beat_01 = visuals / "beat_01.png"
    provider.generate(make_request(out_path=beat_01))
    beat_01.unlink()
    provider.generate(make_request(out_path=visuals / "beat_02.png"))  # must not raise
    assert image_blocks(request_body(requests[1])) == []
    # the stale entry is dropped; the next success re-anchors the directory
    assert provider.anchor_for(visuals) == visuals / "beat_02.png"


def test_failed_render_records_no_anchor(tmp_path: Path):
    """Only a *successful* render may anchor: a beat-1 failure must not pin a
    half-written or absent file as the style reference for later beats."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return gemini_error_response("api_error", "down", status=500)
        return httpx.Response(200, json=interaction_response(png_b64()))

    requests, provider = make_provider(handler)
    visuals = tmp_path / "visuals"
    with pytest.raises(GeminiError):
        provider.generate(make_request(out_path=visuals / "beat_01.png"))
    provider.generate(make_request(out_path=visuals / "beat_02.png"))
    assert image_blocks(request_body(requests[1])) == []  # beat 2 became the anchor
    assert provider.anchor_for(visuals) == visuals / "beat_02.png"


def test_directories_anchor_independently(tmp_path: Path):
    requests, provider = make_provider(
        lambda request: httpx.Response(200, json=interaction_response(png_b64()))
    )
    provider.generate(make_request(out_path=tmp_path / "project-a" / "visuals" / "beat_01.png"))
    requests.clear()
    provider.generate(make_request(out_path=tmp_path / "project-b" / "visuals" / "beat_01.png"))
    assert image_blocks(request_body(requests[0])) == []


# --- response handling (spike §3: image in steps[].content) ---------------------------


def test_image_bytes_are_written_and_result_names_the_file(tmp_path: Path):
    out_path = tmp_path / "visuals" / "beat_01.png"
    _, result = generate_with(
        lambda request: httpx.Response(200, json=interaction_response(png_b64())),
        request=make_request(out_path=out_path),
    )
    assert isinstance(result, ImageResult)
    assert result.file == out_path
    assert out_path.read_bytes() == PNG_BYTES


def test_uri_delivered_image_is_downloaded_without_credentials():
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
                            "type": "image",
                            "mime_type": "image/png",
                            "uri": "https://files.example.com/delivery/beat.png",
                        }
                    ],
                }
            ]
            return httpx.Response(200, json=interaction_response("", steps=steps))
        return httpx.Response(200, content=PNG_BYTES)

    out_path = OUT_PNG  # absolute via the autouse fixture
    _, result = generate_with(handler, request=make_request(out_path=out_path))
    download = seen[-1]
    assert download.url.host == "files.example.com"
    assert "x-goog-api-key" not in download.headers
    assert Path(result.file).read_bytes() == PNG_BYTES


def test_completed_response_without_image_names_what_is_missing(tmp_path: Path):
    steps = [{"type": "model_output", "content": [{"type": "text", "text": "oops"}]}]
    requests, provider = make_provider(
        lambda request: httpx.Response(200, json=interaction_response("", steps=steps))
    )
    out_path = tmp_path / "visuals" / "beat_01.png"
    with pytest.raises(GeminiError) as exc_info:
        provider.generate(make_request(out_path=out_path))
    error = exc_info.value
    assert error.retryable is False  # retrying the same request won't add an image
    assert "image" in error.message.lower()
    assert "steps" in error.message.lower()  # names where the adapter looked
    assert not out_path.exists()  # nothing was written
    assert provider.anchor_for(out_path.parent) is None  # and nothing anchored


def test_multiple_image_blocks_fail_loud_instead_of_guessing(tmp_path: Path):
    """A single-entry image response_format requests ONE image (spike §2.1);
    several arriving blocks cannot be spliced (images aren't chunks) and
    picking one silently would ship an arbitrary image — fail loud, mirror of
    the TTS multi-RIFF refusal (review m2-f5)."""
    steps = [
        {
            "type": "model_output",
            "content": [
                {"type": "image", "mime_type": "image/png", "data": png_b64(b"one")},
                {"type": "image", "mime_type": "image/png", "data": png_b64(b"two")},
            ],
        }
    ]
    requests, provider = make_provider(
        lambda request: httpx.Response(200, json=interaction_response("", steps=steps))
    )
    out_path = tmp_path / "visuals" / "beat_01.png"
    with pytest.raises(GeminiError, match="2 image blocks"):
        provider.generate(make_request(out_path=out_path))
    assert not out_path.exists()


def test_non_completed_status_is_an_error(tmp_path: Path):
    requests, provider = make_provider(
        lambda request: httpx.Response(200, json=interaction_response(png_b64(), status="failed"))
    )
    with pytest.raises(GeminiError) as exc_info:
        provider.generate(make_request(out_path=tmp_path / "visuals" / "beat_01.png"))
    error = exc_info.value
    assert error.retryable is False
    assert "failed" in error.message.lower()


def test_undecodable_image_block_is_a_permanent_error(tmp_path: Path):
    steps = [
        {
            "type": "model_output",
            "content": [{"type": "image", "mime_type": "image/png", "data": "!!not-b64!!"}],
        }
    ]
    requests, provider = make_provider(
        lambda request: httpx.Response(200, json=interaction_response("", steps=steps))
    )
    out_path = tmp_path / "visuals" / "beat_01.png"
    with pytest.raises(GeminiError, match="base64"):
        provider.generate(make_request(out_path=out_path))
    assert not out_path.exists()


# --- guards and errors -----------------------------------------------------------------


def test_missing_key_fails_before_any_traffic(tmp_path: Path):
    store = KeyStore(tmp_path / "keys.json", provider_engines={"gemini": ("image",)})
    requests, provider = make_provider(
        lambda request: httpx.Response(200, json=interaction_response(png_b64())),
        key_source=store,
    )
    with pytest.raises(GeminiError) as exc_info:
        provider.generate(make_request())
    assert requests == []  # zero bytes left the machine
    error = exc_info.value
    assert error.retryable is False
    assert "no gemini api key" in error.message.lower()
    assert "settings" in error.message.lower()


def test_blank_prompt_fails_before_any_traffic():
    requests, provider = make_provider(
        lambda request: httpx.Response(200, json=interaction_response(png_b64()))
    )
    with pytest.raises(GeminiError, match="prompt"):
        provider.generate(make_request(prompt="   "))
    assert requests == []


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


# --- thinking level option (spike §3: sanctioned cost/latency lever) -------------------


def test_thinking_level_option_lands_in_generation_config():
    requests, _ = generate_with(
        lambda request: httpx.Response(200, json=interaction_response(png_b64())),
        thinking_level="minimal",
    )
    assert request_body(requests[0])["generation_config"] == {"thinking_level": "minimal"}


def test_thinking_level_defaults_to_the_api_default_no_key_at_all():
    requests, _ = generate_with(
        lambda request: httpx.Response(200, json=interaction_response(png_b64()))
    )
    assert "generation_config" not in request_body(requests[0])


def test_unknown_thinking_level_is_rejected_before_any_traffic():
    with pytest.raises(ValueError, match="thinking_level"):
        make_provider(
            lambda request: httpx.Response(200, json=interaction_response(png_b64())),
            thinking_level="ludicrous",
        )


# --- cost estimate -----------------------------------------------------------------------


def test_estimate_cost_is_one_image_from_the_price_table():
    """One call = one image at the prices.json unit price (§6.6 confirmation
    flow shows the same number)."""
    requests, provider = make_provider(lambda request: httpx.Response(200, json={}))
    price = load_prices().price_for("gemini", "image").price
    assert provider.estimate_cost_usd(make_request()) == pytest.approx(price)


# --- registration (real image wiring; mirrors task 2.3's TTS registration) ---------------


def test_default_registry_registers_the_gemini_image_provider_with_a_key_store(tmp_path: Path):
    from app.main import default_registry

    store = KeyStore(tmp_path / "keys.json", provider_engines={"gemini": ("image",)})
    registry = default_registry(key_store=store)
    provider = registry.get("image", "gemini")
    assert isinstance(provider, GeminiImageProvider)
    assert provider.provider_id == "gemini"
    # the key is read at call time from the store — never baked in
    assert provider.client.api_key() is None


def test_default_registry_without_a_key_store_stays_mock_only_for_images():
    """Dry-run gate (§1.2): without a key store there is no Gemini adapter to
    reach the network with."""
    from app.main import default_registry

    registry = default_registry()
    assert [p.provider_id for p in registry.providers_for("image")] == ["mock"]
