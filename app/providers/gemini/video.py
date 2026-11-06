"""Gemini video provider — submit → poll → download (BUILD_PLAN 4.1).

One :class:`GeminiVideoProvider` per registry; it rides on the 2.2
:class:`~app.providers.gemini.client.GeminiClient` (auth, timeouts, error
mapping live there and nowhere else) and implements the ``VideoProvider``
protocol from ``app.engines.base``, so BYOK video is interchangeable with the
zero-network mock. The contract is the task 4.0 research spike
(``research/gemini-video.md``); the current model is ``gemini-omni-1.2-flash``
(Veo 3.1 Preview was shut down 2026-10-22 — spike §1).

- **Submit** (spike §2, §7.1): one **stateless** ``POST /v1beta/interactions``
  per beat — no ``previous_interaction_id`` chaining, so per-asset retry
  (§6.6) stays an idempotent re-POST exactly like the 2.3/3.1 providers.
  ``response_format`` is ALWAYS fully explicit —
  ``{"type": "video", "aspect_ratio": <mapped>, "resolution": "720p",
  "delivery": "uri"}`` — because the docs mark URI delivery as the way to
  avoid payload limits for videos larger than 4 MB (every clip we request is)
  and the silent defaults (1:1 ratio, 720p, inline) are wrong for every
  platform format we ship. No ``task`` (docs: prompt first — ``task`` adds
  constraints), no ``store``/``stream`` overrides on the unary path.
- **Poll, two documented levels** (spike §3):
  1. the *interaction*: the unary POST blocks until ``status: "completed"``;
     a response that arrives non-terminal (the documented ``background: true``
     mode, or an early return) is continued by polling
     ``GET /v1beta/interactions/{id}`` (docs' 5 s cadence) until a terminal
     state. ``background=True`` (constructor flag) sends
     ``{"background": true, "store": true}`` — the docs' async alternative
     with its mandatory ``store: true``; the spike discloses that the
     background-execution support list names text models only while the Omni
     guide implies the parameter is accepted, so this path is the documented
     fallback pending the real-key probe. Terminal ``failed``/``cancelled``
     and ``requires_action`` (this app cannot answer tool calls) are
     permanent; unknown states keep polling until the budget runs out
     (retryable — burning bounded retries beats failing a paid asset).
  2. the *generated file* (spike §3.2): the URI from the **creation response**
     points at ``/v1beta/files/{id}:download`` whose file is not immediately
     ready — ``GET /v1beta/files/{id}`` is polled (docs' own 5 s cadence,
     bounded budget, default ~15 min until per-clip latency is measured with
     a real key) until ``state == "ACTIVE"``. ``FAILED`` is permanent; other
     states keep polling. Budget exhaustion is retryable: the file may still
     become ACTIVE server-side, and the message names the file id.
- **The creation-response gotcha, pinned** (spike §3.2): refetching
  ``GET /v1beta/interactions/{id}`` returns the video as *inline base64* even
  for ``delivery: "uri"`` — the URI is only guaranteed in the creation
  response, so it is captured there and carried to download; the unary flow
  never refetches the interaction.
- **Download** (spike §4): ``GET /v1beta/files/{id}:download?alt=media`` via
  the client — the URI is on the Gemini host, so the key header attaches
  there and only there (§6.5). Files expire after 48 h; the download happens
  in the same call, never deferred.
- **Real durations** (BUILD_PLAN §1.2 "Durations"; task 3.3 precedent):
  nothing in the response about timing is trusted — after the bytes land on
  disk the file is ffprobed and the **probed** value is what
  ``VideoResult.duration_s`` carries (the manifest stays the single source of
  truth). A failed probe means the "video" is not a playable file: permanent
  error naming the file.
- **Clip bounds enforced before spend** (spike §6 [E]): 1.2 renders 3–15 s
  clips; beats outside that window are rejected *before any traffic* with the
  remedy named (split / merge the beat) — never after paying for a clip that
  assembly's exact per-segment alignment check (3.3) would refuse anyway.
- **Aspect ratio** (spike §2 [A]): the API supports ``16:9`` and ``9:16``
  only — :func:`aspect_ratio_for` gcd-reduces the platform resolution and
  takes the nearest supported ratio otherwise (deterministic tie-break;
  assembly normalizes the result to platform dims). Every ``kind:"video"``
  format in ``app/data/platforms.json`` is exactly one of the two.
- **Resolution = the cost lever** (spike §5/§7.5): the provider always
  requests 720p ($0.0695/s ≈ $0.07) and assembly upscales to the platform
  resolution — the docs mark 1080p/4k "(upscaled)", i.e. the API itself
  renders 720p and upscales, so local upscaling is the 33 %-cheaper default.
  prices.json prices exactly what is requested; :data:`DEFAULT_RESOLUTION` is
  pinned by the tests so the two can never drift apart.
- **Per-second cost model** (§6.6): the estimate is
  :func:`app.core.cost.estimate_video_cost` against the editable price table
  (fractional seconds ceiled up) — never a hardcoded number. jobs.py calls
  this both for the confirmation-flow estimate and the recorded spend.
- **Prompts** (spike §7.3): the beat prompt rides at the head **verbatim**;
  the fixed single-scene instruction ("no scene cuts" — the model otherwise
  defaults to multi-shot edits) and the no-dialogue/neutral-ambient audio
  description (assembly discards the generated track in favor of per-beat
  TTS; instructing neutral ambient avoids lip-flap visuals) are appended.
  Clip length rides the docs' natural-language timing surface — no duration
  field exists in the documented request — and the manifest stays
  authoritative from probed values regardless of what comes back. Negatives
  go in the prompt text (no ``negative_prompt`` parameter exists).
- **Errors** are the client's ``GeminiError`` (a ``ProviderError``): jobs.py's
  bounded retry sees ``retryable``, the UI sees exactly what's wrong. Guards
  (blank prompt, clip bounds, dimensions, suffix, unparseable URI, response
  shapes) fail before traffic or permanently — nothing is guessed, spliced,
  or picked arbitrarily.

All HTTP runs through the client (``httpx.MockTransport`` in tests) — fully
offline contract tests in ``tests/test_gemini_video.py``.
"""

from __future__ import annotations

import base64
import math
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.assembly.ffmpeg import FFmpegError
from app.assembly.ffmpeg import probe_duration as _default_probe
from app.core.cost import estimate_video_cost, load_prices
from app.engines.base import VideoRequest, VideoResult
from app.providers.gemini.client import GeminiClient, GeminiError

PROVIDER_ID = "gemini"
INTERACTIONS_PATH = "/v1beta/interactions"

#: The resolution the provider always requests (spike §7.5): the docs render
#: native 720p and merely *upscale* for 1080p/4k, so generating 720p and
#: letting assembly (3.3) scale to the platform resolution is the 33 %-cheaper
#: default. prices.json's video row prices exactly this resolution — the
#: ``test_price_table_carries_the_resolution_the_provider_requests`` pin ties
#: the two together; changing one means changing both.
DEFAULT_RESOLUTION = "720p"

#: Aspect ratios documented for video output (spike §2 [A]) — no others.
VIDEO_RATIOS = ("16:9", "9:16")

#: Documented clip bounds for gemini-omni-1.2-flash (spike §6 [E]):
#: "clips 3–15 s". Beats outside the window fail loud before spend.
MIN_CLIP_S = 3.0
MAX_CLIP_S = 15.0

#: Files/interaction poll cadence (spike §3: the docs' own 5 s examples) and
#: bounded budget (spike §7.2: cap ~15 min until per-clip latency is measured
#: with a real key — spike §8 could not verify documented per-clip numbers).
DEFAULT_POLL_INTERVAL_S = 5.0
DEFAULT_MAX_POLL_S = 900.0

#: Appended to every beat prompt (spike §7.3): the single-scene instruction
#: the docs recommend for single-scene beats, plus the audio description —
#: our assembly discards the generated track in favor of per-beat TTS, and
#: instructing neutral ambient avoids lip-flap visuals. Negatives live in the
#: prompt text; the API has no ``negative_prompt`` parameter (spike §2).
PROMPT_SUFFIX = (
    "Continuous, unbroken shot of a single scene — no scene cuts."
    " No dialogue; neutral ambient sound only."
)

#: The only documented video output mime (spike §1/§2: ``video/mp4``) — and
#: the only container in platforms.json's video formats.
_MIME_BY_SUFFIX = {".mp4": "video/mp4"}

_FILE_ID_RE = re.compile(r"/files/([^:/?#]+)")


def compose_video_prompt(prompt: str, *, duration_s: float) -> str:
    """The per-beat generation prompt: beat text verbatim at the head (spike
    §7.3), then the fixed single-scene + audio clauses, then the clip length
    on the docs' natural-language timing surface (the documented request has
    no duration field — spike §2 — so the intent rides the prompt; the timing
    manifest stays authoritative from probed values either way, §1.2)."""
    seconds = max(1, round(duration_s))
    return f"{prompt}\n\n{PROMPT_SUFFIX} Clip length: about {seconds} seconds."


def aspect_ratio_for(width: int, height: int) -> str:
    """Map a platform resolution onto a requestable video ratio (spike §2).

    Exact gcd reduction first (1920×1080 → "16:9"); an off-table resolution
    (a 1:1 square or 1200×630) takes the nearest supported ratio by log-ratio
    distance — the API has no free-form pixel sizes, and assembly (3.3)
    normalizes the result onto the platform-resolution canvas. An exact tie
    (a square) resolves deterministically to ``16:9``.
    """
    if width <= 0 or height <= 0:
        raise GeminiError(
            f"Video dimensions must be positive, got {width}x{height} —"
            " check the platform resolution data (app/data/platforms.json).",
            retryable=False,
        )

    def distance(ratio: str) -> float:
        r_w, r_h = (int(part) for part in ratio.split(":"))
        return abs(math.log(r_w / r_h) - math.log(width / height))

    return min(VIDEO_RATIOS, key=distance)


class GeminiVideoProvider:
    """Per-beat video generation via the Gemini Interactions API.

    ``client`` is a :class:`GeminiClient` bound to a ``KeyStore`` (key read at
    call time — adding/replacing the key in Settings applies to the next
    beat) or a literal key string for probes/tests. ``probe`` injects the
    duration probe for offline tests (default: real
    :func:`app.assembly.ffmpeg.probe_duration`); ``sleep``/``monotonic``
    inject the poll loop's clock for the same reason. ``background=True``
    switches to the documented async request shape (§3.1). ``poll_interval_s``
    and ``max_poll_s`` bound both polling loops.
    """

    provider_id = PROVIDER_ID

    def __init__(
        self,
        client: GeminiClient,
        *,
        model: str | None = None,
        background: bool = False,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        max_poll_s: float = DEFAULT_MAX_POLL_S,
        probe: Callable[[Path | str], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if not isinstance(client, GeminiClient):
            raise ValueError(
                "GeminiVideoProvider needs a GeminiClient (it owns auth,"
                f" timeouts and error mapping), got {type(client).__name__}."
            )
        if poll_interval_s <= 0:
            raise ValueError(
                f"poll_interval_s must be positive (got {poll_interval_s!r})"
                " — a zero interval would hammer the API in a tight loop."
            )
        if max_poll_s <= 0:
            raise ValueError(
                f"max_poll_s must be positive (got {max_poll_s!r}) — the poll"
                " budget must be finite; an unbounded wait would hang the"
                " job worker."
            )
        self.client = client  # public: the key stays readable at call time (§6.5)
        self._model = model if model is not None else _configured_model()
        self._background = background
        self._poll_interval_s = poll_interval_s
        self._max_poll_s = max_poll_s
        self._probe = probe if probe is not None else _default_probe
        self._sleep = sleep if sleep is not None else time.sleep
        self._monotonic = monotonic if monotonic is not None else time.monotonic

    def estimate_cost_usd(self, request: VideoRequest) -> float:
        """Per-second billing from the price table, fractional seconds ceiled
        up (§6.6) — the confirmation-flow estimate and the recorded spend."""
        return estimate_video_cost(load_prices(), PROVIDER_ID, duration_s=request.duration_s)

    def generate(self, request: VideoRequest) -> VideoResult:
        """Generate one clip; write the MP4; return its **probed** duration."""
        _require_generatable(request)
        interaction = self.client.post_json(INTERACTIONS_PATH, json=self._body(request))
        interaction = self._await_interaction(interaction)
        video = self._extract_video(interaction)
        payload = video if isinstance(video, bytes) else self._poll_and_download(video)
        request.out_path.parent.mkdir(parents=True, exist_ok=True)
        request.out_path.write_bytes(payload)
        return VideoResult(file=request.out_path, duration_s=self._probed_duration(request))

    # --- submit ---------------------------------------------------------------------

    def _body(self, request: VideoRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self._model,
            "input": [
                {
                    "type": "user_input",
                    "content": [
                        {
                            "type": "text",
                            "text": compose_video_prompt(
                                request.prompt, duration_s=request.duration_s
                            ),
                        }
                    ],
                }
            ],
            "response_format": {
                "type": "video",
                "aspect_ratio": aspect_ratio_for(request.width, request.height),
                "resolution": DEFAULT_RESOLUTION,
                "delivery": "uri",
            },
        }
        if self._background:
            # Spike §3.1: background:true is the documented async alternative
            # and REQUIRES store:true (combining it with store:false is a
            # documented 400) — sent explicitly so the invariant holds even
            # if the API's default ever changes.
            body["background"] = True
            body["store"] = True
        return body

    # --- poll level 1: the interaction (spike §3.1) -----------------------------------

    def _await_interaction(self, interaction: dict[str, Any]) -> dict[str, Any]:
        """Continue a non-terminal interaction to a terminal state.

        The unary POST normally returns ``completed`` in one call; the
        documented ``background`` mode returns an id + ``in_progress``
        immediately and is polled at ``GET /v1beta/interactions/{id}`` (docs'
        5 s cadence). An early return without ``background`` is continued the
        same documented way rather than failed. Terminal ``failed``/
        ``cancelled`` and ``requires_action`` (this app cannot answer tool
        calls) are permanent; unknown states keep polling until the budget
        runs out (retryable). A response with no status is passed through —
        extraction decides whether it actually carries a video (a completed-
        shaped body with no status that still has the paid clip must not be
        discarded).
        """
        status = interaction.get("status")
        if status == "completed" or not isinstance(status, str) or not status:
            return interaction
        if status in ("failed", "cancelled"):
            raise GeminiError(
                f"The Gemini video interaction ended with status {status!r} —"
                " no clip was produced; regenerate the asset.",
                retryable=False,
            )
        if status == "requires_action":
            raise GeminiError(
                "The Gemini video interaction paused with status"
                " 'requires_action' — the model is asking for input this app"
                " cannot provide; regenerate the asset. If this keeps"
                " happening, the response shape may have drifted"
                " (research/gemini-video.md §3.1).",
                retryable=False,
            )
        return self._poll_interaction(interaction)

    def _poll_interaction(self, interaction: dict[str, Any]) -> dict[str, Any]:
        """Poll ``GET /v1beta/interactions/{id}`` until a terminal status."""
        interaction_id = str(interaction.get("id") or "").strip()
        if not interaction_id:
            raise GeminiError(
                "The Gemini video response carries no interaction id, so the"
                " non-terminal interaction cannot be polled — nothing was"
                " produced; regenerate the asset.",
                retryable=False,
            )
        path = f"/v1beta/interactions/{interaction_id}"
        deadline = self._monotonic() + self._max_poll_s
        while True:
            self._sleep(self._poll_interval_s)
            if self._monotonic() > deadline:
                raise GeminiError(
                    f"The Gemini video interaction {interaction_id} did not"
                    " reach a terminal state within"
                    f" {self._max_poll_s:.0f}s of polling — generation may"
                    " still be running server-side; it will be retried"
                    " automatically.",
                    retryable=True,
                )
            interaction = self.client.get_json(path)
            status = interaction.get("status")
            if status == "completed" or not isinstance(status, str) or not status:
                return interaction
            if status in ("failed", "cancelled"):
                raise GeminiError(
                    f"The Gemini video interaction ended with status {status!r}"
                    " while polling — no clip was produced; regenerate the"
                    " asset.",
                    retryable=False,
                )
            if status == "requires_action":
                raise GeminiError(
                    f"The Gemini video interaction {interaction_id} paused"
                    " with status 'requires_action' while polling — the model"
                    " is asking for input this app cannot provide; regenerate"
                    " the asset.",
                    retryable=False,
                )
            # in_progress and unknown states: keep polling (bounded above).

    # --- extract (spike §1 envelope, §3.2 gotcha) -------------------------------------

    def _extract_video(self, interaction: dict[str, Any]) -> bytes | str:
        """Walk the REST envelope (spike §1): ``steps[]`` entries of type
        ``model_output`` carry ``content[]`` video blocks. A block carries
        base64 ``data`` (inline delivery) or a ``uri`` (URI delivery) — the
        URI is captured from THIS response (the creation response) because a
        refetch returns inline base64 even for URI deliveries (spike §3.2).
        One response = one clip: none is permanent, several are refused
        rather than picked or spliced (the 2.3/3.1 precedent)."""
        blocks: list[dict[str, Any]] = []
        for step in _as_list(interaction.get("steps")):
            if not isinstance(step, dict) or step.get("type") != "model_output":
                continue
            for block in _as_list(step.get("content")):
                if isinstance(block, dict) and block.get("type") == "video":
                    blocks.append(block)
        if not blocks:
            raise GeminiError(
                "Gemini returned a completed response but it contains no"
                " video (no video block under steps[].content) — nothing was"
                " written; regenerate the asset. If this keeps happening, the"
                " model response shape may have drifted"
                " (research/gemini-video.md §1).",
                retryable=False,
            )
        if len(blocks) > 1:
            raise GeminiError(
                f"Gemini returned {len(blocks)} video blocks for a request"
                " that asked for exactly one — picking one silently would"
                " ship an arbitrary clip and joining chunks would splice one"
                " corrupt file, so nothing was written; regenerate the asset."
                " If this keeps happening, the model response shape may have"
                " drifted (research/gemini-video.md §1).",
                retryable=False,
            )
        block = blocks[0]
        uri = block.get("uri") or block.get("file_uri")
        if isinstance(uri, str) and uri.strip():
            return uri.strip()
        data = block.get("data")
        if isinstance(data, str) and data.strip():
            return _decode_video(data)
        raise GeminiError(
            "Gemini returned a video block with neither base64 data nor a"
            " download URI — nothing was written; regenerate the asset. If"
            " this keeps happening, the model response shape may have"
            " drifted (research/gemini-video.md §1).",
            retryable=False,
        )

    # --- poll level 2 + download (spike §3.2, §4) ---------------------------------------

    def _poll_and_download(self, uri: str) -> bytes:
        """Poll the generated file to ``ACTIVE`` at ``GET /v1beta/files/{id}``
        (the URI's file is not immediately downloadable), then download the
        bytes via the captured creation URI — on the Gemini host, so the key
        header attaches there and only there (§6.5)."""
        file_id = _file_id_from_uri(uri)
        deadline = self._monotonic() + self._max_poll_s
        while True:
            metadata = self.client.get_json(f"/v1beta/files/{file_id}")
            state = metadata.get("state")
            if state == "ACTIVE":
                return self.client.download(uri)
            if state == "FAILED":
                raise GeminiError(
                    f"Google's Files API reports the generated video"
                    f" (file {file_id}) failed processing — no usable clip"
                    " exists; regenerate the asset.",
                    retryable=False,
                )
            # PROCESSING / STATE_UNSPECIFIED / unknown: keep polling (bounded).
            if self._monotonic() > deadline:
                raise GeminiError(
                    f"The generated video (file {file_id}) did not become"
                    f" downloadable within {self._max_poll_s:.0f}s of polling"
                    " — the file may still become ACTIVE server-side (files"
                    " persist 48h); it will be retried automatically.",
                    retryable=True,
                )
            self._sleep(self._poll_interval_s)

    # --- durations: probed, never trusted ---------------------------------------------

    def _probed_duration(self, request: VideoRequest) -> float:
        """ffprobe the written file — the authoritative duration (§1.2)."""
        try:
            duration = float(self._probe(request.out_path))
        except FFmpegError as exc:
            raise GeminiError(
                f"The generated video for {request.out_path.name} is not a"
                f" playable file (ffprobe failed: {exc}) — regenerate the"
                " asset.",
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
                f"The generated video for {request.out_path.name} probes as"
                f" {duration:g}s — an empty/invalid file; regenerate the"
                " asset.",
                retryable=False,
            )
        return duration


# --- request/response helpers -------------------------------------------------------


def _configured_model() -> str:
    """The spike-verified video model id from the editable price table."""
    model = load_prices().price_for(PROVIDER_ID, "video").model
    if not model:
        raise ValueError(
            "app/data/prices.json has no 'model' for the gemini video engine"
            " — the Gemini video provider refuses to guess a model id"
            " (see research/gemini-video.md §1)."
        )
    return model


def _require_generatable(request: VideoRequest) -> None:
    """Fail before any traffic when the beat cannot be generated at all."""
    if not request.prompt.strip():
        raise GeminiError(
            "The video beat has no prompt — add an image hint to the beat in"
            " the script review step, then regenerate.",
            retryable=False,
        )
    if not math.isfinite(request.duration_s):
        raise GeminiError(
            f"The video beat has a non-finite duration ({request.duration_s!r})"
            " — check the beat's audio timing, then regenerate.",
            retryable=False,
        )
    if request.duration_s < MIN_CLIP_S:
        raise GeminiError(
            f"The video beat is {request.duration_s:g}s but the Gemini video"
            f" model renders clips of {MIN_CLIP_S:g}–{MAX_CLIP_S:g}s — merge"
            " this beat with an adjacent one in the script review step, then"
            " regenerate.",
            retryable=False,
        )
    if request.duration_s > MAX_CLIP_S:
        raise GeminiError(
            f"The video beat is {request.duration_s:g}s but the Gemini video"
            f" model renders clips of {MIN_CLIP_S:g}–{MAX_CLIP_S:g}s — split"
            " this beat in the script review step, then regenerate.",
            retryable=False,
        )
    if request.width <= 0 or request.height <= 0:
        raise GeminiError(
            f"Video dimensions must be positive, got {request.width}x"
            f"{request.height} — check the platform resolution in the"
            " options step, then regenerate.",
            retryable=False,
        )
    if request.out_path.suffix.lower() not in _MIME_BY_SUFFIX:
        raise GeminiError(
            f"The video output path ends in {request.out_path.suffix!r} but"
            " the Gemini video API only produces video/mp4 — check the"
            " platform format in the options step, then regenerate.",
            retryable=False,
        )


def _file_id_from_uri(uri: str) -> str:
    """The Files API id inside a delivery URI
    (``…/v1beta/files/<id>:download?alt=media``)."""
    match = _FILE_ID_RE.search(uri)
    if match is None:
        raise GeminiError(
            f"The Gemini video delivery URI ({uri[:120]}) carries no parseable"
            " Files API file id, so the generated file cannot be polled or"
            " downloaded — nothing was written; regenerate the asset. If this"
            " keeps happening, the response shape may have drifted"
            " (research/gemini-video.md §3.2).",
            retryable=False,
        )
    return match.group(1)


def _decode_video(data: str) -> bytes:
    try:
        return base64.b64decode(data, validate=True)
    except (ValueError, TypeError) as exc:
        raise GeminiError(
            "Gemini returned a video block whose base64 payload could not be"
            f" decoded ({exc}) — nothing was written; regenerate the asset.",
            retryable=False,
        ) from exc


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []
