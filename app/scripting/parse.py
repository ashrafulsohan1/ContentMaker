"""Script parser for the annotation convention (spec §6.1).

Convention (ruling 2026-11-04, ledgered): blank lines always bound beats;
``[scene: short-label]`` on its own line names the *next* beat only;
``<!-- img: description -->`` hints what a beat's visual should show (one
hint per beat, attached to the beat it precedes). Non-img HTML comments are
stripped silently. A marker before any body text carries to the next block
that has one; a later marker overrides an earlier pending one.

Malformed input degrades gracefully (no crash): an unmatched ``[scene:``
line stays as plain text; an unclosed ``<!-- img:`` comment still yields a
hint.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_SCENE_LINE = re.compile(r"^\[scene:\s*([^\]]+)\]\s*$")
_IMG_HINT = re.compile(r"<!--\s*img:\s*(.*?)(?:-->|$)", re.IGNORECASE)
_ANY_COMMENT = re.compile(r"<!--.*?(?:-->|$)", re.DOTALL)


@dataclass
class ParsedBeat:
    label: str | None
    text: str
    img_hint: str | None = None


def _normalize(text: str) -> str:
    return text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n").strip("\n")


def _clean_hint(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def parse_script(text: str) -> list[ParsedBeat]:
    text = _normalize(text)
    if not text.strip():
        return []

    beats: list[ParsedBeat] = []
    pending_label: str | None = None
    pending_hint: str | None = None

    def flush(label: str | None, hint: str | None, body: str) -> None:
        beats.append(ParsedBeat(label=label, text=body, img_hint=hint))

    for block in text.split("\n\n"):
        pre_marker_lines: list[str] = []
        block_hint: str | None = None
        label_in_block: str | None = None

        for raw_line in block.split("\n"):
            line = raw_line.strip()
            scene = _SCENE_LINE.match(line)
            if scene:
                pre = "\n".join(ln for ln in pre_marker_lines if ln).strip()
                if pre:
                    flush(pending_label, pending_hint or block_hint, pre)
                    pending_label = None
                    pending_hint = None
                    block_hint = None
                label_in_block = _clean_hint(scene.group(1))
                pre_marker_lines = []
                continue
            comment = _IMG_HINT.search(line)
            if comment and block_hint is None:
                block_hint = _clean_hint(comment.group(1))
            pre_marker_lines.append(_ANY_COMMENT.sub("", line))

        body = "\n".join(ln for ln in pre_marker_lines if ln).strip()
        if body:
            flush(label_in_block or pending_label, block_hint or pending_hint, body)
            pending_label = None
            pending_hint = None
        else:
            if label_in_block is not None:
                pending_label = label_in_block
            if block_hint is not None:
                pending_hint = block_hint

    return beats
