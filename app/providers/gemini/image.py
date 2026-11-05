"""Gemini image provider with style anchoring (BUILD_PLAN 3.1).

One :class:`GeminiImageProvider` per registry; it rides on the 2.2
:class:`~app.providers.gemini.client.GeminiClient` (auth, timeouts, error
mapping live there and nowhere else) and implements the ``ImageProvider``
protocol from ``app.engines.base``, so BYOK images are interchangeable with
the zero-network mock. The contract is the task 3.0 research spike
(``research/gemini-image.md``); visual consistency is a hard requirement
(BUILD_PLAN §2 / spec §6.3), so every render carries the full anchoring
package the API actually supports:

- **One stateless POST per beat** (spike §5.2): ``POST /v1beta/interactions``
  with ``input = [text, *reference images]`` and a single-entry image
  ``response_format`` (suppresses the conversational text, spike §2.1/§2.4).
  No ``previous_interaction_id`` chaining, no grounding tools — per-asset
  retry/regen (§6.6) is an idempotent re-POST, and a regenerated beat
  re-downloads only that beat.
- **Platform-correct dimensions** (spike §2.1–§2.3, §5.3): pixel sizes are
  not free-form; the request picks a supported aspect ratio + a size tier.
  :func:`aspect_ratio_for` maps the platform resolution onto the per-model
  ratio table by gcd reduction (:func:`supported_ratios`: 14 ratios on Nano
  Banana 2.1, the 10 standard ones on 3 Pro Image); an off-table resolution
  (e.g. 1200×630 → 40:21) takes the nearest supported ratio and assembly
  (3.2) scales with aspect preserved and pads onto the platform-resolution
  canvas (images are padded, never cropped). ``image_size`` defaults to
  ``"2K"`` — 1K is undersized on every ``platforms.json`` target (spike
  §2.3) — and both dimension fields are ALWAYS sent explicitly: the API's
  silent defaults (match-input size / 1:1 square, 1K) are wrong for every
  format we ship. Uppercase ``K`` is mandatory (lowercase rejected by the
  API), so the adapter rejects lowercase tiers up front.
- **Style anchoring** (spike §4.2, §4.4, §5.4) — two documented mechanisms,
  no invented ones:
  1. a **fixed style block**: ``ImageRequest.style_hint`` (the user's preset
     style) is prepended to every beat prompt verbatim as a ``<style>`` block
     — the docs' "be hyper-specific … for consistent results" pattern;
  2. a **reference anchor**: the first *successfully rendered* image of a
     visuals directory becomes that directory's anchor; every later beat
     re-attaches its bytes as a reference image plus an explicit
     match-the-style instruction (docs: provided-image edits match the
     original image's "style, lighting, and perspective"). The anchor is
     stored as a *path* and re-read per call, so a regenerated beat 1
     re-anchors later beats with the new bytes; a vanished anchor degrades to
     style-block-only instead of failing the beat. Anchoring never fires on
     the anchor's own regeneration (no self-reference).
- **No seed** (spike §4.1): no ``seed`` parameter is documented anywhere on
  the current image path and third-party reports conflict — so
  ``ImageRequest.seed`` is deliberately NOT sent (the mock uses it only for
  its own determinism). Consistency comes from the style block + anchor,
  never from a knob that would silently do nothing.
- **Model id from config** (BUILD_PLAN §0 line 26): the model comes from
  ``app/data/prices.json`` (spike-verified ``gemini-nano-banana-2.1`` —
  never the deprecated ``gemini-2.5-flash-image``); an explicit ``model=``
  override wins.
- **Errors** are the client's ``GeminiError`` (a ``ProviderError``): jobs.py's
  bounded retry sees ``retryable``, the UI sees exactly what's wrong. Guards
  (blank prompt, bad dimensions/tier/suffix) fail *before any traffic*; a
  completed response with no image block, several image blocks, or an
  undecodable payload is permanent — nothing is guessed or spliced (the
  multi-block refusal mirrors TTS's multi-RIFF refusal, review m2-f5).

All HTTP runs through the client (``httpx.MockTransport`` in tests) — fully
offline contract tests in ``tests/test_gemini_image.py``.
"""

from __future__ import annotations

import base64
import threading
from collections import OrderedDict
from math import gcd, log
from pathlib import Path
from typing import Any

from app.core.cost import estimate_image_cost, load_prices
from app.engines.base import ImageRequest, ImageResult
from app.providers.gemini.client import GeminiClient, GeminiError

PROVIDER_ID = "gemini"
INTERACTIONS_PATH = "/v1beta/interactions"

#: Size tiers accepted by the image models (spike §2.1). Uppercase K is
#: mandatory — the API rejects e.g. "2k" — and "2K" is the default because
#: 1K is undersized on every platforms.json target (spike §2.3); assembly
#: downscales 2K → platform dims. prices.json carries the 2K per-image price.
DEFAULT_IMAGE_SIZE = "2K"
IMAGE_SIZES = ("1K", "2K", "4K")

#: Thinking levels documented for the image models (spike §3). ``None`` (the
#: default) omits the key entirely and leaves the API default in charge;
#: "minimal" is the sanctioned cost/latency lever for multi-beat jobs.
THINKING_LEVELS = ("minimal", "medium", "high")

#: The full 14-ratio table for Nano Banana 2.1 / 3.1 Flash (spike §2.2), and
#: the 10 standard ratios 3 Pro Image supports (no 1:4/4:1/1:8/8:1).
NANO_BANANA_RATIOS = frozenset(
    {
        "1:1",
        "9:16",
        "16:9",
        "4:5",
        "5:4",
        "3:4",
        "4:3",
        "2:3",
        "3:2",
        "21:9",
        "1:4",
        "4:1",
        "1:8",
        "8:1",
    }
)
PRO_IMAGE_RATIOS = frozenset(
    {"1:1", "9:16", "16:9", "4:5", "5:4", "3:4", "4:3", "2:3", "3:2", "21:9"}
)

#: Requestable output mime types (spike §2.1) by out_path suffix — the
#: platform formats are png/jpg (platforms.json ``container``).
_MIME_BY_SUFFIX = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}

#: Appended to the beat prompt whenever the anchor image is attached (spike
#: §4.2: provided-image edits match the original image's "style, lighting,
#: and perspective").
ANCHOR_INSTRUCTION = (
    "\n\nUse the attached reference image as the style anchor: match its"
    " style, color palette, lighting, and perspective exactly — keep every"
    " stylistic element identical while depicting the new subject."
)

#: Directories whose anchors are remembered (one anchor per visuals dir).
_MAX_ANCHORED_DIRS = 16


def supported_ratios(model: str) -> frozenset[str]:
    """The aspect ratios the model documents (spike §2.2)."""
    return PRO_IMAGE_RATIOS if "pro-image" in model else NANO_BANANA_RATIOS


def aspect_ratio_for(width: int, height: int, *, model: str) -> str:
    """Map a platform resolution onto a requestable aspect ratio (spike §5.3).

    Exact gcd reduction first (1920×1080 → "16:9"); a resolution off the
    table (1200×630 → 40:21) takes the nearest supported ratio by log-ratio
    distance — the API has no free-form pixel sizes, and assembly scales with
    aspect preserved and pads to the platform resolution (task 3.2; images
    are padded, never cropped — see app/assembly/images.py).
    """
    if width <= 0 or height <= 0:
        raise GeminiError(
            f"Image dimensions must be positive, got {width}x{height} —"
            " check the platform resolution data (app/data/platforms.json).",
            retryable=False,
        )
    ratios = supported_ratios(model)
    divisor = gcd(width, height)
    exact = f"{width // divisor}:{height // divisor}"
    if exact in ratios:
        return exact
    target = log(width / height)

    def distance(ratio: str) -> float:
        r_w, r_h = (int(part) for part in ratio.split(":"))
        return abs(log(r_w / r_h) - target)

    return min(sorted(ratios), key=distance)


class GeminiImageProvider:
    """Per-beat image generation with style anchoring via the Interactions API.

    ``client`` is a :class:`GeminiClient` bound to a ``KeyStore`` (key read at
    call time — adding/replacing the key in Settings applies to the next
    beat) or a literal key string for probes/tests.
    """

    provider_id = PROVIDER_ID

    def __init__(
        self,
        client: GeminiClient,
        *,
        model: str | None = None,
        image_size: str = DEFAULT_IMAGE_SIZE,
        thinking_level: str | None = None,
    ) -> None:
        if not isinstance(client, GeminiClient):
            raise ValueError(
                "GeminiImageProvider needs a GeminiClient (it owns auth,"
                f" timeouts and error mapping), got {type(client).__name__}."
            )
        if image_size not in IMAGE_SIZES:
            raise ValueError(
                f"image_size must be one of {', '.join(IMAGE_SIZES)} (uppercase"
                f" K mandatory — the API rejects lowercase), got {image_size!r}."
            )
        if thinking_level is not None and thinking_level not in THINKING_LEVELS:
            raise ValueError(
                f"thinking_level must be one of {', '.join(THINKING_LEVELS)} or"
                f" None (API default), got {thinking_level!r}."
            )
        self.client = client  # public: the key stays readable at call time (§6.5)
        self._model = model if model is not None else _configured_model()
        self._image_size = image_size
        self._thinking_level = thinking_level
        self._anchors: OrderedDict[Path, Path] = OrderedDict()  # dir → anchor file
        self._anchor_lock = threading.Lock()  # the worker is single-threaded; stay honest

    def estimate_cost_usd(self, request: ImageRequest) -> float:
        """One call = one image at the prices.json unit price (§6.6)."""
        return estimate_image_cost(load_prices(), PROVIDER_ID, count=1)

    def generate(self, request: ImageRequest) -> ImageResult:
        """Render one beat; write the image; return its path."""
        mime = _require_generatable(request, image_size=self._image_size)
        interaction = self.client.post_json(INTERACTIONS_PATH, json=self._body(request, mime))
        _require_completed(interaction)
        image = self._extract_image(interaction)
        request.out_path.parent.mkdir(parents=True, exist_ok=True)
        request.out_path.write_bytes(image)
        self._remember_anchor(request.out_path)
        return ImageResult(file=request.out_path)

    def anchor_for(self, directory: Path) -> Path | None:
        """The anchor image recorded for ``directory``, if it still exists."""
        anchor = self._anchors.get(Path(directory).resolve())
        if anchor is None or not anchor.is_file():
            return None
        return anchor

    # --- request assembly --------------------------------------------------------------

    def _body(self, request: ImageRequest, mime: str) -> dict[str, Any]:
        anchor = self._reference_bytes(request)
        text = _compose_prompt(request.prompt, style_hint=request.style_hint, anchored=anchor)
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        if anchor is not None:
            content.append({"type": "image", "mime_type": "image/png", "data": _b64(anchor)})
        body: dict[str, Any] = {
            "model": self._model,
            "input": [{"type": "user_input", "content": content}],
            "response_format": {
                "type": "image",
                "mime_type": mime,
                "aspect_ratio": aspect_ratio_for(request.width, request.height, model=self._model),
                "image_size": self._image_size,
            },
        }
        if self._thinking_level is not None:
            body["generation_config"] = {"thinking_level": self._thinking_level}
        return body

    # --- anchor bookkeeping (spike §5.4b) ------------------------------------------------

    def _reference_bytes(self, request: ImageRequest) -> bytes | None:
        """The anchor image's bytes for this render, if one applies.

        ``None`` for the first render of a directory and for the anchor's own
        regeneration (no self-reference); a vanished anchor file is dropped
        and the beat degrades to style-block-only.
        """
        key = request.out_path.parent.resolve()
        with self._anchor_lock:
            anchor = self._anchors.get(key)
            if anchor is None:
                return None
            if anchor == request.out_path.resolve():
                return None
            try:
                return anchor.read_bytes()
            except OSError:
                del self._anchors[key]  # stale; the next success re-anchors
                return None

    def _remember_anchor(self, out_path: Path) -> None:
        """Record a successful render as its directory's anchor (first success
        wins; later successes in other beats do not steal the role)."""
        key = out_path.parent.resolve()
        with self._anchor_lock:
            if key in self._anchors:
                return
            self._anchors[key] = out_path.resolve()
            self._anchors.move_to_end(key)
            while len(self._anchors) > _MAX_ANCHORED_DIRS:
                self._anchors.popitem(last=False)

    # --- response handling ----------------------------------------------------------------

    def _extract_image(self, interaction: dict[str, Any]) -> bytes:
        """Walk the REST envelope (spike §3): ``steps[]`` entries of type
        ``model_output`` carry ``content[]`` image blocks with base64 ``data``;
        URI-delivered blocks are downloaded via the client, which attaches the
        key to the Gemini API host only (§6.5). One response = one image:
        none is permanent, several are refused rather than guessed."""
        images: list[bytes] = []
        for step in _as_list(interaction.get("steps")):
            if not isinstance(step, dict) or step.get("type") != "model_output":
                continue
            for block in _as_list(step.get("content")):
                if not isinstance(block, dict) or block.get("type") != "image":
                    continue
                data = block.get("data")
                if isinstance(data, str) and data.strip():
                    images.append(_decode_image(data))
                    continue
                uri = block.get("uri") or block.get("file_uri")
                if isinstance(uri, str) and uri.strip():
                    images.append(self.client.download(uri.strip()))
        if not images:
            raise GeminiError(
                "Gemini returned a completed response but it contains no image"
                " (no image block under steps[].content) — nothing was written;"
                " regenerate the asset. If this keeps happening, the model"
                " response shape may have drifted (research/gemini-image.md §3).",
                retryable=False,
            )
        if len(images) > 1:
            raise GeminiError(
                f"Gemini returned {len(images)} image blocks for a request that"
                " asked for exactly one — picking one silently would ship an"
                " arbitrary image, so nothing was written; regenerate the"
                " asset. If this keeps happening, the model response shape may"
                " have drifted (research/gemini-image.md §3).",
                retryable=False,
            )
        return images[0]


# --- request/response helpers -------------------------------------------------------


def _configured_model() -> str:
    """The spike-verified image model id from the editable price table."""
    model = load_prices().price_for(PROVIDER_ID, "image").model
    if not model:
        raise ValueError(
            "app/data/prices.json has no 'model' for the gemini image engine —"
            " the Gemini image provider refuses to guess a model id"
            " (see research/gemini-image.md §1)."
        )
    return model


def _require_generatable(request: ImageRequest, *, image_size: str) -> str:
    """Fail before any traffic when the beat cannot be generated at all;
    return the response mime type for the out_path."""
    if not request.prompt.strip():
        raise GeminiError(
            "The image beat has no prompt — add an image hint to the beat in"
            " the script review step, then regenerate.",
            retryable=False,
        )
    if request.width <= 0 or request.height <= 0:
        raise GeminiError(
            f"Image dimensions must be positive, got"
            f" {request.width}x{request.height} — check the platform"
            " resolution in the options step, then regenerate.",
            retryable=False,
        )
    mime = _MIME_BY_SUFFIX.get(request.out_path.suffix.lower())
    if mime is None:
        supported = ", ".join(sorted(_MIME_BY_SUFFIX))
        raise GeminiError(
            f"The image output path ends in {request.out_path.suffix!r} but the"
            f" Gemini image API only produces {supported} — check the platform"
            " format in the options step, then regenerate.",
            retryable=False,
        )
    return mime


def _compose_prompt(prompt: str, *, style_hint: str | None, anchored: bytes | None) -> str:
    """Beat prompt with the fixed style block at the head (spike §4.4) and,
    when the anchor image is attached, the match-the-style instruction (§4.2)."""
    text = prompt
    if style_hint and style_hint.strip():
        text = f"<style>\n{style_hint}\n</style>\n\n{text}"
    if anchored is not None:
        text += ANCHOR_INSTRUCTION
    return text


def _require_completed(interaction: dict[str, Any]) -> None:
    """A unary 200 whose interaction did not complete carries no image."""
    status = interaction.get("status")
    if isinstance(status, str) and status and status != "completed":
        raise GeminiError(
            f"The Gemini image interaction ended with status {status!r} instead"
            " of 'completed' — no image was produced; regenerate the asset.",
            retryable=False,
        )


def _b64(payload: bytes) -> str:
    return base64.b64encode(payload).decode("ascii")


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _decode_image(data: str) -> bytes:
    try:
        return base64.b64decode(data, validate=True)
    except (ValueError, TypeError) as exc:
        raise GeminiError(
            "Gemini returned an image block whose base64 payload could not be"
            f" decoded ({exc}) — nothing was written; regenerate the asset.",
            retryable=False,
        ) from exc
