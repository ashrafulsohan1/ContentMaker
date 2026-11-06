"""Manifest-driven video stitch: H.264/AAC MP4 with normalization (BUILD_PLAN 1.12).

The timing manifest (§6.2) is the **single source of truth** for assembly
(§6.4): beat audio always plays exactly in its recorded ``duration_s`` slot —
silent beats included — in manifest order, and nothing else influences the
timeline.

Two composition modes, chosen from the visuals on disk (§6.3):

**Ken Burns mode — every beat's visual is a generated still** (the §6.3
"video composed from generated images with motion (pan/zoom) and transitions"
fallback: a video deliverable without a video-capable key):

- **Motion** — each still renders as a *motion segment* via ffmpeg
  ``zoompan``: deterministic pan/zoom patterns cycle per beat (zoom-in,
  zoom-out, pan left→right, pan right→left), so restarts reproduce the same
  video. The still is pre-normalized to the target aspect (scale + pad, never
  a crop) at a 2× intermediate to keep zoompan's integer stepping smooth, and
  zoompan emits exactly ``duration × fps`` frames at ``s=WxH``.
- **Transitions** — segments are rendered ``transition_s`` longer than their
  beat (motion is continuous, so the padding is just more of the same motion)
  and joined with a chained ``xfade`` whose every crossfade is **centered on
  the manifest beat boundary** (offset = boundary − transition/2), then
  trimmed back to the manifest total. The picture blends across boundaries;
  beat boundaries and the total never move.
- **Audio** — built as its own track, one exact-duration piece per beat
  (resampled 44.1 kHz stereo, ``apad``/``atrim`` to the beat duration, or
  ``anullsrc`` for a silent beat) concatenated at the manifest boundaries —
  deliberately independent of the picture crossfade, so narration never
  shifts. Tracks are muxed (video stream-copied, audio AAC) with
  ``+faststart``.

**Cut mode — any beat carries a generated clip** (or a single beat): each
beat renders one normalized A/V segment — a still is looped *with Ken Burns
motion* for the beat duration, a clip is loop-padded (``-stream_loop -1``)
and cut with ``-t`` — and the segments are concatenated with hard cuts
(clips carry their own motion; Veo-style prompts even ask for unbroken
shots). Both modes normalize to the target resolution (aspect preserved,
exact platform dimensions, black letterbox), the manifest ``fps_target`` and
square pixels (``setsar=1``, ``yuv420p``).

Alignment is verified at every stage: **every segment is probed right after
it renders** and must match its expected duration (manifest beat, plus the
transition padding in Ken Burns mode) within ~1 frame — a mis-timed beat
fails immediately, naming the beat, instead of hiding inside the final
total's cumulative tolerance — and the audio track and final file are probed
against the manifest total. Any deviation means assembly violated the
manifest and raises :class:`~app.assembly.AssemblyError` — the deliverable
is never silently off-timing.

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
_AUDIO_FORMAT = f"aformat=sample_fmts=fltp:sample_rates={_AUDIO_RATE_HZ}:channel_layouts=stereo"
_VIDEO_CRF = "20"
_VIDEO_PRESET = "veryfast"
_AUDIO_BITRATE = "160k"
# Manifest durations are ms-rounded; ffmpeg commands use the same precision.
_DURATION_DECIMALS = 3
# Duration guard: ≤ 1 frame of quantization drift per beat plus encoder slack.
_DURATION_GUARD_SLACK_S = 0.05
# Ken Burns (4.2): nominal crossfade length, capped so no beat ever fades
# away entirely (interior beats keep at least half their slot unblended).
_TRANSITION_S = 0.5
_TRANSITION_KIND = "fade"
# zoompan motion: zoom-in/out magnitude (z: 1 → 1+range) and the constant
# mild zoom pans ride on; stills are pre-scaled to an upscale× intermediate
# so zoompan's integer x/y stepping stays sub-pixel smooth (anti-jitter).
_KB_ZOOM_RANGE = 0.25
_KB_PAN_ZOOM = 0.15
_KB_UPSCALE = 2
_KB_PATTERNS = 4  # zoom-in / zoom-out / pan L→R / pan R→L, cycling per beat
_KB_CENTER_X = "iw/2-(iw/zoom/2)"
_KB_CENTER_Y = "ih/2-(ih/zoom/2)"


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
    scratch = (
        Path(scratch_dir)
        if scratch_dir is not None
        else (out_path.parent / f".{out_path.stem}.stitch_scratch")
    )
    scratch.mkdir(parents=True, exist_ok=True)
    stills_only = all(
        visuals.get(beat.index) is not None and visuals[beat.index].kind == "image"
        for beat in manifest.beats
    )
    transition_s = _transition_length(manifest) if stills_only and len(manifest.beats) > 1 else 0.0
    try:
        if transition_s > 0.0:
            _stitch_with_transitions(
                runner,
                manifest,
                visuals,
                project_root=Path(project_root),
                out_path=out_path,
                scratch=scratch,
                width=target_width,
                height=target_height,
                transition_s=transition_s,
            )
        else:
            _stitch_with_cuts(
                runner,
                manifest,
                visuals,
                project_root=Path(project_root),
                out_path=out_path,
                scratch=scratch,
                width=target_width,
                height=target_height,
            )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return out_path


def _transition_length(manifest: TimingManifest) -> float:
    """Crossfade length for this manifest, quantized to the fps grid.

    Capped at half the shortest beat so no beat ever fades away entirely, and
    0.0 (→ cut mode) when no whole-frame transition fits.
    """
    fps = float(manifest.fps_target)
    raw = min(_TRANSITION_S, 0.5 * min(beat.duration_s for beat in manifest.beats))
    quantized = round(raw * fps) / fps
    return quantized if quantized >= 1.0 / fps else 0.0


def _transition_offsets(manifest: TimingManifest, transition_s: float) -> list[float]:
    """One xfade offset per boundary, centered on the manifest boundary."""
    offsets: list[float] = []
    elapsed = 0.0
    for beat in manifest.beats[:-1]:
        elapsed += beat.duration_s
        offsets.append(elapsed - transition_s / 2)
    return offsets


def _stitch_with_transitions(
    runner: _Runner,
    manifest: TimingManifest,
    visuals: dict[int, BeatVisual],
    *,
    project_root: Path,
    out_path: Path,
    scratch: Path,
    width: int,
    height: int,
    transition_s: float,
) -> None:
    """Ken Burns mode (4.2): motion segments → centered crossfades → exact audio track."""
    segments: list[Path] = []
    for beat in manifest.beats:
        visual = _require_visual(beat.index, visuals.get(beat.index))
        segment = scratch / f"beat_{beat.index:03d}.mp4"
        # Motion is continuous, so the transition padding is just more of the
        # same pan/zoom — the crossfade consumes it without moving anything.
        padded = beat.duration_s + transition_s
        _render_motion_segment(
            runner,
            beat.index,
            padded,
            visual,
            out=segment,
            width=width,
            height=height,
            fps=manifest.fps_target,
        )
        _verify_segment(runner, segment, beat.index, padded, manifest.fps_target)
        segments.append(segment)
    motion = scratch / "motion.mp4"
    _compose_transitions(
        runner,
        segments,
        _transition_offsets(manifest, transition_s),
        transition_s,
        total_s=manifest.total_duration_s,
        out=motion,
    )
    _verify_duration(runner, motion, manifest, what="motion track")
    audio_track = scratch / "audio_track.wav"
    _build_audio_track(runner, manifest, project_root, out=audio_track)
    _verify_audio_track(runner, audio_track, manifest)
    _mux_tracks(runner, motion, audio_track, out=out_path)
    _verify_duration(runner, out_path, manifest, what="assembled video")


def _stitch_with_cuts(
    runner: _Runner,
    manifest: TimingManifest,
    visuals: dict[int, BeatVisual],
    *,
    project_root: Path,
    out_path: Path,
    scratch: Path,
    width: int,
    height: int,
) -> None:
    """Cut mode (1.12/3.3): one normalized A/V segment per beat, hard concat."""
    segments: list[Path] = []
    for beat in manifest.beats:
        visual = _require_visual(beat.index, visuals.get(beat.index))
        segment = scratch / f"beat_{beat.index:03d}.mp4"
        _render_segment(
            runner,
            beat.index,
            beat.duration_s,
            beat.audio_file,
            visual,
            project_root=project_root,
            out=segment,
            width=width,
            height=height,
            fps=manifest.fps_target,
        )
        _verify_segment(runner, segment, beat.index, beat.duration_s, manifest.fps_target)
        segments.append(segment)
    _concat(runner, segments, out_path)
    _verify_duration(runner, out_path, manifest, what="assembled video")


def _run_or_assembly_error(runner: _Runner, args: list[str | Path], what: str) -> None:
    """One ffmpeg invocation, with failures surfaced as AssemblyError (the
    stitcher's documented failure type — callers and the HTTP layer map it to
    a user-visible outcome, not a bare 500)."""
    try:
        runner.run(args)
    except FFmpegError as exc:
        raise AssemblyError(f"{what}: {exc}") from exc


def _require_visual(beat_index: int, visual: BeatVisual | None) -> BeatVisual:
    if visual is None:
        raise AssemblyError(
            f"beat {beat_index} has no visual to stitch — every manifest"
            " beat needs its image or clip (regenerate the asset)"
        )
    if not visual.file.is_file():
        raise AssemblyError(
            f"beat {beat_index}: visual file {visual.file.name} is missing on disk"
            " — regenerate the asset"
        )
    if visual.kind not in ("image", "video"):
        raise AssemblyError(f"beat {beat_index}: unknown visual kind {visual.kind!r} (image|video)")
    return visual


def _kb_expressions(pattern: int, frames: int) -> tuple[str, str, str]:
    """``(z, x, y)`` zoompan expressions for one Ken Burns pattern.

    Progress runs 0→1 over the segment's frames via zoompan's ``on`` counter,
    so motion is deterministic per beat position — restarts reproduce the same
    video. Patterns cycle: zoom-in, zoom-out, pan left→right, pan right→left
    (pans ride a mild constant zoom so there is always headroom to move).
    """
    progress = f"min(on/{max(frames - 1, 1)},1)"
    step = pattern % _KB_PATTERNS
    if step == 0:  # zoom in, centered
        return f"1+{_KB_ZOOM_RANGE}*{progress}", _KB_CENTER_X, _KB_CENTER_Y
    if step == 1:  # zoom out, centered
        return f"1+{_KB_ZOOM_RANGE}*(1-{progress})", _KB_CENTER_X, _KB_CENTER_Y
    if step == 2:  # pan left→right
        return f"1+{_KB_PAN_ZOOM}", f"(iw-iw/zoom)*{progress}", _KB_CENTER_Y
    return f"1+{_KB_PAN_ZOOM}", f"(iw-iw/zoom)*(1-{progress})", _KB_CENTER_Y  # pan right→left


def _image_chain(width: int, height: int, fps: int | float, frames: int, pattern: int) -> str:
    """Normalize a still to the target aspect at an upscale× intermediate, then
    animate it with zoompan at exactly ``frames`` frames of ``s=WxH`` output."""
    z, x, y = _kb_expressions(pattern, frames)
    big_w, big_h = width * _KB_UPSCALE, height * _KB_UPSCALE
    return (
        f"scale={big_w}:{big_h}:force_original_aspect_ratio=decrease,"
        f"pad={big_w}:{big_h}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={width}x{height}:fps={fps},"
        "setsar=1,format=yuv420p[v]"
    )


def _clip_chain(width: int, height: int, fps: int | float) -> str:
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"fps={fps},setsar=1,format=yuv420p[v]"
    )


def _render_motion_segment(
    runner: _Runner,
    beat_index: int,
    duration_s: float,
    visual: BeatVisual,
    *,
    out: Path,
    width: int,
    height: int,
    fps: int | float,
) -> None:
    """One still → video-only Ken Burns segment (transitions mode)."""
    frames = max(1, round(duration_s * float(fps)))
    chain = f"[0:v]{_image_chain(width, height, fps, frames, (beat_index - 1) % _KB_PATTERNS)}"
    _run_or_assembly_error(
        runner,
        [
            "-i",
            str(visual.file),
            "-filter_complex",
            chain,
            "-map",
            "[v]",
            "-t",
            f"{duration_s:.3f}",
            *(_encoder_args()),
            "-y",
            out,
        ],
        f"beat {beat_index}: motion segment encode failed",
    )


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
    if visual.kind == "image":
        # Single-frame input; zoompan animates the Ken Burns motion for the
        # beat (still a loop of the still — of its moving rendition).
        visual_input = ["-i", str(visual.file)]
        frames = max(1, round(duration_s * float(fps)))
        video_chain = (
            f"[0:v]{_image_chain(width, height, fps, frames, (beat_index - 1) % _KB_PATTERNS)}"
        )
    elif visual.kind == "video":
        # Loop-pads clips shorter than the beat and gets trimmed by output -t.
        visual_input = ["-stream_loop", "-1", "-i", str(visual.file)]
        video_chain = f"[0:v]{_clip_chain(width, height, fps)}"
    else:
        raise AssemblyError(f"beat {beat_index}: unknown visual kind {visual.kind!r} (image|video)")
    if audio_file is not None:
        audio_path = project_root / audio_file
        if not audio_path.is_file():
            raise AssemblyError(
                f"beat {beat_index}: audio file {Path(audio_file).name} is missing on"
                " disk — regenerate the asset"
            )
        audio_input = ["-i", str(audio_path)]
        audio_chain = (
            f"[1:a]aresample={_AUDIO_RATE_HZ},{_AUDIO_FORMAT},apad=whole_dur={duration_s:.3f}[a]"
        )
    else:  # silent beat — uniform silent stream keeps the concat filter simple
        audio_input = [
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=channel_layout=stereo:sample_rate={_AUDIO_RATE_HZ}",
        ]
        audio_chain = f"[1:a]atrim=end={duration_s:.3f},asetpts=PTS-STARTPTS[a]"
    _run_or_assembly_error(
        runner,
        [
            *visual_input,
            *audio_input,
            "-filter_complex",
            f"{video_chain};{audio_chain}",
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-t",
            f"{duration_s:.3f}",
            *(_encoder_args()),
            "-y",
            out,
        ],
        f"beat {beat_index}: segment encode failed",
    )


def _compose_transitions(
    runner: _Runner,
    segments: list[Path],
    offsets: list[float],
    transition_s: float,
    *,
    total_s: float,
    out: Path,
) -> None:
    """Chain one crossfade per beat boundary (each **centered** on the manifest
    boundary: offset = boundary − transition/2), then trim back to the manifest
    total — the picture blends across boundaries while boundaries and total
    never move (one pass, no generational re-encodes)."""
    inputs: list[str | Path] = []
    parts: list[str] = []
    label = "[0:v]"
    for segment in segments:
        inputs += ["-i", segment]
    for position, offset in enumerate(offsets, 1):
        next_label = f"[x{position}]"
        parts.append(
            f"{label}[{position}:v]xfade=transition={_TRANSITION_KIND}:"
            f"duration={transition_s:.3f}:offset={offset:.3f}{next_label}"
        )
        label = next_label
    parts.append(f"{label}trim=duration={total_s:.3f},setpts=PTS-STARTPTS[v]")
    _run_or_assembly_error(
        runner,
        [
            *inputs,
            "-filter_complex",
            ";".join(parts),
            "-map",
            "[v]",
            *(_encoder_args()),
            "-y",
            out,
        ],
        "transition compose failed",
    )


def _build_audio_track(
    runner: _Runner, manifest: TimingManifest, project_root: Path, *, out: Path
) -> None:
    """Per-beat exact-duration audio pieces concatenated at the manifest
    boundaries — the audio timeline the picture crossfades never touch."""
    inputs: list[str | Path] = []
    chains: list[str] = []
    for position, beat in enumerate(manifest.beats):
        if beat.audio_file is not None:
            audio_path = project_root / beat.audio_file
            if not audio_path.is_file():
                raise AssemblyError(
                    f"beat {beat.index}: audio file {Path(beat.audio_file).name} is"
                    " missing on disk — regenerate the asset"
                )
            inputs += ["-i", str(audio_path)]
            chains.append(
                f"[{position}:a]aresample={_AUDIO_RATE_HZ},{_AUDIO_FORMAT},"
                f"apad=whole_dur={beat.duration_s:.3f},"
                f"atrim=end={beat.duration_s:.3f},asetpts=N/SR/TB[a{position}]"
            )
        else:
            inputs += [
                "-f",
                "lavfi",
                "-i",
                f"anullsrc=channel_layout=stereo:sample_rate={_AUDIO_RATE_HZ}",
            ]
            chains.append(
                f"[{position}:a]{_AUDIO_FORMAT},"
                f"atrim=end={beat.duration_s:.3f},asetpts=N/SR/TB[a{position}]"
            )
    count = len(manifest.beats)
    graph = (
        ";".join(chains)
        + ";"
        + "".join(f"[a{i}]" for i in range(count))
        + f"concat=n={count}:v=0:a=1[a]"
    )
    _run_or_assembly_error(
        runner,
        [
            *inputs,
            "-filter_complex",
            graph,
            "-map",
            "[a]",
            "-c:a",
            "pcm_s16le",
            "-y",
            out,
        ],
        "beat audio track encode failed",
    )


def _mux_tracks(runner: _Runner, motion: Path, audio_track: Path, *, out: Path) -> None:
    """Final H.264/AAC MP4: the encoded motion track is stream-copied (it was
    already encoded by the xfade pass), the PCM audio track gets its single
    AAC generation."""
    _run_or_assembly_error(
        runner,
        [
            "-i",
            motion,
            "-i",
            audio_track,
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            _AUDIO_BITRATE,
            "-ar",
            str(_AUDIO_RATE_HZ),
            "-ac",
            "2",
            "-shortest",
            "-movflags",
            "+faststart",
            "-y",
            out,
        ],
        "final mux failed",
    )


def _verify_segment(
    runner: _Runner, segment: Path, beat_index: int, expected_s: float, fps: int | float
) -> None:
    """Per-beat alignment check (3.3): the freshly rendered segment must last
    exactly its expected duration — the manifest beat, plus the transition
    padding in Ken Burns mode — within ~1 frame + container slack. Catches a
    mis-cut beat at its source — the final total check alone could absorb one
    bad beat into the cumulative ``n/fps`` allowance."""
    probed = runner.probe_duration(segment)
    tolerance = 1.0 / float(fps) + _DURATION_GUARD_SLACK_S
    if abs(probed - expected_s) > tolerance:
        raise AssemblyError(
            f"beat {beat_index}: segment lasts {probed:.3f}s but its manifest slot"
            f" is {expected_s:.3f}s — assembly deviated from the timing manifest"
            " (the single source of truth); regenerate the beat's assets"
        )


def _verify_audio_track(runner: _Runner, track: Path, manifest: TimingManifest) -> None:
    probed = runner.probe_duration(track)
    tolerance = 1.0 / float(manifest.fps_target) + _DURATION_GUARD_SLACK_S
    if abs(probed - manifest.total_duration_s) > tolerance:
        raise AssemblyError(
            f"assembled audio track lasts {probed:.3f}s but the manifest total is"
            f" {manifest.total_duration_s:.3f}s — beat audio would drift against"
            " the timing manifest (the single source of truth); regenerate the assets"
        )


def _concat(runner: _Runner, segments: list[Path], out_path: Path) -> None:
    inputs: list[str | Path] = []
    streams = ""
    for position, segment in enumerate(segments):
        inputs += ["-i", segment]
        streams += f"[{position}:v][{position}:a]"
    _run_or_assembly_error(
        runner,
        [
            *inputs,
            "-filter_complex",
            f"{streams}concat=n={len(segments)}:v=1:a=1[v][a]",
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-movflags",
            "+faststart",
            *(_encoder_args()),
            "-y",
            out_path,
        ],
        "concat encode failed",
    )


def _encoder_args() -> list[str]:
    # Identical for every encode, so every stream is uniform.
    return [
        "-c:v",
        "libx264",
        "-preset",
        _VIDEO_PRESET,
        "-crf",
        _VIDEO_CRF,
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        _AUDIO_BITRATE,
        "-ar",
        str(_AUDIO_RATE_HZ),
        "-ac",
        "2",
    ]


def _verify_duration(
    runner: _Runner, out_path: Path, manifest: TimingManifest, *, what: str
) -> None:
    probed = runner.probe_duration(out_path)
    fps = float(manifest.fps_target)
    tolerance = len(manifest.beats) / fps + _DURATION_GUARD_SLACK_S
    if abs(probed - manifest.total_duration_s) > tolerance:
        raise AssemblyError(
            f"{what} lasts {probed:.3f}s but the manifest total is"
            f" {manifest.total_duration_s:.3f}s — assembly deviated from the"
            " timing manifest (the single source of truth); refusing to deliver it"
        )
