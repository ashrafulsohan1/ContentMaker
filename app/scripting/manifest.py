"""Timing manifest — read/write the exact §6.2 schema (BUILD_PLAN 1.7).

The manifest is the **single source of truth for assembly** (§6.2–6.4):
stitching reads start_s / duration_s / total_duration_s from it and from
nothing else. Consequences baked in here:

- The JSON shape is exactly the spec's — four top-level keys, six per-beat
  keys. Reading rejects anything else (missing/unknown keys, wrong types,
  indexes not 1..n, non-contiguous timeline, total ≠ sum of beats) with a
  ``ManifestError`` naming the offending key/beat. A corrupt source of truth
  must fail loudly, never be guessed at.
- Durations are recorded at **build** time: a beat with audio carries its
  (ffprobe-probed, task 2.4) duration; a beat without audio gets
  ``DEFAULT_SILENT_BEAT_DURATION_S`` seconds (§6.3) recorded in the manifest
  like any other duration. ``read_manifest`` never re-assigns defaults —
  it honors what is recorded.
- The timeline is contiguous: the first beat starts at 0, every beat starts
  where the previous one ends, and total_duration_s equals the sum of beat
  durations (ms-tolerance on read, so ms-rounded manifests stay readable).
  All written values are rounded to whole milliseconds.
"""
from __future__ import annotations

import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_FPS = 30
DEFAULT_SILENT_BEAT_DURATION_S = 3.0  # §6.3 "sensible default" for a silent beat
_TIMELINE_TOLERANCE_S = 1e-3  # one millisecond — well under a frame at 30 fps
_MS_DECIMALS = 3

_ROOT_KEYS = frozenset({"job_id", "fps_target", "total_duration_s", "beats"})
_BEAT_KEYS = frozenset({"index", "label", "text", "audio_file", "start_s", "duration_s"})


class ManifestError(ValueError):
    """The manifest violates the §6.2 schema — fail loudly, never guess."""


@dataclass(frozen=True)
class ManifestBeat:
    """One beat of the manifest, field-for-field the §6.2 beat object."""

    index: int
    label: str | None
    text: str
    audio_file: str | None  # project-relative, forward slashes ("audio/beat_01.mp3")
    start_s: float
    duration_s: float


@dataclass(frozen=True)
class TimingManifest:
    """The §6.2 document: assembly's single source of truth."""

    job_id: str
    fps_target: int | float
    total_duration_s: float
    beats: tuple[ManifestBeat, ...]


@dataclass(frozen=True, kw_only=True)
class BeatSpec:
    """Input for :func:`build_manifest` — one beat's text plus its audio, if any.

    A beat with audio carries ``audio_file`` **and** its (probed) ``duration_s``;
    a silent beat carries neither and receives the default duration.
    """

    text: str
    label: str | None = None
    audio_file: str | Path | None = None
    duration_s: float | None = None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _manifest_from_data(data: Any) -> TimingManifest:
    """Validate raw JSON data against the exact §6.2 schema and build from it.

    The one schema authority: both ``read_manifest`` (from disk) and
    ``build_manifest`` (from computed values) construct through it, so the
    read and write paths can never drift apart.
    """
    if not isinstance(data, dict):
        raise ManifestError(f"manifest must be a JSON object, got {type(data).__name__}")
    keys = set(data)
    missing = sorted(_ROOT_KEYS - keys)
    unknown = sorted(keys - _ROOT_KEYS)
    if missing:
        raise ManifestError(f"manifest is missing key(s): {', '.join(missing)}")
    if unknown:
        raise ManifestError(
            f"manifest has unknown key(s) (§6.2 schema is exact): {', '.join(unknown)}"
        )

    job_id = data["job_id"]
    if not isinstance(job_id, str) or not job_id.strip():
        raise ManifestError(f"job_id must be a non-empty string, got {job_id!r}")
    fps_target = data["fps_target"]
    if not _is_number(fps_target) or fps_target <= 0:
        raise ManifestError(f"fps_target must be a positive number, got {fps_target!r}")
    total = data["total_duration_s"]
    if not _is_number(total) or total < 0:
        raise ManifestError(f"total_duration_s must be a non-negative number, got {total!r}")

    raw_beats = data["beats"]
    if not isinstance(raw_beats, list) or not raw_beats:
        raise ManifestError("beats must be a non-empty list")
    beats = tuple(_beat_from_data(position, raw) for position, raw in enumerate(raw_beats, 1))
    if [beat.index for beat in beats] != list(range(1, len(beats) + 1)):
        indexes = [beat.index for beat in beats]
        raise ManifestError(f"beat indexes must be exactly 1..n in order, got {indexes}")

    _validate_timeline(beats, float(total))
    return TimingManifest(
        job_id=job_id, fps_target=fps_target, total_duration_s=float(total), beats=beats
    )


def _beat_from_data(position: int, raw: Any) -> ManifestBeat:
    if not isinstance(raw, dict):
        raise ManifestError(f"beat {position} must be a JSON object, got {type(raw).__name__}")
    keys = set(raw)
    missing = sorted(_BEAT_KEYS - keys)
    unknown = sorted(keys - _BEAT_KEYS)
    if missing:
        raise ManifestError(f"beat {position} is missing key(s): {', '.join(missing)}")
    if unknown:
        raise ManifestError(
            f"beat {position} has unknown key(s) (§6.2 schema is exact): {', '.join(unknown)}"
        )

    index = raw["index"]
    if not isinstance(index, int) or isinstance(index, bool) or index < 1:
        raise ManifestError(f"beat {position}: index must be a positive integer, got {index!r}")
    label = raw["label"]
    if label is not None and (not isinstance(label, str) or not label.strip()):
        raise ManifestError(
            f"beat {position}: label must be a non-empty string or null, got {label!r}"
        )
    text = raw["text"]
    if not isinstance(text, str) or not text.strip():
        raise ManifestError(f"beat {position}: text must be a non-empty string, got {text!r}")
    audio_file = raw["audio_file"]
    if audio_file is not None and (not isinstance(audio_file, str) or not audio_file.strip()):
        raise ManifestError(
            f"beat {position}: audio_file must be a non-empty string or null, got {audio_file!r}"
        )
    start_s = raw["start_s"]
    if not _is_number(start_s) or start_s < 0:
        raise ManifestError(
            f"beat {position}: start_s must be a non-negative number, got {start_s!r}"
        )
    duration_s = raw["duration_s"]
    if not _is_number(duration_s) or duration_s <= 0:
        raise ManifestError(
            f"beat {position}: duration_s must be a positive number, got {duration_s!r}"
        )
    return ManifestBeat(
        index=index,
        label=label,
        text=text,
        audio_file=audio_file,
        start_s=float(start_s),
        duration_s=float(duration_s),
    )


def _validate_timeline(beats: tuple[ManifestBeat, ...], total: float) -> None:
    first = beats[0]
    if abs(first.start_s) > _TIMELINE_TOLERANCE_S:
        raise ManifestError(
            f"beat {first.index}: start_s {first.start_s} — the first beat starts at 0"
        )
    end = first.start_s + first.duration_s
    for beat in beats[1:]:
        if abs(beat.start_s - end) > _TIMELINE_TOLERANCE_S:
            raise ManifestError(
                f"beat {beat.index}: start_s {beat.start_s} does not continue the timeline"
                f" (previous beat ends at {round(end, _MS_DECIMALS)}); timing must be contiguous"
            )
        end = beat.start_s + beat.duration_s
    if abs(total - end) > _TIMELINE_TOLERANCE_S:
        raise ManifestError(
            f"total_duration_s {total} does not match the beat timeline"
            f" (beats sum to {round(end, _MS_DECIMALS)})"
        )


def build_manifest(
    job_id: str,
    beats: Sequence[BeatSpec],
    *,
    fps_target: int | float = DEFAULT_FPS,
) -> TimingManifest:
    """Assemble a valid manifest: sequential start times, totals, silent defaults.

    A beat without audio (no ``audio_file``/``duration_s``) gets
    ``DEFAULT_SILENT_BEAT_DURATION_S`` recorded as its duration (§6.3,
    BUILD_PLAN §10.2). Durations/starts/totals are rounded to whole
    milliseconds.
    """
    specs = list(beats)
    if not specs:
        raise ManifestError("cannot build a timing manifest with zero beats")

    beat_data: list[dict[str, Any]] = []
    start = 0.0
    for position, spec in enumerate(specs, 1):
        raw_audio = spec.audio_file
        audio_file = raw_audio.as_posix() if isinstance(raw_audio, Path) else raw_audio
        duration = spec.duration_s
        if (audio_file is None) != (duration is None):
            raise ManifestError(
                f"beat {position}: audio_file and duration_s must be set together — a beat"
                " with audio carries its (probed) duration, a silent beat carries neither"
                f" and gets DEFAULT_SILENT_BEAT_DURATION_S ({DEFAULT_SILENT_BEAT_DURATION_S}s)"
            )
        if audio_file is None:
            duration_s: float = DEFAULT_SILENT_BEAT_DURATION_S
        else:
            if not _is_number(duration) or duration <= 0:
                raise ManifestError(
                    f"beat {position}: duration_s must be a positive number of seconds,"
                    f" got {duration!r}"
                )
            duration_s = round(float(duration), _MS_DECIMALS)
        beat_data.append(
            {
                "index": position,
                "label": spec.label,
                "text": spec.text,
                "audio_file": audio_file,
                "start_s": round(start, _MS_DECIMALS),
                "duration_s": duration_s,
            }
        )
        start = beat_data[-1]["start_s"] + duration_s

    return _manifest_from_data(
        {
            "job_id": job_id,
            "fps_target": fps_target,
            "total_duration_s": round(start, _MS_DECIMALS),
            "beats": beat_data,
        }
    )


def with_probed_durations(
    specs: Sequence[BeatSpec],
    *,
    probe: Callable[[Path | str], float],
) -> tuple[BeatSpec, ...]:
    """Fill audio-bearing specs' durations by **ffprobing each audio file**.

    The one seam where audio durations enter the manifest (BUILD_PLAN 2.4;
    §1.2 "Durations"): a spec with an ``audio_file`` gets its ``duration_s``
    from ``probe`` — any pre-filled value (a provider-reported duration, a
    stale cache) is **discarded and re-probed**, never trusted. Silent specs
    pass through untouched and keep their build-time default. Probed values
    are rounded to whole milliseconds; a failed or non-positive probe raises
    :class:`ManifestError` naming the beat and file — a manifest must never
    carry a guessed duration.

    ``audio_file`` paths must resolve on the filesystem (absolute paths are
    the safe form — the probe runs in the caller's process); the §6.2
    project-relative spelling is a manifest-presentation concern, applied
    after probing.
    """
    probed: list[BeatSpec] = []
    for position, spec in enumerate(specs, 1):
        if spec.audio_file is None:
            probed.append(spec)
            continue
        probed.append(
            BeatSpec(
                text=spec.text,
                label=spec.label,
                audio_file=spec.audio_file,
                duration_s=_probe_one(position, spec.audio_file, probe),
            )
        )
    return tuple(probed)


def _probe_one(
    position: int, audio_file: Path | str, probe: Callable[[Path | str], float]
) -> float:
    """One file's probed duration, ms-rounded and validated; loud on failure."""
    where = f"beat {position}: audio_file {Path(audio_file).name}"
    try:
        duration = float(probe(audio_file))
    except Exception as exc:
        raise ManifestError(
            f"{where}: could not probe the audio duration ({exc}) — the"
            " timing manifest is written from ffprobed values only."
        ) from exc
    if not math.isfinite(duration) or duration <= 0:
        raise ManifestError(
            f"{where}: probed duration must be a positive number of seconds,"
            f" got {duration!r} — the audio file looks empty or invalid."
        )
    return round(duration, _MS_DECIMALS)


def write_manifest(manifest: TimingManifest, path: Path) -> Path:
    """Write the manifest as UTF-8 JSON in exactly the §6.2 shape; returns path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "job_id": manifest.job_id,
        "fps_target": manifest.fps_target,
        "total_duration_s": manifest.total_duration_s,
        "beats": [
            {
                "index": beat.index,
                "label": beat.label,
                "text": beat.text,
                "audio_file": beat.audio_file,
                "start_s": beat.start_s,
                "duration_s": beat.duration_s,
            }
            for beat in manifest.beats
        ],
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def read_manifest(path: Path) -> TimingManifest:
    """Load and validate a manifest; any §6.2 violation raises ManifestError."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{path}: manifest is not valid JSON: {exc}") from exc
    return _manifest_from_data(data)
