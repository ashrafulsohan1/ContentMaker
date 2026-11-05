"""Job → deliverables orchestration (BUILD_PLAN 1.12; spec §6.2–6.4, §7).

``assemble_job`` turns one persisted job into finished files in the project's
``output/`` folder, driven exclusively by what is on disk:

1. **Timing manifest first (§6.2)** — the duration of every completed beat's
   audio is *probed* with ffprobe (never trusted from a provider), the
   manifest is built via ``scripting.manifest.build_manifest`` and written to
   ``plan/<job_id>_manifest.json``. The manifest covers exactly the beats that
   made it into the deliverables — on a partial job that means the completed
   beats only (renumbered to the §6.2 contiguous 1..n; the retained
   ``audio_file`` keeps the provenance), and failed/skipped beats rejoin after
   their retry re-assembles.
2. **Video formats** — one MP4 per format (``stitch_video``): manifest beats,
   per-beat visuals (images looped, clips loop-padded), per-beat audio.
3. **Image/carousel formats** — one platform-correct export per completed
   beat (``export_image``), with the plan's optional per-image text overlay
   (spec §6.4; ruling in DECISIONS D13 — ffmpeg drawtext stays, no Pillow);
   a carousel *is* that file set, and single-image formats export every beat
   so the user picks (per-beat selection policy lands with task 3.4).

Target resolution/container/fps come from the **job plan** — a small
machine-readable record (``plan/<job_id>_plan.json``) the options step writes
when the job is created, so assembly survives server restarts (§6.6) without
in-memory wizard state. A job created outside the flow has no plan file;
assembly then falls back to the visual assets' own dimensions and the job id
for deliverable names. Deliverable names embed the format id (one deliverable
per selected platform format, §6.4) — two jobs for the same format replace
its files, which is the intended regeneration semantics.

Beats participate iff their asset is ``done``: a visual job beat whose visual
failed is skipped (its audio stays on disk; retry + re-assemble restores it),
and a beat whose visual succeeded but audio failed renders with the §6.3
silent default. Nothing completed → :class:`AssemblyError` (loud, never an
empty deliverable).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from app.assembly import AssemblyError
from app.assembly.ffmpeg import FFmpeg
from app.assembly.images import export_image
from app.assembly.video import BeatVisual, stitch_video
from app.core.jobs import Job
from app.scripting.manifest import (
    BeatSpec,
    TimingManifest,
    build_manifest,
    with_probed_durations,
    write_manifest,
)

VISUAL_KINDS = frozenset({"image", "video"})
PLAN_SUFFIX = "_plan.json"
MANIFEST_SUFFIX = "_manifest.json"


# --- job plan (options-step record; keeps assembly restart-safe) ---------------


@dataclass(frozen=True)
class JobPlan:
    """What the options step locked in for one job (mirrors a FormatSpec)."""

    job_id: str
    platform_id: str
    format_id: str
    kind: str  # "video" | "image" | "carousel"
    width: int
    height: int
    container: str  # "mp4" | "jpg" | "png"
    fps: int | float
    overlay_text: str | None = None  # image/carousel only (§6.4); video ignores it


def write_job_plan(
    project_dir: Path,
    *,
    job_id: str,
    platform_id: str,
    format_id: str,
    kind: str,
    width: int,
    height: int,
    container: str,
    fps: int | float,
    overlay_text: str | None = None,
) -> Path:
    """Write ``plan/<job_id>_plan.json``; returns the path."""
    plan = JobPlan(
        job_id=job_id,
        platform_id=platform_id,
        format_id=format_id,
        kind=kind,
        width=int(width),
        height=int(height),
        container=container,
        fps=fps,
        overlay_text=overlay_text,
    )
    path = _plan_path(project_dir, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "job_id": plan.job_id,
        "platform_id": plan.platform_id,
        "format_id": plan.format_id,
        "kind": plan.kind,
        "width": plan.width,
        "height": plan.height,
        "container": plan.container,
        "fps": plan.fps,
        "overlay_text": plan.overlay_text,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def read_job_plan(project_dir: Path, job_id: str) -> JobPlan | None:
    """The job's plan record, or None when the job was created without one."""
    path = _plan_path(project_dir, job_id)
    if not path.is_file():
        return None
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise AssemblyError(f"job plan {path.name} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AssemblyError(f"job plan {path.name} must be a JSON object")
    try:
        raw_overlay = data.get("overlay_text")  # pre-3.2 plan files omit the key
        overlay = raw_overlay if isinstance(raw_overlay, str) and raw_overlay.strip() else None
        return JobPlan(
            job_id=str(data["job_id"]),
            platform_id=str(data["platform_id"]),
            format_id=str(data["format_id"]),
            kind=str(data["kind"]),
            width=int(data["width"]),
            height=int(data["height"]),
            container=str(data["container"]),
            fps=data["fps"],
            overlay_text=overlay,
        )
    except KeyError as exc:
        raise AssemblyError(f"job plan {path.name} is missing key {exc.args[0]!r}") from exc
    except (TypeError, ValueError) as exc:
        raise AssemblyError(f"job plan {path.name} has an invalid value: {exc}") from exc


def _plan_path(project_dir: Path, job_id: str) -> Path:
    return Path(project_dir) / "plan" / f"{job_id}{PLAN_SUFFIX}"


# --- deliverable listing (preview/export UI reads this) -------------------------


_MEDIA_TYPES: dict[str, str] = {
    ".mp4": "video/mp4",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".json": "application/json",
    ".txt": "text/plain",
}


@dataclass(frozen=True)
class Deliverable:
    """One file in ``output/`` that belongs to the job's platform format."""

    name: str
    path: Path
    size_bytes: int
    media_type: str
    kind: str  # "video" | "image" | "audio" | "file"


def media_type_for(filename: str) -> str:
    return _MEDIA_TYPES.get(Path(filename).suffix.lower(), "application/octet-stream")


def list_deliverables(project_dir: Path, job: Job) -> tuple[Deliverable, ...]:
    """The files in ``output/`` attributable to this job's format (or job id).

    Names are ``<format_id>.mp4`` / ``<format_id>_beat_NN.<ext>`` (plan file)
    or ``<job_id>…`` (fallback), so the prefix match is exact — unrelated
    files in ``output/`` are never shown as this job's deliverables.
    """
    out_dir = Path(project_dir) / "output"
    if not out_dir.is_dir():
        return ()
    plan = read_job_plan(project_dir, job.id)
    base = plan.format_id if plan else job.id
    pattern = re.compile(rf"^{re.escape(base)}(?:_beat_\d+)?\.(?P<ext>[A-Za-z0-9]+)$")
    items: list[Deliverable] = []
    for file in sorted(out_dir.iterdir(), key=lambda p: p.name):
        if not file.is_file() or not pattern.match(file.name):
            continue
        media_type = media_type_for(file.name)
        top = media_type.split("/")[0]
        kind = top if top in ("video", "image", "audio") else "file"
        items.append(
            Deliverable(
                name=file.name,
                path=file,
                size_bytes=file.stat().st_size,
                media_type=media_type,
                kind=kind,
            )
        )
    return tuple(items)


# --- assembly ---------------------------------------------------------------------


@dataclass(frozen=True)
class AssemblyResult:
    """What one assembly run produced (deliverables + the manifest of record)."""

    deliverables: tuple[Path, ...]
    manifest_path: Path


def assemble_job(project_dir: Path, job: Job, *, ffmpeg: FFmpeg | None = None) -> AssemblyResult:
    """Build the job's deliverables from its completed assets; see module docstring.

    Raises :class:`~app.assembly.AssemblyError` when there is nothing to
    assemble, an asset marked done is missing on disk, or a local encode
    fails — the message names the beat/file involved.
    """
    runner = ffmpeg if ffmpeg is not None else FFmpeg()
    project_dir = Path(project_dir)
    done_tts = {a.beat_index: a for a in job.assets if a.kind == "tts" and a.status == "done"}
    done_visual = {
        a.beat_index: a for a in job.assets if a.kind in VISUAL_KINDS and a.status == "done"
    }
    visual_job = any(a.kind in VISUAL_KINDS for a in job.assets)
    beat_indexes = sorted(done_visual) if visual_job else sorted(done_tts)
    if not beat_indexes:
        raise AssemblyError(
            "nothing to assemble: the job has no completed assets"
            " — retry failed assets or regenerate them first"
        )

    specs: list[BeatSpec] = []
    visuals: dict[int, BeatVisual] = {}
    # The §6.2 schema numbers manifest beats 1..n contiguously, so the stitch
    # inputs are keyed by manifest position; a partial job's retained beats are
    # renumbered (audio_file/prompt keep the provenance to the original beat).
    # audio_file carries the ABSOLUTE asset path here — probing needs a
    # resolvable file — and is relativized for the manifest below.
    for position, index in enumerate(beat_indexes, 1):
        text = ""
        audio_file: Path | None = None
        tts = done_tts.get(index)
        if tts is not None:
            audio_path = Path(tts.out_path)
            _require_on_disk(index, audio_path, "audio")
            text = str(tts.params.get("text") or "").strip()
            if not text:
                raise AssemblyError(f"beat {index}: tts asset has no text to record")
            audio_file = audio_path.resolve()
        visual = done_visual.get(index)
        if visual is not None:
            visual_path = Path(visual.out_path)
            _require_on_disk(index, visual_path, "visual")
            text = text or str(visual.params.get("prompt") or "").strip()
            if not text:
                raise AssemblyError(f"beat {index}: visual asset has no prompt to record")
            visuals[position] = BeatVisual(index=position, file=visual_path, kind=visual.kind)
        specs.append(BeatSpec(text=text, label=None, audio_file=audio_file))

    # Durations enter the manifest ffprobed from the files on disk — never a
    # provider-reported value (BUILD_PLAN §1.2 "Durations", task 2.4);
    # with_probed_durations is the single probe seam. The §6.2 audio_file is
    # project-relative with forward slashes; audio outside the project stays
    # absolute so the manifest still resolves it.
    probed = with_probed_durations(specs, probe=runner.probe_duration)
    manifest = build_manifest(
        job.id,
        [
            replace(
                spec,
                audio_file=(
                    _project_relative(project_dir, Path(spec.audio_file))
                    if spec.audio_file is not None
                    else None
                ),
            )
            for spec in probed
        ],
    )
    manifest_path = write_manifest(manifest, project_dir / "plan" / f"{job.id}{MANIFEST_SUFFIX}")
    deliverables: tuple[Path, ...] = ()
    if visuals:
        deliverables = tuple(
            _build_deliverables(project_dir, job, manifest, visuals, done_visual, runner)
        )
    return AssemblyResult(deliverables=deliverables, manifest_path=manifest_path)


def _require_on_disk(beat_index: int, path: Path, what: str) -> None:
    if not path.is_file():
        raise AssemblyError(
            f"beat {beat_index}: {what} file {path.name} is missing on disk — regenerate the asset"
        )


def _project_relative(project_dir: Path, path: Path) -> str:
    """Manifest audio_file: project-relative forward slashes when possible (§6.2)."""
    try:
        return path.resolve().relative_to(project_dir.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()  # outside the project — absolute still resolves


def _build_deliverables(
    project_dir: Path,
    job: Job,
    manifest: TimingManifest,
    visuals: dict[int, BeatVisual],
    done_visual: dict[int, Any],
    runner: FFmpeg,
) -> list[Path]:
    plan = read_job_plan(project_dir, job.id)
    out_dir = project_dir / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    base = plan.format_id if plan else job.id
    if plan is None:
        # Engine-direct job: no format was recorded, so assemble the richer
        # deliverable — a video from the beat visuals (§6.3 "video composed
        # from generated images"); dimensions prefer the assets' own params.
        width, height = _dims_from_assets(done_visual, runner)
        out = out_dir / f"{base}.mp4"
        stitch_video(
            manifest,
            visuals,
            project_root=project_dir,
            out_path=out,
            target_width=width,
            target_height=height,
            ffmpeg=runner,
            scratch_dir=project_dir / "state" / "scratch" / job.id,
        )
        return [out]

    if plan.kind == "video":
        out = out_dir / f"{base}.mp4"
        stitch_video(
            manifest,
            visuals,
            project_root=project_dir,
            out_path=out,
            target_width=plan.width,
            target_height=plan.height,
            ffmpeg=runner,
            scratch_dir=project_dir / "state" / "scratch" / job.id,
        )
        return [out]
    extension = "jpg" if plan.container in ("jpg", "jpeg") else "png"
    outs = []
    for index in sorted(visuals):
        out = out_dir / f"{base}_beat_{index:02d}.{extension}"
        export_image(
            visuals[index].file,
            out,
            width=plan.width,
            height=plan.height,
            container=extension,
            overlay_text=plan.overlay_text,
            ffmpeg=runner,
        )
        outs.append(out)
    return outs


def _dims_from_assets(done_visual: dict[int, Any], runner: FFmpeg) -> tuple[int, int]:
    """Target dimensions for a plan-less job: asset params (the request) first,
    the probed file dimensions as fallback."""
    for asset in done_visual.values():
        width, height = asset.params.get("width"), asset.params.get("height")
        if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
            return width, height
    for asset in done_visual.values():
        probe = runner.probe(Path(asset.out_path))
        stream = next((s for s in probe.get("streams", []) if s.get("width")), None)
        if stream is not None:
            return int(stream["width"]), int(stream["height"])
    raise AssemblyError("cannot determine target dimensions from the job's visuals")
