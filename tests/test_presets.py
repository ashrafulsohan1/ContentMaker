"""Seam: app.data.presets (BUILD_PLAN 1.10) — the editable preset catalog.

Catches: presets.json drift (malformed rows, duplicate ids, out-of-range
pace), loader failing loudly instead of guessing, and the §8 UI contract
that presets are named bundles of style options (visual style + voice
settings) selectable in the options step.
"""
from __future__ import annotations

import json

import pytest

from app.data.presets import (
    PresetError,
    load_presets,
    presets_path,
)


def test_catalog_file_exists_and_is_utf8_json():
    path = presets_path()
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data.get("presets"), list) and data["presets"]


def test_load_returns_presets_with_unique_ids():
    presets = load_presets()
    assert len(presets) >= 3
    ids = [p.id for p in presets]
    assert len(ids) == len(set(ids))
    for preset in presets:
        assert preset.id and preset.label


def test_preset_is_a_named_bundle_of_style_options():
    """§2 glossary: preset = visual style + voice settings bundle."""
    presets = {p.id: p for p in load_presets()}
    clean = presets["clean-minimal"]
    assert clean.visual_style  # a visual style string for image prompts
    assert clean.voice is not None
    assert clean.voice.voice  # a voice name for TTS
    assert 0.1 <= clean.voice.pace <= 3.0
    assert clean.description  # shown in the UI


def test_preset_voice_tone_is_optional():
    presets = {p.id: p for p in load_presets()}
    assert all(
        p.voice is None or isinstance(p.voice.tone, (str, type(None))) for p in presets.values()
    )


def test_load_from_custom_path(tmp_path):
    file = tmp_path / "presets.json"
    file.write_text(
        json.dumps(
            {
                "presets": [
                    {
                        "id": "custom-one",
                        "label": "Custom One",
                        "description": "d",
                        "visual_style": "vs",
                        "voice": {"voice": "v", "pace": 1.0, "tone": None},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    presets = load_presets(file)
    assert [p.id for p in presets] == ["custom-one"]


def test_malformed_catalog_fails_loudly(tmp_path):
    file = tmp_path / "presets.json"
    file.write_text(json.dumps({"presets": [{"id": "x"}]}), encoding="utf-8")
    with pytest.raises(PresetError):
        load_presets(file)


def test_duplicate_ids_rejected(tmp_path):
    file = tmp_path / "presets.json"
    row = {
        "id": "dup",
        "label": "Dup",
        "description": "d",
        "visual_style": "vs",
        "voice": {"voice": "v", "pace": 1.0},
    }
    file.write_text(json.dumps({"presets": [row, row]}), encoding="utf-8")
    with pytest.raises(PresetError, match="dup"):
        load_presets(file)


def test_out_of_range_pace_rejected(tmp_path):
    file = tmp_path / "presets.json"
    file.write_text(
        json.dumps(
            {
                "presets": [
                    {
                        "id": "fast",
                        "label": "Fast",
                        "description": "d",
                        "voice": {"voice": "v", "pace": 99.0},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(PresetError, match="pace"):
        load_presets(file)


def test_missing_catalog_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_presets(tmp_path / "nope.json")
