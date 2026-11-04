"""Manifest-driven video stitch: H.264/AAC MP4 with normalization (BUILD_PLAN 1.12).

The timing manifest (§6.2) is the **single source of truth** for assembly
(§6.4): every beat renders exactly its recorded ``duration_s`` — silent beats
included — in manifest order, and nothing else influences the timeline.

How a beat is rendered (each beat → one normalized intermediate segment):
- **Visual** — an image is looped for the beat duration (``-loop 1``), a video
  clip is loop-padded (``-stream_loop -1``) and the output is cut with ``-t``
  to the manifest duration, so clips shorter than their beat still fill it and
  longer inputs are trimmed. Both are normalized to the target resolution with
  ``scale=…:force_original_aspect_ratio=decrease, pad=…`` (aspect preserved,
  exact platform dimensions, black letterbox — never a crop), forced to the
  manifest ``fps_target`` and square pixels (``setsar=1``, ``yuv420p``).
- **Audio** — a beat with audio gets its file resampled to a uniform 44.1 kHz
  stereo stream and padded to the beat duration; a silent beat gets an
  ``anullsrc`` stream of the same duration, so every segment has identical
  stream layout and the final ``concat`` filter always applies.

The segments are concatenated in one pass and encoded **H.264 + AAC MP4** with
``+faststart``. After encoding, the probed duration is checked against the
manifest total: frame quantization allows up to ~1 frame of drift per beat,
anything beyond that means assembly violated the manifest and is an
:class:`~app.assembly.AssemblyError` — the deliverable is never silently
off-timing.

All ffmpeg/ffprobe work goes through :class:`app.assembly.ffmpeg.FFmpeg`
(absolute Windows-safe paths, ``-nostdin``, loud failures). Any problem —
missing visual/audio file, failed encode, timing deviation — raises
:class:`~app.assembly.AssemblyError` naming the beat involved.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.assembly import AssemblyError
from app.assembly.ffmpeg import FFmpeg, FFmpegError
from app.scripting.manifest import TimingManifest

_AUDIO_RATE_HZ = 44_100
_VIDEO_CRF = "20"
_VIDEO_PRESET = "veryfast"
_AUDIO_BITRATE = "160k"
# Manifest durations are ms-rounded; ffmpeg commands use the same precision.
_DURATION_DECIMALS = 3
# Duration guard: ≤ 1 frame of quantization drift per beat plus encoder slack.
_DURATION_GUARD_SLACK_S = 0.05


class _Runner(Protocol):
    """The slice of :class:`FFmpeg` the stitcher needs (duck-typed for tests)."""

    def run(self, args: list[str | Path]) -> object: ...

    def probe_duration(self, path: str | Path) -> float: ...


@dataclass(frozen=True)
class BeatVisual:
    """One beat's visual input: a generated image or a generated video clip."""

    index: int
    file: Path
    kind: str  # "image" | "video"


def stitch_video(
    manifest: TimingManifest,
    visuals: dict[int, BeatVisual],
    *,
    project_root: Path,
    out_path: Path,
    target_width: int,
    target_height: int,
    ffmpeg: _Runner | None = None,
    scratch_dir: Path | None = None,
) -> Path:
    """Render the manifest to one H.264/AAC MP4 at the target resolution; returns it.

    ``visuals`` must cover every manifest beat index (a beat without a visual
    cannot be stitched — the pipeline never requests that). ``project_root``
    resolves the manifest's project-relative ``audio_file`` paths (§6.2).
    Intermediates live under ``scratch_dir`` (default: next to the output) and
    are removed afterwards, whatever the outcome. Raises
    :class:`~app.assembly.AssemblyError` when a beat has no visual, a file is
    missing, an encode fails, or the result deviates from the manifest.
    """
    runner: _Runner = ffmpeg if ffmpeg is not None else FFmpeg()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    scratch = Path(scratch_dir) if scratch_dir is not None else (
        out_path.parent / f".{out_path.stem}.stitch_scratch"
    )
    scratch.mkdir(parents=True, exist_ok=True)
    segments: list[Path] = []
    try:
        for beat in manifest.beats:
            visual = visuals.get(beat.index)
            if visual is None:
                raise AssemblyError(
                    f"beat {beat.index} has no visual to stitch — every manifest"
                    " beat needs its image or clip (regenerate the asset)"
                )
            segment = scratch / f"beat_{beat.index:03d}.mp4"
            _render_segment(
                runner, beat.index, beat.duration_s, beat.audio_file, visual,
                project_root=Path(project_root), out=segment,
                width=target_width, height=target_height, fps=manifest.fps_target,
            )
            segments.append(segment)
        _concat(runner, segments, out_path)
        _verify_duration(runner, out_path, manifest)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return out_path


def _run_or_assembly_error(runner: _Runner, args: list[str | Path], what: str) -> None:
    """One ffmpeg invocation, with failures surfaced as AssemblyError (the
    stitcher's documented failure type — callers and the HTTP layer map it to
    a user-visible outcome, not a bare 500)."""
    try:
        runner.run(args)
    except FFmpegError as exc:
        raise AssemblyError(f"{what}: {exc}") from exc


def _render_segment(
    runner: _Runner,
    beat_index: int,
    duration_s: float,
    audio_file: str | None,
    visual: BeatVisual,
    *,
    project_root: Path,
    out: Path,
    width: int,
    height: int,
    fps: int | float,
) -> None:
    if not visual.file.is_file():
        raise AssemblyError(
            f"beat {beat_index}: visual file {visual.file.name} is missing on disk"
            " — regenerate the asset"
        )
    if visual.kind == "image":
        # Loop the still for the beat duration, then cut to it (frame-exact).
        visual_input = ["-loop", "1", "-framerate", f"{fps}", "-t", f"{duration_s:.3f}",
                        "-i", str(visual.file)]
    elif visual.kind == "video":
        # Loop-pads clips shorter than the beat and gets trimmed by output -t.
        visual_input = ["-stream_loop", "-1", "-i", str(visual.file)]
    else:
        raise AssemblyError(
            f"beat {beat_index}: unknown visual kind {visual.kind!r} (image|video)"
        )
    if audio_file is not None:
        audio_path = project_root / audio_file
        if not audio_path.is_file():
            raise AssemblyError(
                f"beat {beat_index}: audio file {Path(audio_file).name} is missing on"
                " disk — regenerate the asset"
            )
        audio_input = ["-i", str(audio_path)]
        audio_chain = (
            f"[1:a]aresample={_AUDIO_RATE_HZ},"
            f"aformat=sample_fmts=fltp:sample_rates={_AUDIO_RATE_HZ}"
            f":channel_layouts=stereo,apad=whole_dur={duration_s:.3f}[a]"
        )
    else:  # silent beat — uniform silent stream keeps the concat filter simple
        audio_input = [
            "-f", "lavfi",
            "-i", f"anullsrc=channel_layout=stereo:sample_rate={_AUDIO_RATE_HZ}",
        ]
        audio_chain = f"[1:a]atrim=end={duration_s:.3f},asetpts=PTS-STARTPTS[a]"
    video_chain = (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"fps={fps},setsar=1,format=yuv420p[v]"
    )
    _run_or_assembly_error(runner, [
        *visual_input,
        *audio_input,
        "-filter_complex", f"{video_chain};{audio_chain}",
        "-map", "[v]", "-map", "[a]",
        "-t", f"{duration_s:.3f}",
        *(_encoder_args()),
        "-y", out,
    ], f"beat {beat_index}: segment encode failed")


def _concat(runner: _Runner, segments: list[Path], out_path: Path) -> None:
    inputs: list[str | Path] = []
    streams = ""
    for position, segment in enumerate(segments):
        inputs += ["-i", segment]
        streams += f"[{position}:v][{position}:a]"
    _run_or_assembly_error(runner, [
        *inputs,
        "-filter_complex", f"{streams}concat=n={len(segments)}:v=1:a=1[v][a]",
        "-map", "[v]", "-map", "[a]",
        "-movflags", "+faststart",
        *(_encoder_args()),
        "-y", out_path,
    ], "concat encode failed")


def _encoder_args() -> list[str]:
    # Identical for segments and the concat pass, so every stream is uniform.
    return [
        "-c:v", "libx264", "-preset", _VIDEO_PRESET, "-crf", _VIDEO_CRF,
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", _AUDIO_BITRATE,
        "-ar", str(_AUDIO_RATE_HZ), "-ac", "2",
    ]


def _verify_duration(runner: _Runner, out_path: Path, manifest: TimingManifest) -> None:
    probed = runner.probe_duration(out_path)
    fps = float(manifest.fps_target)
    tolerance = len(manifest.beats) / fps + _DURATION_GUARD_SLACK_S
    if abs(probed - manifest.total_duration_s) > tolerance:
        raise AssemblyError(
            f"assembled video lasts {probed:.3f}s but the manifest total is"
            f" {manifest.total_duration_s:.3f}s — assembly deviated from the"
            " timing manifest (the single source of truth); refusing to deliver it"
        )
