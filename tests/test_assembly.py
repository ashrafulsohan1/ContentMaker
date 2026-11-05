"""Seams: assembly pipeline (BUILD_PLAN 1.12) — real ffmpeg on mock assets.

- ``assembly.video.stitch_video`` — the timing manifest (§6.2) is the single
  source of truth: every beat renders exactly its manifest duration (silent
  beats included), visuals are normalized to the target resolution/fps
  (scale + pad, never crop), short clips are loop-padded, long inputs are
  trimmed, and the concat output is H.264/AAC MP4 whose probed duration stays
  within a frame-per-beat of the manifest total — deviation beyond that is an
  ``AssemblyError``.
- ``assembly.images.export_image`` — platform-correct PNG/JPEG export: scale +
  pad to exact platform dims, container honored (jpg → mjpeg, png → png);
  optional text overlay via ffmpeg drawtext (the 3.2 ruling, DECISIONS D13),
  loud error when a font is required but none exists.
- ``assembly.pipeline`` — job → deliverables orchestration: probed audio
  durations → timing manifest written to ``plan/<job>_manifest.json`` (§6.2/§7),
  video format → one MP4 per format, image/carousel → one export per beat with
  the plan's optional per-image overlay text (§6.4; video ignores it — captions
  are out of scope §9), partial jobs assemble only completed beats, no-plan
  jobs fall back to asset params, nothing-to-assemble fails loudly.
- engine assembly hook — runs before finalize (deliverables exist when the job
  turns terminal), an assembly failure marks the job ``failed``, and
  ``JobEngine.assemble`` re-runs it without touching asset state (the
  regenerate-without-redoing path, §6.6/§8).
"""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from app.assembly import AssemblyError
from app.assembly.ffmpeg import FFmpeg
from app.assembly.images import export_image, find_font
from app.assembly.pipeline import (
    assemble_job,
    list_deliverables,
    read_job_plan,
    write_job_plan,
)
from app.assembly.video import BeatVisual, stitch_video
from app.core.jobs import Asset, AssetSpec, IllegalTransition, Job, JobEngine
from app.core.projects import create_project
from app.engines.base import ImageRequest, ProviderError, TTSRequest, VideoRequest
from app.engines.mocks import MockImageProvider, MockTTSProvider, MockVideoProvider
from app.engines.registry import Registry
from app.scripting.manifest import (
    DEFAULT_SILENT_BEAT_DURATION_S,
    BeatSpec,
    build_manifest,
    read_manifest,
)

MS = 0.05  # concat/frame-quantization slack used across duration assertions


def _silent_wav(path: Path, seconds: float, rate: int = 8000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * round(seconds * rate))


def _png(path: Path, width: int = 64, height: int = 64, seed: int = 7) -> Path:
    MockImageProvider().generate(
        ImageRequest(
            prompt="p", style_hint=None, seed=seed, width=width, height=height, out_path=path
        )
    )
    return path


def _clip(path: Path, seconds: float, width: int = 160, height: int = 120) -> Path:
    MockVideoProvider().generate(
        VideoRequest(prompt="clip", duration_s=seconds, width=width, height=height, out_path=path)
    )
    return path


def _project(tmp_path: Path, name: str = "demo") -> Path:
    return create_project(name, root=tmp_path / "projects").path


def _job(job_id: str, project: str, assets: list[Asset], status: str = "running") -> Job:
    return Job(
        id=job_id,
        project=project,
        status=status,
        created_at="t",
        updated_at="t",
        assets=tuple(assets),
    )


def _asset(
    asset_id: int,
    kind: str,
    beat: int,
    out_path: Path,
    status: str = "done",
    params: dict | None = None,
) -> Asset:
    return Asset(
        id=asset_id,
        job_id="job_x",
        kind=kind,
        beat_index=beat,
        params=params or {},
        out_path=out_path,
        status=status,
    )


def _probe_streams(ff: FFmpeg, path: Path) -> tuple[dict, dict | None, float | None]:
    """(video stream, audio stream | None, duration | None) — images have no duration."""
    data = ff.probe(path)
    video = next((s for s in data["streams"] if s.get("codec_type") == "video"), None)
    audio = next((s for s in data["streams"] if s.get("codec_type") == "audio"), None)
    raw = data["format"].get("duration")
    return video, audio, None if raw is None else float(raw)


# --- assembly.video.stitch_video ------------------------------------------------


class TestStitchVideo:
    def test_stitches_manifest_to_h264_aac_mp4(self, tmp_path: Path):
        root = tmp_path / "proj"
        _silent_wav(root / "audio" / "beat_01.wav", 1.2)
        manifest = build_manifest(
            "job_x",
            [
                BeatSpec(text="one", audio_file="audio/beat_01.wav", duration_s=1.2),
                BeatSpec(text="two"),
            ],
        )
        visuals = {
            1: BeatVisual(1, _png(tmp_path / "v1.png"), "image"),
            2: BeatVisual(2, _png(tmp_path / "v2.png"), "image"),
        }
        out = tmp_path / "out" / "demo.mp4"
        stitch_video(
            manifest, visuals, project_root=root, out_path=out, target_width=320, target_height=180
        )
        video, audio, duration = _probe_streams(FFmpeg(), out)
        assert video["codec_name"] == "h264"
        assert audio["codec_name"] == "aac"
        assert (video["width"], video["height"]) == (320, 180)
        assert abs(duration - manifest.total_duration_s) <= 2 / 30 + MS

    def test_beat_order_and_totals_follow_manifest(self, tmp_path: Path):
        root = tmp_path / "proj"
        manifest = build_manifest(
            "job_x",
            [
                BeatSpec(text="a"),
                BeatSpec(text="b"),
                BeatSpec(text="c"),
            ],
        )
        assert manifest.total_duration_s == pytest.approx(9.0)  # 3 × silent default
        visuals = {i: BeatVisual(i, _png(tmp_path / f"v{i}.png"), "image") for i in (1, 2, 3)}
        out = tmp_path / "out.mp4"
        stitch_video(
            manifest, visuals, project_root=root, out_path=out, target_width=320, target_height=180
        )
        _, _, duration = _probe_streams(FFmpeg(), out)
        assert duration == pytest.approx(9.0, abs=3 / 30 + MS)

    def test_normalizes_offsize_image_and_clip(self, tmp_path: Path):
        root = tmp_path / "proj"
        _silent_wav(root / "audio" / "beat_01.wav", 0.8)
        manifest = build_manifest(
            "job_x",
            [
                BeatSpec(text="one", audio_file="audio/beat_01.wav", duration_s=0.8),
                BeatSpec(text="two"),
            ],
        )
        visuals = {
            1: BeatVisual(1, _png(tmp_path / "tiny.png", 64, 64), "image"),
            2: BeatVisual(2, _clip(tmp_path / "clip.mp4", 0.5), "video"),
        }
        out = tmp_path / "out.mp4"
        stitch_video(
            manifest, visuals, project_root=root, out_path=out, target_width=320, target_height=180
        )
        video, _, duration = _probe_streams(FFmpeg(), out)
        assert (video["width"], video["height"]) == (320, 180)
        assert video["r_frame_rate"] == "30/1"
        assert abs(duration - 3.8) <= 2 / 30 + MS

    def test_short_clip_is_loop_padded_to_manifest_duration(self, tmp_path: Path):
        manifest = build_manifest("job_x", [BeatSpec(text="only")])  # 3.0s
        visuals = {1: BeatVisual(1, _clip(tmp_path / "short.mp4", 0.5), "video")}
        out = tmp_path / "out.mp4"
        stitch_video(
            manifest,
            visuals,
            project_root=tmp_path,
            out_path=out,
            target_width=320,
            target_height=180,
        )
        _, _, duration = _probe_streams(FFmpeg(), out)
        assert duration == pytest.approx(3.0, abs=1 / 30 + MS)

    def test_long_audio_beats_are_trimmed_to_manifest(self, tmp_path: Path):
        root = tmp_path / "proj"
        # audio file is 6s but the manifest says the beat lasts 1.0s
        _silent_wav(root / "audio" / "beat_01.wav", 6.0)
        manifest = build_manifest(
            "job_x", [BeatSpec(text="one", audio_file="audio/beat_01.wav", duration_s=1.0)]
        )
        visuals = {1: BeatVisual(1, _png(tmp_path / "v.png"), "image")}
        out = tmp_path / "out.mp4"
        stitch_video(
            manifest, visuals, project_root=root, out_path=out, target_width=320, target_height=180
        )
        _, _, duration = _probe_streams(FFmpeg(), out)
        assert duration == pytest.approx(1.0, abs=1 / 30 + MS)

    def test_missing_visual_raises_assembly_error(self, tmp_path: Path):
        manifest = build_manifest("job_x", [BeatSpec(text="a"), BeatSpec(text="b")])
        with pytest.raises(AssemblyError, match="beat 2"):
            stitch_video(
                manifest,
                {1: BeatVisual(1, _png(tmp_path / "v.png"), "image")},
                project_root=tmp_path,
                out_path=tmp_path / "o.mp4",
                target_width=64,
                target_height=64,
            )

    def test_missing_audio_file_raises_assembly_error(self, tmp_path: Path):
        manifest = build_manifest(
            "job_x", [BeatSpec(text="a", audio_file="audio/gone.wav", duration_s=1.0)]
        )
        with pytest.raises(AssemblyError, match="gone.wav"):
            stitch_video(
                manifest,
                {1: BeatVisual(1, _png(tmp_path / "v.png"), "image")},
                project_root=tmp_path,
                out_path=tmp_path / "o.mp4",
                target_width=64,
                target_height=64,
            )

    def test_survives_spaces_and_unicode_paths(self, tmp_path: Path):
        out_dir = tmp_path / "final output" / "映像 出力"
        manifest = build_manifest("job_x", [BeatSpec(text="one")])
        out = stitch_video(
            manifest,
            {1: BeatVisual(1, _png(tmp_path / "v.png"), "image")},
            project_root=tmp_path,
            out_path=out_dir / "成片 final.mp4",
            target_width=320,
            target_height=180,
        )
        assert out.is_file() and out.stat().st_size > 0

    def test_scratch_intermediates_are_cleaned(self, tmp_path: Path):
        scratch = tmp_path / "state" / "scratch" / "job_x"
        manifest = build_manifest("job_x", [BeatSpec(text="a"), BeatSpec(text="b")])
        visuals = {
            1: BeatVisual(1, _png(tmp_path / "v1.png"), "image"),
            2: BeatVisual(2, _png(tmp_path / "v2.png"), "image"),
        }
        stitch_video(
            manifest,
            visuals,
            project_root=tmp_path,
            out_path=tmp_path / "out.mp4",
            target_width=64,
            target_height=64,
            scratch_dir=scratch,
        )
        assert not scratch.exists() or not any(scratch.iterdir())

    def test_duration_deviation_beyond_frame_slack_fails(self, tmp_path: Path):
        class LyingProbe:
            """Duck-typed FFmpeg: segments probe on-time, the final total lies."""

            def run(self, args):
                out = Path(str(args[-1]))
                out.write_bytes(b"\x00\x00\x00\x18ftypisom")
                return None

            def probe_duration(self, path):
                # beat_001.mp4 (per-beat check) sees its true 3.0s; only the
                # final concat output lies — that path is what this test covers.
                return 3.0 if Path(str(path)).name == "beat_001.mp4" else 99.0

        manifest = build_manifest("job_x", [BeatSpec(text="a")])
        with pytest.raises(AssemblyError, match="manifest"):
            stitch_video(
                manifest,
                {1: BeatVisual(1, _png(tmp_path / "v.png"), "image")},
                project_root=tmp_path,
                out_path=tmp_path / "o.mp4",
                target_width=64,
                target_height=64,
                ffmpeg=LyingProbe(),
            )  # type: ignore[arg-type]

    def test_failed_encode_surfaces_as_assembly_error(self, tmp_path: Path):
        from app.assembly.ffmpeg import FFmpegError

        class FailingEncoder:
            """Duck-typed FFmpeg whose every encode fails like the wrapper does."""

            def run(self, args):
                raise FFmpegError(
                    "ffmpeg failed (exit 1): bad filter", returncode=1, stderr_tail="bad filter"
                )

            def probe_duration(self, path):
                return 0.0

        manifest = build_manifest("job_x", [BeatSpec(text="a")])
        with pytest.raises(AssemblyError, match="segment encode failed"):
            stitch_video(
                manifest,
                {1: BeatVisual(1, _png(tmp_path / "v.png"), "image")},
                project_root=tmp_path,
                out_path=tmp_path / "o.mp4",
                target_width=64,
                target_height=64,
                ffmpeg=FailingEncoder(),
            )  # type: ignore[arg-type]


# --- assembly.video.stitch_video — 3.3 hardening (real assets, exact alignment) ---


class TestStitchVideoHardening:
    """Task 3.3: assembly hardened for real provider assets.

    The 1.12 stitch already normalizes fps/resolution/aspect; hardening pins
    what real Gemini assets exercise: every segment must render **exactly**
    its manifest beat (verified per beat, not only in total), 24 kHz mono TTS
    WAVs land as uniform 44.1 kHz stereo AAC, and platform-dim outputs carry
    the manifest frame rate with square pixels.
    """

    def test_segment_deviating_from_manifest_fails_naming_the_beat(self, tmp_path: Path):
        class LyingSegment:
            """Segments encode 'fine', but beat 2's segment probes off-timing."""

            def run(self, args):
                Path(str(args[-1])).write_bytes(b"\x00\x00\x00\x18ftypisom")
                return None

            def probe_duration(self, path):
                name = Path(str(path)).name
                if name == "beat_002.mp4":
                    return 1.5  # off-manifest: beat 2 is 1.0s in the manifest
                if name == "final.mp4":
                    return 2.0  # the total is right — only the per-beat check catches it
                return 1.0

        root = tmp_path / "proj"
        _silent_wav(root / "audio" / "beat_01.wav", 1.0)
        _silent_wav(root / "audio" / "beat_02.wav", 1.0)
        manifest = build_manifest(
            "job_x",
            [
                BeatSpec(text="one", audio_file="audio/beat_01.wav", duration_s=1.0),
                BeatSpec(text="two", audio_file="audio/beat_02.wav", duration_s=1.0),
            ],
        )
        visuals = {
            1: BeatVisual(1, _png(tmp_path / "v1.png"), "image"),
            2: BeatVisual(2, _png(tmp_path / "v2.png"), "image"),
        }
        with pytest.raises(AssemblyError, match=r"beat 2.*manifest"):
            stitch_video(
                manifest,
                visuals,
                project_root=root,
                out_path=tmp_path / "final.mp4",
                target_width=64,
                target_height=64,
                ffmpeg=LyingSegment(),
            )  # type: ignore[arg-type]

    def test_gemini_tts_shape_and_platform_dims_normalize(self, tmp_path: Path):
        """Real-asset shapes: 24 kHz mono TTS WAV + off-aspect image → exact
        platform portrait MP4 (h264/aac, 44.1 kHz stereo, 30 fps, square pixels)."""
        root = tmp_path / "proj"
        _silent_wav(root / "audio" / "beat_01.wav", 0.9, rate=24_000)  # Gemini TTS shape
        manifest = build_manifest(
            "job_x",
            [BeatSpec(text="one", audio_file="audio/beat_01.wav", duration_s=0.9)],
        )
        visuals = {1: BeatVisual(1, _png(tmp_path / "v.png", 1344, 768), "image")}
        out = tmp_path / "out.mp4"
        stitch_video(
            manifest,
            visuals,
            project_root=root,
            out_path=out,
            target_width=1080,
            target_height=1920,  # tiktok/reels/shorts portrait platform dims
        )
        video, audio, duration = _probe_streams(FFmpeg(), out)
        assert video["codec_name"] == "h264"
        assert (video["width"], video["height"]) == (1080, 1920)
        assert video["avg_frame_rate"] == "30/1"
        assert video["pix_fmt"] == "yuv420p"
        assert video["sample_aspect_ratio"] == "1:1"
        assert audio["codec_name"] == "aac"
        assert audio["sample_rate"] == "44100"
        assert audio["channels"] == 2
        assert duration == pytest.approx(0.9, abs=1 / 30 + MS)

    def test_jpeg_visual_stitches(self, tmp_path: Path):
        """A JPEG visual (image providers may return JPEG) demuxes and stitches."""
        jpg = export_image(
            _png(tmp_path / "src.png", 128, 96),
            tmp_path / "v.jpg",
            width=320,
            height=180,
            container="jpg",
        )
        manifest = build_manifest("job_x", [BeatSpec(text="a")])  # silent default 3.0s
        out = stitch_video(
            manifest,
            {1: BeatVisual(1, jpg, "image")},
            project_root=tmp_path,
            out_path=tmp_path / "out.mp4",
            target_width=320,
            target_height=180,
        )
        _, _, duration = _probe_streams(FFmpeg(), out)
        assert out.is_file() and out.stat().st_size > 0
        assert duration == pytest.approx(3.0, abs=1 / 30 + MS)


# --- assembly.images.export_image -------------------------------------------------


class TestExportImage:
    def test_exports_platform_jpeg(self, tmp_path: Path):
        out = tmp_path / "out" / "pin_beat_01.jpg"
        export_image(
            _png(tmp_path / "src.png", 64, 64), out, width=1000, height=1500, container="jpg"
        )
        video, _, _ = _probe_streams(FFmpeg(), out)
        assert video["codec_name"] == "mjpeg"
        assert (video["width"], video["height"]) == (1000, 1500)

    def test_exports_png_container(self, tmp_path: Path):
        out = tmp_path / "out" / "beat_01.png"
        export_image(
            _png(tmp_path / "src.jpg", 64, 64), out, width=640, height=360, container="png"
        )
        video, _, _ = _probe_streams(FFmpeg(), out)
        assert video["codec_name"] == "png"
        assert (video["width"], video["height"]) == (640, 360)

    def test_unknown_container_raises(self, tmp_path: Path):
        with pytest.raises(AssemblyError, match="container"):
            export_image(
                _png(tmp_path / "src.png"), tmp_path / "o.gif", width=64, height=64, container="gif"
            )

    @pytest.mark.skipif(find_font() is None, reason="no system font for drawtext")
    def test_text_overlay_changes_the_image(self, tmp_path: Path):
        src = _png(tmp_path / "src.png", 64, 64)
        plain = tmp_path / "plain.jpg"
        labeled = tmp_path / "labeled.jpg"
        export_image(src, plain, width=1000, height=1500, container="jpg")
        export_image(
            src, labeled, width=1000, height=1500, container="jpg", overlay_text="Hook line 標題"
        )
        assert labeled.stat().st_size != plain.stat().st_size
        video, _, _ = _probe_streams(FFmpeg(), labeled)
        assert (video["width"], video["height"]) == (1000, 1500)

    def test_overlay_without_font_raises_loudly(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("app.assembly.images.find_font", lambda: None)
        with pytest.raises(AssemblyError, match="CONTENTMAKER_FONT"):
            export_image(
                _png(tmp_path / "src.png"),
                tmp_path / "o.jpg",
                width=64,
                height=64,
                container="jpg",
                overlay_text="text",
            )
        assert not (tmp_path / "o.jpg").exists()

    def test_failed_encode_surfaces_as_assembly_error(self, tmp_path: Path):
        from app.assembly.ffmpeg import FFmpegError

        class FailingEncoder:
            def run(self, args):
                raise FFmpegError(
                    "ffmpeg failed (exit 1): no such codec",
                    returncode=1,
                    stderr_tail="no such codec",
                )

        with pytest.raises(AssemblyError, match="image export failed"):
            export_image(
                _png(tmp_path / "src.png"),
                tmp_path / "o.jpg",
                width=64,
                height=64,
                container="jpg",
                ffmpeg=FailingEncoder(),
            )  # type: ignore[arg-type]


# --- assembly.pipeline: job plan ---------------------------------------------------


class TestJobPlan:
    def test_write_read_roundtrip(self, tmp_path: Path):
        path = write_job_plan(
            tmp_path,
            job_id="job_x",
            platform_id="youtube",
            format_id="youtube_shorts",
            kind="video",
            width=1080,
            height=1920,
            container="mp4",
            fps=30,
        )
        assert path == tmp_path / "plan" / "job_x_plan.json"
        plan = read_job_plan(tmp_path, "job_x")
        assert plan is not None
        assert (plan.format_id, plan.kind) == ("youtube_shorts", "video")
        assert (plan.width, plan.height) == (1080, 1920)
        assert plan.container == "mp4" and plan.fps == 30 and plan.platform_id == "youtube"

    def test_read_missing_plan_returns_none(self, tmp_path: Path):
        assert read_job_plan(tmp_path, "job_nope") is None

    def test_read_malformed_plan_raises(self, tmp_path: Path):
        (tmp_path / "plan").mkdir()
        (tmp_path / "plan" / "job_x_plan.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(AssemblyError, match="job_x"):
            read_job_plan(tmp_path, "job_x")

    def test_overlay_text_roundtrips_and_defaults_to_none(self, tmp_path: Path):
        path = write_job_plan(
            tmp_path,
            job_id="job_o",
            platform_id="x",
            format_id="x_multi_image",
            kind="carousel",
            width=640,
            height=360,
            container="jpg",
            fps=30,
            overlay_text="Hook line 標題",
        )
        plan = read_job_plan(tmp_path, "job_o")
        assert plan is not None and plan.overlay_text == "Hook line 標題"
        assert "Hook line 標題" in path.read_text(encoding="utf-8")
        write_job_plan(
            tmp_path,
            job_id="job_plain",
            platform_id="x",
            format_id="x_multi_image",
            kind="carousel",
            width=640,
            height=360,
            container="jpg",
            fps=30,
        )
        plain = read_job_plan(tmp_path, "job_plain")
        assert plain is not None and plain.overlay_text is None

    def test_plan_written_before_3_2_reads_without_overlay(self, tmp_path: Path):
        """A pre-3.2 plan file (no overlay_text key) still assembles after upgrade."""
        (tmp_path / "plan").mkdir()
        (tmp_path / "plan" / "job_old_plan.json").write_text(
            '{"job_id": "job_old", "platform_id": "x", "format_id": "x_multi_image",'
            ' "kind": "carousel", "width": 640, "height": 360, "container": "jpg",'
            ' "fps": 30}',
            encoding="utf-8",
        )
        plan = read_job_plan(tmp_path, "job_old")
        assert plan is not None and plan.overlay_text is None


# --- assembly.pipeline: assemble_job -------------------------------------------------


def _video_job_fixture(root: Path, job_id: str = "job_x") -> tuple[Job, Path]:
    """Project with 2 beats: probed wav audio + mock images (platform 320x180)."""
    _silent_wav(root / "audio" / "beat_01.wav", 1.2)
    _silent_wav(root / "audio" / "beat_02.wav", 0.8)
    _png(root / "visuals" / "beat_01.png", 320, 180)
    _png(root / "visuals" / "beat_02.png", 320, 180)
    job = _job(
        job_id,
        root.name,
        [
            _asset(
                1,
                "tts",
                1,
                root / "audio" / "beat_01.wav",
                params={"text": "first beat", "voice": "v"},
            ),
            _asset(
                2,
                "image",
                1,
                root / "visuals" / "beat_01.png",
                params={"prompt": "first visual", "width": 320, "height": 180},
            ),
            _asset(
                3,
                "tts",
                2,
                root / "audio" / "beat_02.wav",
                params={"text": "second beat", "voice": "v"},
            ),
            _asset(
                4,
                "image",
                2,
                root / "visuals" / "beat_02.png",
                params={"prompt": "second visual", "width": 320, "height": 180},
            ),
        ],
    )
    return job, root


class TestAssembleJob:
    def test_video_format_produces_mp4_and_manifest(self, tmp_path: Path):
        root = _project(tmp_path)
        job, root = _video_job_fixture(root)
        write_job_plan(
            root,
            job_id=job.id,
            platform_id="youtube",
            format_id="youtube_shorts",
            kind="video",
            width=320,
            height=180,
            container="mp4",
            fps=30,
        )
        result = assemble_job(root, job)
        assert result.manifest_path == root / "plan" / f"{job.id}_manifest.json"
        manifest = read_manifest(result.manifest_path)  # §6.2 schema holds
        assert [b.index for b in manifest.beats] == [1, 2]
        assert manifest.beats[0].audio_file == "audio/beat_01.wav"
        assert result.deliverables == (root / "output" / "youtube_shorts.mp4",)
        video, audio, duration = _probe_streams(FFmpeg(), result.deliverables[0])
        assert (video["codec_name"], audio["codec_name"]) == ("h264", "aac")
        assert (video["width"], video["height"]) == (320, 180)
        assert abs(duration - manifest.total_duration_s) <= 2 / 30 + MS

    def test_partial_job_assembles_only_completed_beats(self, tmp_path: Path):
        root = _project(tmp_path)
        job, root = _video_job_fixture(root)
        job = _job(
            job.id,
            job.project,
            [
                asset
                if asset.beat_index != 2
                else _asset(
                    asset.id, asset.kind, 2, asset.out_path, status="failed", params=asset.params
                )
                for asset in job.assets
            ],
        )
        result = assemble_job(root, job)
        manifest = read_manifest(result.manifest_path)
        assert [b.index for b in manifest.beats] == [1]
        assert result.deliverables[0].is_file()

    def test_image_format_exports_every_beat(self, tmp_path: Path):
        root = _project(tmp_path)
        job, root = _video_job_fixture(root)
        write_job_plan(
            root,
            job_id=job.id,
            platform_id="x",
            format_id="x_multi_image",
            kind="carousel",
            width=640,
            height=360,
            container="jpg",
            fps=30,
        )
        result = assemble_job(root, job)
        assert [p.name for p in result.deliverables] == [
            "x_multi_image_beat_01.jpg",
            "x_multi_image_beat_02.jpg",
        ]
        video, _, _ = _probe_streams(FFmpeg(), result.deliverables[0])
        assert (video["codec_name"], video["width"], video["height"]) == ("mjpeg", 640, 360)

    @pytest.mark.skipif(find_font() is None, reason="no system font for drawtext")
    def test_image_format_applies_plan_overlay_to_every_beat(self, tmp_path: Path):
        """§6.4 optional per-image text overlay reaches each exported beat."""
        root = _project(tmp_path)
        job, root = _video_job_fixture(root)
        write_job_plan(
            root,
            job_id=job.id,
            platform_id="x",
            format_id="x_multi_image",
            kind="carousel",
            width=640,
            height=360,
            container="jpg",
            fps=30,
            overlay_text="Hook line 標題",
        )
        result = assemble_job(root, job)
        video, _, _ = _probe_streams(FFmpeg(), result.deliverables[0])
        assert (video["width"], video["height"]) == (640, 360)  # dims stay exact
        # Each overlaid export differs from its plain export of the same beat.
        # A distinct format_id keeps the second run off the first run's files
        # (same-format jobs replace each other's files by design).
        write_job_plan(
            root,
            job_id="job_plain",
            platform_id="x",
            format_id="x_plain_image",
            kind="carousel",
            width=640,
            height=360,
            container="jpg",
            fps=30,
        )
        plain = assemble_job(root, _job("job_plain", job.project, job.assets))
        assert [p.name for p in plain.deliverables] == [
            "x_plain_image_beat_01.jpg",
            "x_plain_image_beat_02.jpg",
        ]
        for overlaid, plain_out in zip(result.deliverables, plain.deliverables, strict=True):
            assert overlaid.read_bytes() != plain_out.read_bytes()

    def test_video_format_ignores_plan_overlay(self, tmp_path: Path):
        """Overlay is an image-export option; video stays caption-free (§9)."""
        root = _project(tmp_path)
        job, root = _video_job_fixture(root)
        write_job_plan(
            root,
            job_id=job.id,
            platform_id="youtube",
            format_id="youtube_shorts",
            kind="video",
            width=320,
            height=180,
            container="mp4",
            fps=30,
            overlay_text="Hook line 標題",
        )
        result = assemble_job(root, job)
        assert result.deliverables == (root / "output" / "youtube_shorts.mp4",)
        assert result.deliverables[0].is_file()

    def test_audio_only_job_writes_manifest_without_video(self, tmp_path: Path):
        root = _project(tmp_path)
        _silent_wav(root / "audio" / "beat_01.wav", 1.5)
        job = _job(
            "job_a",
            root.name,
            [
                _asset(
                    1,
                    "tts",
                    1,
                    root / "audio" / "beat_01.wav",
                    params={"text": "spoken", "voice": "v"},
                )
            ],
        )
        result = assemble_job(root, job)
        assert result.deliverables == ()
        manifest = read_manifest(result.manifest_path)
        assert manifest.beats[0].duration_s == 1.5
        assert not (root / "output").exists() or not any((root / "output").iterdir())

    def test_no_done_assets_raises(self, tmp_path: Path):
        root = _project(tmp_path)
        _png(root / "visuals" / "beat_01.png", 32, 32)
        job = _job(
            "job_f",
            root.name,
            [
                _asset(
                    1,
                    "image",
                    1,
                    root / "visuals" / "beat_01.png",
                    status="failed",
                    params={"prompt": "p"},
                )
            ],
        )
        with pytest.raises(AssemblyError, match="no completed assets"):
            assemble_job(root, job)

    def test_without_plan_falls_back_to_job_id_and_asset_dims(self, tmp_path: Path):
        root = _project(tmp_path)
        job, root = _video_job_fixture(root)
        result = assemble_job(root, job)
        assert result.deliverables == (root / "output" / f"{job.id}.mp4",)
        video, _, _ = _probe_streams(FFmpeg(), result.deliverables[0])
        assert (video["width"], video["height"]) == (320, 180)  # from asset params

    def test_visual_beat_without_audio_gets_silent_default(self, tmp_path: Path):
        root = _project(tmp_path)
        _png(root / "visuals" / "beat_01.png", 320, 180)
        job = _job(
            "job_s",
            root.name,
            [
                _asset(
                    1,
                    "image",
                    1,
                    root / "visuals" / "beat_01.png",
                    params={"prompt": "still", "width": 320, "height": 180},
                )
            ],
        )
        result = assemble_job(root, job)
        manifest = read_manifest(result.manifest_path)
        assert manifest.beats[0].duration_s == DEFAULT_SILENT_BEAT_DURATION_S
        assert manifest.beats[0].audio_file is None

    def test_asset_marked_done_but_file_missing_raises(self, tmp_path: Path):
        root = _project(tmp_path)
        _png(root / "visuals" / "beat_01.png", 320, 180)
        job = _job(
            "job_m",
            root.name,
            [
                _asset(
                    1,
                    "tts",
                    1,
                    root / "audio" / "gone.wav",  # never written
                    params={"text": "spoken", "voice": "v"},
                ),
                _asset(
                    2,
                    "image",
                    1,
                    root / "visuals" / "beat_01.png",
                    params={"prompt": "still", "width": 320, "height": 180},
                ),
            ],
        )
        with pytest.raises(AssemblyError, match="gone.wav"):
            assemble_job(root, job)

    def test_deliverables_listing_attributed_to_job(self, tmp_path: Path):
        root = _project(tmp_path)
        job, root = _video_job_fixture(root)
        write_job_plan(
            root,
            job_id=job.id,
            platform_id="youtube",
            format_id="youtube_shorts",
            kind="video",
            width=320,
            height=180,
            container="mp4",
            fps=30,
        )
        assemble_job(root, job)
        (root / "output" / "stranger.mp4").write_bytes(b"not this job's file")
        items = list_deliverables(root, job)
        assert [d.name for d in items] == ["youtube_shorts.mp4"]
        assert items[0].media_type == "video/mp4" and items[0].kind == "video"
        assert items[0].size_bytes > 0

    def test_rerun_on_same_format_replaces_superseded_beat_files(self, tmp_path: Path):
        """A shorter re-run on the same format replaces the format's file set.

        Two jobs for the same format replace its files (regeneration semantics,
        module docstring): the earlier run's higher-numbered beat files must be
        removed, or the deliverables listing — and with it the export ZIP —
        bundles them into the new job's set (e.g. a 5-slide set survives a
        3-slide re-run)."""
        root = _project(tmp_path)

        def beats(n: int) -> list[Asset]:
            assets = []
            for index in range(1, n + 1):
                path = _png(root / "visuals" / f"beat_{index:02d}.png", 320, 180)
                assets.append(
                    _asset(
                        index,
                        "image",
                        index,
                        path,
                        params={"prompt": f"beat {index}", "width": 320, "height": 180},
                    )
                )
            return assets

        plan = {
            "platform_id": "x",
            "format_id": "x_multi_image",
            "kind": "carousel",
            "width": 320,
            "height": 180,
            "container": "jpg",
            "fps": 30,
        }
        write_job_plan(root, job_id="job_long", **plan)
        assemble_job(root, _job("job_long", root.name, beats(3)))
        assert (root / "output" / "x_multi_image_beat_03.jpg").is_file()  # setup sanity

        write_job_plan(root, job_id="job_short", **plan)
        result = assemble_job(root, _job("job_short", root.name, beats(2)))
        assert [p.name for p in result.deliverables] == [
            "x_multi_image_beat_01.jpg",
            "x_multi_image_beat_02.jpg",
        ]
        assert sorted(p.name for p in (root / "output").iterdir()) == [
            "x_multi_image_beat_01.jpg",
            "x_multi_image_beat_02.jpg",
        ]
        listing = _job("job_short", root.name, [])
        assert [d.name for d in list_deliverables(root, listing)] == [
            "x_multi_image_beat_01.jpg",
            "x_multi_image_beat_02.jpg",
        ]


# --- engine assembly hook ------------------------------------------------------------
# Engine-direct (no plan file): exercises the fallback path end to end.


class CountingTTS(MockTTSProvider):
    def __init__(self) -> None:
        self.generate_calls = 0

    def generate(self, request: TTSRequest):
        self.generate_calls += 1
        return super().generate(request)


class FlakyImage(MockImageProvider):
    """Fails the first ``failures`` generate calls, then delegates to the mock."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.generate_calls = 0

    def generate(self, request):
        self.generate_calls += 1
        if self.generate_calls <= self.failures:
            raise ProviderError("mock", "image engine down", retryable=False)
        return super().generate(request)


def _engine_with_assembly(registry: Registry, projects_root: Path) -> JobEngine:
    from app.assembly.pipeline import assemble_job as _assemble

    def assembler(job: Job) -> None:
        _assemble(projects_root / job.project, job)

    return JobEngine(
        registry, projects_root=projects_root, assembler=assembler, backoff_base_s=0.01
    )


class TestEngineAssemblyHook:
    def _two_beat_assets(self, root: Path) -> list[AssetSpec]:
        return [
            AssetSpec(
                kind="tts",
                beat_index=1,
                out_path=root / "audio" / "beat_01.wav",
                params={"text": "hook line", "voice": "v"},
            ),
            AssetSpec(
                kind="image",
                beat_index=1,
                out_path=root / "visuals" / "beat_01.png",
                params={"prompt": "hook visual", "width": 320, "height": 180},
            ),
            AssetSpec(
                kind="tts",
                beat_index=2,
                out_path=root / "audio" / "beat_02.wav",
                params={"text": "closing line", "voice": "v"},
            ),
            AssetSpec(
                kind="image",
                beat_index=2,
                out_path=root / "visuals" / "beat_02.png",
                params={"prompt": "closing visual", "width": 320, "height": 180},
            ),
        ]

    def test_assembler_runs_before_finalize(self, tmp_path: Path):
        root = _project(tmp_path, "wired")
        registry = Registry()
        tts = CountingTTS()
        registry.register("tts", tts)
        registry.register("image", MockImageProvider())
        engine = _engine_with_assembly(registry, tmp_path / "projects")
        try:
            job = engine.create_job("wired", self._two_beat_assets(root))
            engine.start(engine.estimate(job.id).id)
            final = engine.wait(job.id, {"completed"}, timeout=60)
            assert final.status == "completed"
            mp4 = root / "output" / f"{job.id}.mp4"  # no plan file → job-id naming
            assert mp4.is_file()
            manifest = read_manifest(root / "plan" / f"{job.id}_manifest.json")
            assert [b.index for b in manifest.beats] == [1, 2]
        finally:
            engine.stop()

    def test_assembler_failure_marks_job_failed(self, tmp_path: Path):
        root = _project(tmp_path, "boom")
        registry = Registry()
        registry.register("tts", MockTTSProvider())

        def bad_assembler(job: Job) -> None:
            raise RuntimeError("encode exploded")

        engine = JobEngine(
            registry,
            projects_root=tmp_path / "projects",
            assembler=bad_assembler,
            backoff_base_s=0.01,
        )
        try:
            job = engine.create_job(
                "boom",
                [
                    AssetSpec(
                        kind="tts",
                        beat_index=1,
                        out_path=root / "audio" / "beat_01.wav",
                        params={"text": "spoken", "voice": "v"},
                    )
                ],
            )
            engine.start(engine.estimate(job.id).id)
            final = engine.wait(job.id, {"failed"}, timeout=30)
            assert final.status == "failed"
            assert "assembly failed" in (final.error or "")
            assert "encode exploded" in (final.error or "")
        finally:
            engine.stop()

    def test_engine_without_assembler_completes(self, tmp_path: Path):
        root = _project(tmp_path, "plain")
        registry = Registry()
        registry.register("tts", MockTTSProvider())
        engine = JobEngine(registry, projects_root=tmp_path / "projects", backoff_base_s=0.01)
        try:
            job = engine.create_job(
                "plain",
                [
                    AssetSpec(
                        kind="tts",
                        beat_index=1,
                        out_path=root / "audio" / "beat_01.wav",
                        params={"text": "spoken", "voice": "v"},
                    )
                ],
            )
            engine.start(engine.estimate(job.id).id)
            final = engine.wait(job.id, {"completed"}, timeout=30)
            assert final.status == "completed"
        finally:
            engine.stop()

    def test_assemble_method_reruns_after_terminal(self, tmp_path: Path):
        root = _project(tmp_path, "again")
        registry = Registry()
        registry.register("tts", MockTTSProvider())
        calls: list[str] = []

        def recorder(job: Job) -> None:
            calls.append(job.id)

        engine = JobEngine(
            registry, projects_root=tmp_path / "projects", assembler=recorder, backoff_base_s=0.01
        )
        try:
            job = engine.create_job(
                "again",
                [
                    AssetSpec(
                        kind="tts",
                        beat_index=1,
                        out_path=root / "audio" / "beat_01.wav",
                        params={"text": "spoken", "voice": "v"},
                    )
                ],
            )
            engine.start(engine.estimate(job.id).id)
            engine.wait(job.id, {"completed"}, timeout=30)
            assert calls == [job.id]  # finalize hook ran once
            returned = engine.assemble(job.id)
            assert returned.id == job.id
            assert calls == [job.id, job.id]  # manual re-run, assets untouched
        finally:
            engine.stop()

    def test_assemble_method_rejects_non_terminal(self, tmp_path: Path):
        root = _project(tmp_path, "early")
        registry = Registry()
        registry.register("tts", MockTTSProvider())
        engine = JobEngine(
            registry,
            projects_root=tmp_path / "projects",
            assembler=lambda job: None,
            backoff_base_s=0.01,
        )
        try:
            job = engine.create_job(
                "early",
                [
                    AssetSpec(
                        kind="tts",
                        beat_index=1,
                        out_path=root / "audio" / "beat_01.wav",
                        params={"text": "spoken", "voice": "v"},
                    )
                ],
            )
            with pytest.raises(IllegalTransition):
                engine.assemble(job.id)  # still draft — nothing to assemble
        finally:
            engine.stop()

    def test_assemble_method_without_assembler_configured(self, tmp_path: Path):
        registry = Registry()
        registry.register("tts", MockTTSProvider())
        engine = JobEngine(registry, projects_root=tmp_path / "projects", backoff_base_s=0.01)
        try:
            job = engine.create_job("x", [])
            engine.start(engine.estimate(job.id).id)  # zero-asset job → completed
            engine.wait(job.id, {"completed"}, timeout=30)
            with pytest.raises(ValueError, match="no assembler"):
                engine.assemble(job.id)
        finally:
            engine.stop()

    def test_retry_asset_then_reassemble_without_redoing(self, tmp_path: Path):
        """§6.6/§8 pin: regenerate one asset → deliverables refresh, done assets
        are not re-generated."""
        root = _project(tmp_path, "regen")
        registry = Registry()
        tts = CountingTTS()
        image = FlakyImage(failures=1)  # first run: beat-1 image fails for good
        registry.register("tts", tts)
        registry.register("image", image)
        engine = _engine_with_assembly(registry, tmp_path / "projects")
        try:
            job = engine.create_job("regen", self._two_beat_assets(root))
            engine.start(engine.estimate(job.id).id)
            partial = engine.wait(job.id, {"partial"}, timeout=60)
            assert partial.status == "partial"
            first_manifest = read_manifest(root / "plan" / f"{job.id}_manifest.json")
            # §6.2 renumbers retained beats to contiguous 1..n; audio_file
            # carries the provenance that this is the original beat 2.
            assert [b.index for b in first_manifest.beats] == [1]
            assert first_manifest.beats[0].audio_file == "audio/beat_02.wav"
            failed_image = next(
                a for a in partial.assets if a.kind == "image" and a.beat_index == 1
            )
            assert failed_image.status == "failed"
            tts_calls_after_round1 = tts.generate_calls
            image_calls_after_round1 = image.generate_calls

            healthy = FlakyImage(failures=0)  # engine is healthy now
            registry.register("image", healthy)
            engine.retry_asset(job.id, failed_image.id)
            final = engine.wait(job.id, {"completed"}, timeout=60)
            assert final.status == "completed"

            refreshed = read_manifest(root / "plan" / f"{job.id}_manifest.json")
            assert [b.index for b in refreshed.beats] == [1, 2]
            assert [b.audio_file for b in refreshed.beats] == [
                "audio/beat_01.wav",
                "audio/beat_02.wav",
            ]
            assert healthy.generate_calls == 1  # exactly the retried asset ran
            assert tts.generate_calls == tts_calls_after_round1  # audio never redone
            assert image.generate_calls == image_calls_after_round1  # done work untouched
        finally:
            engine.stop()
