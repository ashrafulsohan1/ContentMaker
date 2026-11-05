"""Seam: app.scripting.manifest — timing manifest read/write (§6.2).

Catches: schema drift vs PROMPT §6.2 (exact keys/types on both levels),
silent beats losing their default duration (BUILD_PLAN §10.2), and broken
timelines (non-zero start, gaps/overlaps, total mismatch) — the manifest is
the single source of truth for assembly, so a corrupt one must fail loudly
with a ManifestError naming the problem, never be guessed at.
"""
import json
from pathlib import Path

import pytest

from app.scripting.manifest import (
    DEFAULT_FPS,
    DEFAULT_SILENT_BEAT_DURATION_S,
    BeatSpec,
    ManifestBeat,
    ManifestError,
    build_manifest,
    read_manifest,
    write_manifest,
)


def _valid_data() -> dict:
    """A complete, self-consistent manifest in the exact §6.2 shape."""
    return {
        "job_id": "job_20261104_093012",
        "fps_target": 30,
        "total_duration_s": 7.2,
        "beats": [
            {
                "index": 1,
                "label": "hook",
                "text": "Hook line.",
                "audio_file": "audio/beat_01.mp3",
                "start_s": 0.0,
                "duration_s": 4.2,
            },
            {
                "index": 2,
                "label": None,
                "text": "Silent beat.",
                "audio_file": None,
                "start_s": 4.2,
                "duration_s": 3.0,
            },
        ],
    }


# -- building ------------------------------------------------------------------


def test_silent_beat_gets_default_duration(tmp_path):
    """BUILD_PLAN §10.2: a beat with no audio gets a sensible default duration."""
    manifest = build_manifest(
        "job_x",
        [
            BeatSpec(text="Look at this."),
            BeatSpec(text="Spoken.", audio_file="audio/beat_02.mp3", duration_s=2.5),
        ],
    )
    silent = manifest.beats[0]
    assert silent.audio_file is None
    assert silent.duration_s == DEFAULT_SILENT_BEAT_DURATION_S == 3.0
    # ... it advances the timeline and is recorded in the written manifest
    assert manifest.beats[1].start_s == 3.0
    assert manifest.total_duration_s == 5.5
    path = write_manifest(manifest, tmp_path / "plan" / "manifest.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["beats"][0]["duration_s"] == 3.0
    assert data["beats"][0]["audio_file"] is None


def test_starts_are_sequential_and_total_is_the_sum():
    manifest = build_manifest(
        "job_x",
        [
            BeatSpec(text="a", audio_file="audio/01.mp3", duration_s=4.2),
            BeatSpec(text="b", audio_file="audio/02.mp3", duration_s=9.7),
            BeatSpec(text="c"),  # silent → default
        ],
    )
    assert [b.index for b in manifest.beats] == [1, 2, 3]
    assert [b.start_s for b in manifest.beats] == [0.0, 4.2, 13.9]
    assert manifest.total_duration_s == 16.9


def test_audio_and_duration_must_come_together():
    with pytest.raises(ManifestError, match="together"):
        build_manifest("job_x", [BeatSpec(text="a", audio_file="audio/01.mp3")])
    with pytest.raises(ManifestError, match="together"):
        build_manifest("job_x", [BeatSpec(text="a", duration_s=2.0)])


def test_zero_beats_rejected():
    with pytest.raises(ManifestError, match="zero beats"):
        build_manifest("job_x", [])


def test_audio_file_path_is_recorded_with_forward_slashes(tmp_path):
    """Windows safety: a pathlib audio path must not become a backslash path."""
    manifest = build_manifest(
        "job_x",
        [BeatSpec(text="a", audio_file=Path("audio") / "beat_01.mp3", duration_s=1.0)],
    )
    path = write_manifest(manifest, tmp_path / "manifest.json")
    assert "audio/beat_01.mp3" in path.read_text(encoding="utf-8")
    assert read_manifest(path).beats[0].audio_file == "audio/beat_01.mp3"


# -- exact §6.2 schema ---------------------------------------------------------


def test_written_schema_is_exactly_prompt_6_2(tmp_path):
    manifest = build_manifest(
        "job_x",
        [
            BeatSpec(text="a", label="hook", audio_file="audio/beat_01.mp3", duration_s=4.2),
            BeatSpec(text="b"),
        ],
    )
    path = write_manifest(manifest, tmp_path / "manifest.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) == {"job_id", "fps_target", "total_duration_s", "beats"}
    assert all(
        set(beat) == {"index", "label", "text", "audio_file", "start_s", "duration_s"}
        for beat in data["beats"]
    )
    assert data["fps_target"] == DEFAULT_FPS == 30
    assert isinstance(data["fps_target"], int)  # written as 30, not 30.0


def test_prompt_6_2_example_shape_reads_back(tmp_path):
    """The §6.2 example's two beats (its 41.8 total covers beats elided with '…')."""
    data = _valid_data()
    data["job_id"] = "job_20261104_093012"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    manifest = read_manifest(path)
    assert manifest.job_id == "job_20261104_093012"
    assert manifest.fps_target == 30
    assert manifest.total_duration_s == 7.2
    assert manifest.beats[0] == ManifestBeat(
        index=1,
        label="hook",
        text="Hook line.",
        audio_file="audio/beat_01.mp3",
        start_s=0.0,
        duration_s=4.2,
    )
    assert manifest.beats[1].start_s == 4.2


def test_write_then_read_roundtrips_exactly(tmp_path):
    manifest = build_manifest(
        "job_20261104_093012",
        [
            BeatSpec(
                text="Hook line.", label="hook", audio_file="audio/beat_01.mp3", duration_s=4.2333
            ),
            BeatSpec(text="Silent closer."),
        ],
        fps_target=60,
    )
    path = write_manifest(manifest, tmp_path / "plan" / "manifest.json")
    assert read_manifest(path) == manifest


def test_write_is_utf8_and_creates_missing_parent_dirs(tmp_path):
    manifest = build_manifest(
        "job_x",
        [BeatSpec(text="Café — naïve métier", audio_file="audio/beat_01.mp3", duration_s=2.0)],
    )
    path = write_manifest(manifest, tmp_path / "plan" / "sub" / "manifest.json")
    raw = path.read_text(encoding="utf-8")
    assert "Café — naïve métier" in raw  # real UTF-8, not \uXXXX escapes
    assert read_manifest(path).beats[0].text == "Café — naïve métier"


def test_read_does_not_reassign_silent_default(tmp_path):
    """The default is assigned at build time; read honors what is recorded."""
    data = _valid_data()
    data["beats"][1]["duration_s"] = 5.0
    data["total_duration_s"] = 9.2
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert read_manifest(path).beats[1].duration_s == 5.0


# -- read validation: the source of truth must be exact ------------------------


def _mutate(data: dict, name: str) -> None:
    beat0, beat1 = data["beats"]
    if name == "missing_root_key":
        data.pop("fps_target")
    elif name == "unknown_root_key":
        data["extra"] = 1
    elif name == "missing_beat_key":
        beat0.pop("text")
    elif name == "unknown_beat_key":
        beat0["img_hint"] = "dawn"  # even a plausible extra key is rejected
    elif name == "fps_zero":
        data["fps_target"] = 0
    elif name == "boolean_fps":
        data["fps_target"] = True
    elif name == "blank_job_id":
        data["job_id"] = "   "
    elif name == "duration_zero":
        beat0["duration_s"] = 0
    elif name == "boolean_duration":
        beat0["duration_s"] = True
    elif name == "negative_start":
        beat0["start_s"] = -0.5
    elif name == "index_zero":
        beat0["index"] = 0
    elif name == "duplicate_index":
        beat1["index"] = 1
    elif name == "nonsequential_index":
        beat1["index"] = 3
    elif name == "overlap":
        beat1["start_s"] = 0.0
    elif name == "gap":
        beat1["start_s"] = 5.0
    elif name == "first_start_nonzero":
        beat0["start_s"] = 1.0
    elif name == "total_mismatch":
        data["total_duration_s"] = 99
    elif name == "empty_beats":
        data["beats"] = []
    elif name == "empty_audio_file":
        beat0["audio_file"] = ""
    elif name == "blank_text":
        beat0["text"] = "   "
    elif name == "bad_label":
        beat0["label"] = 42
    elif name == "beat_not_object":
        data["beats"] = ["nope"]
    else:  # pragma: no cover - guards against an untested mutation name
        raise AssertionError(f"unknown mutation {name}")


_MUTATION_NAMES = sorted(
    {
        "missing_root_key",
        "unknown_root_key",
        "missing_beat_key",
        "unknown_beat_key",
        "fps_zero",
        "boolean_fps",
        "blank_job_id",
        "duration_zero",
        "boolean_duration",
        "negative_start",
        "index_zero",
        "duplicate_index",
        "nonsequential_index",
        "overlap",
        "gap",
        "first_start_nonzero",
        "total_mismatch",
        "empty_beats",
        "empty_audio_file",
        "blank_text",
        "bad_label",
        "beat_not_object",
    }
)


@pytest.mark.parametrize("name", _MUTATION_NAMES)
def test_read_rejects_corruption(name, tmp_path):
    data = _valid_data()
    _mutate(data, name)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ManifestError):
        read_manifest(path)


def test_read_rejects_non_object_manifest(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ManifestError, match="JSON object"):
        read_manifest(path)


def test_read_rejects_invalid_json(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ManifestError, match="not valid JSON"):
        read_manifest(path)


# -- with_probed_durations: ffprobe → manifest (BUILD_PLAN 2.4) ------------------
#
# The manifest is the single source of truth for assembly, and its durations
# must be REAL: ffprobe every audio file, never trust a provider-reported
# value (BUILD_PLAN §1.2 "Durations"; risk #4). This seam is the one place
# where audio-file durations enter the manifest.


from app.assembly.ffmpeg import FFmpegError  # noqa: E402
from app.scripting.manifest import with_probed_durations  # noqa: E402


class RecordingProbe:
    """Stands in for ffprobe; hands out canned durations, records the files."""

    def __init__(self, durations: dict[str, float] | float = 2.5) -> None:
        self.durations = durations
        self.probed: list[Path] = []

    def __call__(self, path: Path | str) -> float:
        self.probed.append(Path(path))
        if isinstance(self.durations, dict):
            return self.durations[str(Path(path).name)]
        return float(self.durations)


def _specs() -> list[BeatSpec]:
    return [
        BeatSpec(text="Look at this."),  # silent beat — no audio to probe
        BeatSpec(text="Spoken one.", audio_file="audio/beat_02.wav"),
        BeatSpec(text="Spoken two.", audio_file="audio/beat_03.wav"),
    ]


def test_probed_specs_fill_durations_from_the_probe():
    probe = RecordingProbe({"beat_02.wav": 3.456, "beat_03.wav": 1.2})
    specs = with_probed_durations(_specs(), probe=probe)
    assert [spec.duration_s for spec in specs] == [None, 3.456, 1.2]
    # every audio file was probed, by its own path
    assert [p.name for p in probe.probed] == ["beat_02.wav", "beat_03.wav"]


def test_probed_duration_overrides_any_prefilled_value():
    """The core rule: audio_file present → the probed value wins, whatever a
    provider (or a stale manifest) claimed beforehand (§1.2)."""
    probe = RecordingProbe(4.321)
    specs = with_probed_durations(
        [BeatSpec(text="Spoken.", audio_file="audio/beat_01.wav", duration_s=99.0)],
        probe=probe,
    )
    assert specs[0].duration_s == 4.321


def test_silent_specs_pass_through_untouched():
    probe = RecordingProbe()
    specs = with_probed_durations([BeatSpec(text="Silent.")], probe=probe)
    assert specs[0].duration_s is None
    assert specs[0].audio_file is None
    assert probe.probed == []  # nothing to probe for a silent beat


def test_probed_durations_are_rounded_to_whole_milliseconds():
    probe = RecordingProbe(1.2345678)
    specs = with_probed_durations(
        [BeatSpec(text="Spoken.", audio_file="audio/beat_01.wav")], probe=probe
    )
    assert specs[0].duration_s == 1.235


def test_probe_failure_is_a_manifest_error_naming_the_beat_and_file():
    def failing_probe(path: Path | str) -> float:
        raise FFmpegError(f"ffprobe failed for {path} (exit 1): Invalid data")

    with pytest.raises(ManifestError) as exc_info:
        with_probed_durations(
            [BeatSpec(text="Silent."), BeatSpec(text="Spoken.", audio_file="audio/beat_02.wav")],
            probe=failing_probe,
        )
    message = str(exc_info.value)
    assert "beat 2" in message
    assert "beat_02.wav" in message
    assert "Invalid data" in message  # the probe's own evidence travels along


def test_non_positive_probed_duration_is_rejected():
    probe = RecordingProbe(0.0)
    with pytest.raises(ManifestError, match="positive"):
        with_probed_durations(
            [BeatSpec(text="Spoken.", audio_file="audio/beat_01.wav")], probe=probe
        )


def test_probed_specs_build_a_valid_contiguous_manifest(tmp_path):
    """Integration: probe → build → write → read gives a manifest whose beat
    durations are the probed values and whose timeline is contiguous."""
    audio = tmp_path / "audio" / "beat_01.wav"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"RIFF....")  # the fake probe never reads the bytes
    probe = RecordingProbe(2.75)
    specs = with_probed_durations(
        [
            BeatSpec(text="Spoken.", audio_file=audio),
            BeatSpec(text="Silent."),
        ],
        probe=probe,
    )
    manifest = build_manifest("job_x", specs)
    path = write_manifest(manifest, tmp_path / "plan" / "manifest.json")
    loaded = read_manifest(path)
    assert loaded.beats[0].duration_s == 2.75
    assert loaded.beats[0].start_s == 0.0
    assert loaded.beats[1].start_s == 2.75
    assert loaded.beats[1].duration_s == DEFAULT_SILENT_BEAT_DURATION_S
    assert loaded.total_duration_s == pytest.approx(2.75 + DEFAULT_SILENT_BEAT_DURATION_S)


def test_pipeline_assembly_probes_audio_for_the_manifest(tmp_path):
    """Wiring check (BUILD_PLAN 2.4): assemble_job writes the manifest from
    ffprobed values — a provider-reported-looking duration in a pre-built
    spec path can't leak in, and the real probe runs on the real file."""
    import io
    import wave as wave_mod

    from app.assembly.pipeline import assemble_job
    from app.core.jobs import Asset, Job

    project = tmp_path / "proj"
    audio_dir = project / "audio"
    audio_dir.mkdir(parents=True)
    audio_path = audio_dir / "beat_01.wav"
    frames_2s = 16000
    buffer = io.BytesIO()
    with wave_mod.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\x00\x00" * frames_2s)
    audio_path.write_bytes(buffer.getvalue())

    asset = Asset(
        id=1, job_id="job_x", kind="tts", beat_index=1,
        params={"text": "Spoken.", "voice": "Kore"},
        out_path=audio_path, status="done",
    )
    job = Job(id="job_x", project="proj", status="running", created_at="", updated_at="",
              assets=(asset,))

    result = assemble_job(project, job)
    manifest = read_manifest(result.manifest_path)
    assert manifest.beats[0].duration_s == pytest.approx(2.0, abs=0.05)
    assert manifest.beats[0].audio_file == "audio/beat_01.wav"
