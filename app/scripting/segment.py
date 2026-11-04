"""Auto-segmentation of scripts into beats (spec §6.1).

Paragraph beats come from parse_script; an overlong *unnamed* paragraph is
split by sentence (accumulating whole sentences up to ``max_chars``). A
single sentence longer than ``max_chars`` is never broken mid-sentence.
Named beats are explicit user structure and are never split — the review UI
offers manual merge/split instead.
"""
from __future__ import annotations

import re

from app.scripting.parse import ParsedBeat, parse_script

DEFAULT_MAX_BEAT_CHARS = 280  # ≈ 18s of voiceover at a moderate pace

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?…])\s+")


def _split_by_sentence(text: str, max_chars: int) -> list[str]:
    sentences = _SENTENCE_BOUNDARY.split(text)
    chunks: list[str] = []
    buffer = ""
    for sentence in sentences:
        if not buffer:
            buffer = sentence
        elif len(buffer) + 1 + len(sentence) <= max_chars:
            buffer = f"{buffer} {sentence}"
        else:
            chunks.append(buffer)
            buffer = sentence
    if buffer:
        chunks.append(buffer)
    return chunks


def segment_script(text: str, max_chars: int = DEFAULT_MAX_BEAT_CHARS) -> list[ParsedBeat]:
    beats: list[ParsedBeat] = []
    for beat in parse_script(text):
        if beat.label is not None or len(beat.text) <= max_chars:
            beats.append(beat)
            continue
        for i, chunk in enumerate(_split_by_sentence(beat.text, max_chars)):
            beats.append(
                ParsedBeat(label=None, text=chunk, img_hint=beat.img_hint if i == 0 else None)
            )
    return beats
