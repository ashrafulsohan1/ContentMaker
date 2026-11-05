"""Wizard state + beat operations for the generation flow (BUILD_PLAN 1.10).

The UI is a four-step wizard (spec §4/§8): input → beat review → options →
estimate + confirm → generate. This module is the pure domain seam the HTTP
routes drive:

- ``FlowState`` holds one wizard run: the created ``Project``, the original
  script, the editable beat list, chosen options and (after confirm-able
  options) the job id. Wizard drafts are deliberately **in-memory** (an LRU
  capped by ``FlowStore``): they are cheap to redo, while everything that
  costs money — jobs — is persisted by the job engine (§6.6).
- Beat operations (edit / merge with next / split into sentences) never
  corrupt neighbors: they raise ``FlowError`` with a user-facing sentence
  for unknown ids, blank text, merging the last beat, or nothing to split.
- ``FlowOptions`` + ``validate_options`` encode the rules that make a job
  meaningful: audio on or a visual type chosen (never nothing), video
  visuals only on video formats, image/carousel formats require images,
  carousel min/max image counts honored, non-negative spend cap.
- ``build_assets`` maps beats + options to ``AssetSpec`` paths under the
  project's §7 folders (audio/visuals), with engine availability resolved
  from the registry — a needed engine with no provider raises
  ``MissingEngineError`` naming exactly what's missing (§6.5).
"""
from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from app.core.jobs import AssetSpec
from app.core.keys import KeyStore
from app.core.projects import Project
from app.engines.registry import ENGINES, Registry
from app.providers.catalog import PROVIDERS
from app.scripting.manifest import DEFAULT_SILENT_BEAT_DURATION_S
from app.scripting.segment import split_into_sentences

PAGE_SIZE = 10  # review pagination — keeps 100+ beat scripts usable (risk #8)
VISUAL_CHOICES: tuple[str, ...] = ("none", "images", "video")
DEFAULT_VOICE = "default"


class FlowError(ValueError):
    """A wizard request that cannot be honored; message is user-facing."""


class MissingEngineError(FlowError):
    """A needed engine has no provider registered (§6.5: say what's missing)."""

    def __init__(self, engine: str) -> None:
        super().__init__(
            f"The {engine} engine has no provider registered — add an API key in"
            " Settings, or tick dry-run to use the zero-cost mock providers."
        )
        self.engine = engine


class MissingKeyError(FlowError):
    """A needed BYOK provider has no configured key (§6.5: say what's missing)."""

    def __init__(self, engine: str, provider_id: str, label: str) -> None:
        super().__init__(
            f"The {engine} engine is set to {label}, but no {label} API key is"
            " configured — add your key in Settings, or tick dry-run to use the"
            " zero-cost mock providers."
        )
        self.engine = engine
        self.provider_id = provider_id


@dataclass
class Beat:
    """One reviewable beat: stable id + position-independent content."""

    id: str
    text: str
    label: str | None = None
    img_hint: str | None = None


@dataclass
class FlowOptions:
    """Choices made in the options step (all validated before a job is made)."""

    platform_id: str
    format_id: str
    preset_id: str | None = None
    audio: bool = True
    visual: str = "none"  # "none" | "images" | "video"
    style: str | None = None
    voice: str | None = None
    pace: float = 1.0
    tone: str | None = None
    dry_run: bool = True
    spend_cap_usd: float | None = None
    providers: dict[str, str] = field(default_factory=dict)  # engine → provider id


@dataclass
class FlowState:
    """One wizard run from input to a confirmed job."""

    id: str
    project: Project
    script_text: str
    beats: list[Beat]
    options: FlowOptions | None = None
    job_id: str | None = None

    def beat(self, beat_id: str) -> Beat:
        for beat in self.beats:
            if beat.id == beat_id:
                return beat
        raise FlowError(f"No beat with id {beat_id!r} in this flow.")

    def position(self, beat_id: str) -> int:
        """0-based position of the beat — pages are computed from it."""
        for index, beat in enumerate(self.beats):
            if beat.id == beat_id:
                return index
        raise FlowError(f"No beat with id {beat_id!r} in this flow.")


class FlowStore:
    """Thread-safe in-memory wizard drafts, LRU-capped (drafts are redoable)."""

    def __init__(self, max_flows: int = 32) -> None:
        self._max = max(1, max_flows)
        self._flows: OrderedDict[str, FlowState] = OrderedDict()
        self._lock = threading.Lock()

    def create(self, project: Project, script_text: str, beats: list[Beat]) -> FlowState:
        flow = FlowState(id=uuid.uuid4().hex, project=project, script_text=script_text, beats=beats)
        with self._lock:
            self._flows[flow.id] = flow
            while len(self._flows) > self._max:
                self._flows.popitem(last=False)
        return flow

    def get(self, flow_id: str | None) -> FlowState | None:
        if not flow_id:
            return None
        with self._lock:
            flow = self._flows.get(flow_id)
            if flow is not None:
                self._flows.move_to_end(flow_id)
            return flow

    def find_by_job(self, job_id: str) -> FlowState | None:
        with self._lock:
            for flow in self._flows.values():
                if flow.job_id == job_id:
                    self._flows.move_to_end(flow.id)
                    return flow
        return None


# --- beat review operations (edit / merge / split) ---------------------------


def update_beat(flow: FlowState, beat_id: str, *, text: str, label: str | None) -> None:
    beat = flow.beat(beat_id)
    cleaned = text.strip()
    if not cleaned:
        raise FlowError(
            "A beat needs some text — clear the text and use Delete later, or merge it."
        )
    beat.text = cleaned
    beat.label = label.strip() if label and label.strip() else None


def merge_with_next(flow: FlowState, beat_id: str) -> str | None:
    """Fold the beat into the one after it; returns a notice or None."""
    index = flow.position(beat_id)
    if index >= len(flow.beats) - 1:
        return "This is the last beat — there is no next beat to merge with."
    beat, nxt = flow.beats[index], flow.beats[index + 1]
    beat.text = f"{beat.text}\n{nxt.text}"
    beat.label = beat.label if beat.label is not None else nxt.label
    beat.img_hint = beat.img_hint if beat.img_hint is not None else nxt.img_hint
    flow.beats.pop(index + 1)
    return None


def split_beat(flow: FlowState, beat_id: str) -> str | None:
    """Split the beat into one beat per sentence; returns a notice or None."""
    beat = flow.beat(beat_id)
    sentences = split_into_sentences(beat.text)
    if len(sentences) <= 1:
        return "This beat is a single sentence — there is nothing to split."
    index = flow.position(beat_id)
    parts = [
        Beat(id=_new_beat_id(), text=sentence, label=beat.label if i == 0 else None,
             img_hint=beat.img_hint if i == 0 else None)
        for i, sentence in enumerate(sentences)
    ]
    flow.beats[index : index + 1] = parts
    return None


def _new_beat_id() -> str:
    return f"b{uuid.uuid4().hex[:10]}"


def beats_from_script(script_text: str) -> list[Beat]:
    """Auto-segment a script into reviewable beats (spec §6.1)."""
    from app.scripting.segment import segment_script

    return [
        Beat(id=_new_beat_id(), text=parsed.text, label=parsed.label, img_hint=parsed.img_hint)
        for parsed in segment_script(script_text)
    ]


# --- pagination ---------------------------------------------------------------


def page_count(n_items: int) -> int:
    return max(1, -(-n_items // PAGE_SIZE))


def page_slice(beats: list[Beat], page: int) -> tuple[list[Beat], int, int]:
    """Clamp ``page`` into range; returns (beats on page, page, total pages)."""
    total = page_count(len(beats))
    current = min(max(1, page), total)
    start = (current - 1) * PAGE_SIZE
    return beats[start : start + PAGE_SIZE], current, total


# --- options validation + asset building -------------------------------------


def engines_needed(options: FlowOptions) -> tuple[str, ...]:
    """Engines the chosen audio/visual combination will call (§6.2/§6.3)."""
    needed: list[str] = ["tts"] if options.audio else []
    if options.visual == "images":
        needed.append("image")
    elif options.visual == "video":
        needed.append("video")
    return tuple(needed)


def available_engines(registry: Registry) -> dict[str, list[str]]:
    """Engine → registered provider ids, for the availability display (§6.5)."""
    return {engine: [p.provider_id for p in registry.providers_for(engine)] for engine in ENGINES}


def engine_availability(
    registry: Registry, key_store: KeyStore | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Engine → provider rows for the availability display, keys included (§6.5).

    Each row: ``id``/``label``, ``configured`` (BYOK: a key is on file; mocks
    are always runnable), ``is_default`` (the key store's per-engine default)
    and ``kind`` (``byok`` | ``mock`` | ``builtin``). Catalog providers appear
    even without a key — the UI must be able to say *what's* missing.
    """
    defaults = key_store.defaults() if key_store is not None else {}
    rows: dict[str, list[dict[str, Any]]] = {}
    for engine in ENGINES:
        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        for info in PROVIDERS.values():
            if engine in info.engines:
                seen.add(info.id)
                entries.append(
                    {
                        "id": info.id,
                        "label": info.label,
                        "configured": bool(key_store is not None and key_store.has_key(info.id)),
                        "is_default": defaults.get(engine) == info.id,
                        "kind": "byok",
                    }
                )
        for provider in registry.providers_for(engine):
            pid = provider.provider_id
            if pid in seen:
                continue
            kind = "mock" if pid == "mock" else "builtin"
            entries.append(
                {
                    "id": pid,
                    "label": "Mock (dry-run)" if pid == "mock" else pid,
                    "configured": True,
                    "is_default": False,
                    "kind": kind,
                }
            )
        rows[engine] = entries
    return rows


def validate_options(
    options: FlowOptions,
    *,
    format_kind: str,
    min_images: int,
    max_images: int,
    n_beats: int,
    registry: Registry,
) -> list[str]:
    """All the reasons a job from these options would be meaningless or wrong."""
    errors: list[str] = []
    if not options.audio and options.visual == "none":
        errors.append(
            "There is nothing to generate: turn audio on or choose a visual type"
            " (images or video)."
        )
    if options.visual not in VISUAL_CHOICES:
        errors.append(f"Unknown visual type {options.visual!r}.")
    if options.visual == "video" and format_kind != "video":
        errors.append(
            f"Generated video clips need a video format — this format delivers"
            f" {format_kind}s; pick images as the visual type instead."
        )
    if format_kind == "video" and options.visual == "none":
        errors.append(
            "A video needs visuals to stitch: choose video or images as the visual type."
        )
    if format_kind in ("image", "carousel") and options.visual == "none":
        errors.append(
            f"This format delivers {format_kind} images — choose the images visual type."
        )
    if format_kind == "carousel" and options.visual == "images":
        if n_beats < min_images:
            errors.append(
                f"A carousel needs at least {min_images} images — this script has"
                f" {n_beats} beat(s). Add beats or pick a single-image format."
            )
        elif n_beats > max_images:
            errors.append(
                f"This carousel holds at most {max_images} images — this script has"
                f" {n_beats} beats. Merge beats or pick another format."
            )
    if options.spend_cap_usd is not None and options.spend_cap_usd < 0:
        errors.append("The spend cap cannot be negative.")
    return errors


def resolve_providers(
    options: FlowOptions,
    registry: Registry,
    key_store: KeyStore | None = None,
) -> dict[str, str]:
    """Engine → provider id for the job; dry-run forces mocks (§1.2).

    Resolution order per needed engine: explicit user selection, then the key
    store's per-engine default (§6.5), then the first registered provider. A
    BYOK provider (from the catalog) runs only with a configured key —
    otherwise ``MissingKeyError`` names the engine, the provider and exactly
    what to do (§6.5); with a key but no registered adapter, the error says
    the build doesn't ship that engine's provider yet.
    """
    resolved: dict[str, str] = {}
    for engine in engines_needed(options):
        if options.dry_run:
            try:
                registry.get(engine, "mock")
            except ValueError:
                raise MissingEngineError(engine) from None
            resolved[engine] = "mock"
            continue
        chosen = options.providers.get(engine)
        default = key_store.default_for(engine) if key_store is not None else None
        provider_id = chosen or default
        if provider_id:
            resolved[engine] = _require_runnable(engine, provider_id, registry, key_store)
            continue
        registered = registry.providers_for(engine)
        if not registered:
            raise MissingEngineError(engine)
        resolved[engine] = registered[0].provider_id
    return resolved


def _require_runnable(
    engine: str,
    provider_id: str,
    registry: Registry,
    key_store: KeyStore | None,
) -> str:
    """Gate one BYOK/explicit provider choice on key + adapter (§6.5)."""
    info = PROVIDERS.get(provider_id)
    if info is not None:
        if key_store is None or not key_store.has_key(provider_id):
            raise MissingKeyError(engine, provider_id, info.label)
        if not any(p.provider_id == provider_id for p in registry.providers_for(engine)):
            raise FlowError(
                f"{info.label} is configured for the {engine} engine, but this build"
                f" of ContentMaker does not include its {engine} provider yet —"
                " pick another provider or tick dry-run."
            )
    try:
        registry.get(engine, provider_id)  # names engine + provider when unknown
    except ValueError as exc:
        raise FlowError(str(exc)) from None
    return provider_id


def build_assets(
    flow: FlowState, options: FlowOptions, *, resolution: tuple[int, int]
) -> list[AssetSpec]:
    """Map beats + options to provider work under the project's §7 folders."""
    assets: list[AssetSpec] = []
    width, height = resolution
    for index, beat in enumerate(flow.beats, 1):
        if options.audio:
            assets.append(
                AssetSpec(
                    kind="tts",
                    beat_index=index,
                    out_path=flow.project.subdir("audio") / f"beat_{index:02d}.wav",
                    params=_tts_params(options, beat),
                )
            )
        if options.visual == "images":
            assets.append(
                AssetSpec(
                    kind="image",
                    beat_index=index,
                    out_path=flow.project.subdir("visuals") / f"beat_{index:02d}.png",
                    params={
                        "prompt": beat.img_hint or beat.text,
                        "style_hint": options.style,
                        "width": width,
                        "height": height,
                    },
                )
            )
        elif options.visual == "video":
            assets.append(
                AssetSpec(
                    kind="video",
                    beat_index=index,
                    out_path=flow.project.subdir("visuals") / f"beat_{index:02d}.mp4",
                    params={
                        "prompt": beat.img_hint or beat.text,
                        "duration_s": DEFAULT_SILENT_BEAT_DURATION_S,
                        "width": width,
                        "height": height,
                    },
                )
            )
    return assets


def _tts_params(options: FlowOptions, beat: Beat) -> dict[str, Any]:
    return {
        "text": beat.text,
        "voice": options.voice or DEFAULT_VOICE,
        "pace": options.pace,
        "tone": options.tone,
    }
