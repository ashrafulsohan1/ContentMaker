"""Seam: app.data.specs — platform/format catalog (spec §5 as editable config).

Catches: malformed platform data, missing spec-mandated platforms, lookups
that silently return None. Does not cover UI rendering.
"""
from pathlib import Path

import pytest

from app.data.specs import FormatSpec, load_platforms, platforms_path, lookup_format


def test_load_platforms_covers_spec_section_5():
    platforms = load_platforms()
    ids = {p.id for p in platforms}
    assert {"youtube", "tiktok", "instagram", "x", "facebook", "linkedin", "pinterest"} <= ids


def test_every_format_is_fully_specified():
    kinds = {"video", "image", "carousel"}
    for platform in load_platforms():
        assert platform.formats, f"{platform.id} has no formats"
        for f in platform.formats:
            assert f.kind in kinds, f"{f.id}: bad kind {f.kind!r}"
            assert len(f.aspect) == 2 and all(v > 0 for v in f.aspect), f.id
            assert len(f.resolution) == 2 and all(v > 0 for v in f.resolution), f.id
            assert f.max_duration_s is None or f.max_duration_s > 0, f.id
            if f.kind == "video":
                assert f.max_duration_s is not None, f"{f.id}: video needs max_duration_s"
                assert f.container == "mp4", f.id
            if f.kind == "carousel":
                assert f.min_images >= 2 and f.max_images >= f.min_images, f.id


def test_lookup_format_roundtrip_shorts():
    fmt = lookup_format("youtube", "youtube_shorts")
    assert fmt == FormatSpec(
        id="youtube_shorts",
        label="Shorts 9:16",
        kind="video",
        aspect=(9, 16),
        max_duration_s=180,
        resolution=(1080, 1920),
        container="mp4",
        min_images=1,
        max_images=1,
    )


def test_lookup_format_pinterest_pin_is_2_3_image():
    fmt = lookup_format("pinterest", "pinterest_pin")
    assert fmt.kind == "image"
    assert fmt.aspect == (2, 3)


def test_lookup_format_unknown_platform_raises_with_clear_message():
    with pytest.raises(ValueError, match="unknown platform 'myspace'"):
        lookup_format("myspace", "whatever")


def test_loader_tolerates_meta_key_and_missing_file_message(tmp_path: Path):
    # unknown top-level keys are ignored (keeps the file editable)
    platforms = load_platforms(platforms_path())
    assert all(p.id != "_meta" for p in platforms)

    bogus = tmp_path / "nope.json"
    with pytest.raises(FileNotFoundError):
        load_platforms(bogus)
