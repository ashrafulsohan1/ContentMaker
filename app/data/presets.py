"""Preset catalog loader (PROMPT §2/§8) — BUILD_PLAN 1.10.

A preset is a named bundle of style options (spec §2 glossary): one
``visual_style`` string that anchors image prompts (§6.3 consistency) plus
default voice settings (voice / pace / tone) for TTS (§6.2). The catalog
lives in ``presets.json`` as editable configuration data — like
``specs.py`` for platforms, adding a preset never touches code. Loading
rejects malformed rows with ``PresetError`` instead of guessing: duplicate
ids, blank id/label, non-finite or out-of-range pace (sanity band 0.1–3.0).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent
PRESETS_FILE = DATA_DIR / "presets.json"

_MIN_PACE = 0.1
_MAX_PACE = 3.0


class PresetError(ValueError):
    """The presets.json data is malformed — fail loudly, never guess."""


@dataclass(frozen=True)
class VoiceSettings:
    """Default TTS voice settings carried by a preset (§6.2)."""

    voice: str
    pace: float = 1.0
    tone: str | None = None


@dataclass(frozen=True)
class Preset:
    """One selectable style bundle for the options step."""

    id: str
    label: str
    description: str
    visual_style: str | None
    voice: VoiceSettings | None


def presets_path() -> Path:
    return PRESETS_FILE


def load_presets(path: Path | None = None) -> list[Preset]:
    """Load the preset catalog; any malformed row raises PresetError."""
    file = path if path is not None else PRESETS_FILE
    payload = json.loads(file.read_text(encoding="utf-8"))
    raw_presets = payload.get("presets")
    if not isinstance(raw_presets, list) or not raw_presets:
        raise PresetError(f"{file}: 'presets' must be a non-empty list")

    presets: list[Preset] = []
    seen: set[str] = set()
    for position, raw in enumerate(raw_presets, 1):
        preset = _preset(file, position, raw)
        if preset.id in seen:
            raise PresetError(f"{file}: preset {position}: duplicate id {preset.id!r}")
        seen.add(preset.id)
        presets.append(preset)
    return presets


def _preset(file: Path, position: int, raw: dict) -> Preset:
    where = f"{file}: preset {position}"
    if not isinstance(raw, dict):
        raise PresetError(f"{where} must be a JSON object, got {type(raw).__name__}")
    preset_id = raw.get("id")
    if not isinstance(preset_id, str) or not preset_id.strip():
        raise PresetError(f"{where}: 'id' must be a non-empty string, got {preset_id!r}")
    label = raw.get("label")
    if not isinstance(label, str) or not label.strip():
        raise PresetError(f"{where} ({preset_id!r}): 'label' must be a non-empty string")
    description = raw.get("description", "")
    if not isinstance(description, str):
        raise PresetError(f"{where} ({preset_id!r}): 'description' must be a string")
    style = raw.get("visual_style")
    if style is not None and (not isinstance(style, str) or not style.strip()):
        raise PresetError(
            f"{where} ({preset_id!r}): 'visual_style' must be a non-empty string or null"
        )
    voice_raw = raw.get("voice")
    voice = None if voice_raw is None else _voice(file, preset_id, voice_raw)
    return Preset(
        id=preset_id,
        label=label,
        description=description,
        visual_style=style,
        voice=voice,
    )


def _voice(file: Path, preset_id: str, raw: dict) -> VoiceSettings:
    where = f"{file}: preset {preset_id!r}"
    if not isinstance(raw, dict):
        raise PresetError(f"{where}: 'voice' must be a JSON object, got {type(raw).__name__}")
    name = raw.get("voice")
    if not isinstance(name, str) or not name.strip():
        raise PresetError(f"{where}: 'voice.voice' must be a non-empty string, got {name!r}")
    pace = raw.get("pace", 1.0)
    if isinstance(pace, bool) or not isinstance(pace, (int, float)):
        raise PresetError(f"{where}: 'voice.pace' must be a number, got {pace!r}")
    pace = float(pace)
    if not math.isfinite(pace) or not _MIN_PACE <= pace <= _MAX_PACE:
        raise PresetError(
            f"{where}: 'voice.pace' must be between {_MIN_PACE} and {_MAX_PACE}, got {pace!r}"
        )
    tone = raw.get("tone")
    if tone is not None and not isinstance(tone, str):
        raise PresetError(f"{where}: 'voice.tone' must be a string or null, got {tone!r}")
    return VoiceSettings(voice=name, pace=pace, tone=tone)
