"""Seam: app.scripting.segment — auto-segmentation of long paragraphs (§6.1).

Catches: mid-sentence splits, splitting user-marked scenes, hints lost on
split. Named beats are explicit user structure and are never split.
"""
from app.scripting.segment import DEFAULT_MAX_BEAT_CHARS, segment_script


def _sentences(prefix: str, count: int, length: int) -> str:
    parts = [
        f"{prefix}{i} " + "x" * (length - len(prefix) - len(str(i)) - 1) + "."
        for i in range(count)
    ]
    return " ".join(parts)


def test_short_paragraph_passes_through():
    beats = segment_script("Short and fine.")
    assert [(b.label, b.text) for b in beats] == [(None, "Short and fine.")]


def test_overlong_paragraph_splits_by_sentence():
    text = _sentences("S", 3, 110)  # 3 sentences ~110 chars each = ~330 total
    beats = segment_script(text, max_chars=130)
    assert len(beats) == 3
    assert all(len(b.text) <= 130 for b in beats)


def test_splits_end_at_sentence_boundaries():
    text = _sentences("S", 4, 100)
    for beat in segment_script(text, max_chars=150):
        assert beat.text[-1] in ".!?", beat.text[-20:]


def test_named_beat_is_never_split():
    text = "[scene: long_scene]\n" + _sentences("S", 6, 100)
    beats = segment_script(text, max_chars=100)
    assert len(beats) == 1
    assert beats[0].label == "long_scene"


def test_single_overlong_sentence_stays_intact():
    monster = "y" * 500 + "!"  # no sentence boundary at all
    beats = segment_script(monster, max_chars=100)
    assert len(beats) == 1
    assert beats[0].text == monster


def test_img_hint_stays_on_first_split_beat():
    text = "<!-- img: dawn -->\n\n" + _sentences("S", 3, 110)
    beats = segment_script(text, max_chars=130)
    assert beats[0].img_hint == "dawn"
    assert all(b.img_hint is None for b in beats[1:])


def test_default_threshold_splits_typical_runon():
    two = _sentences("S", 2, 160)  # ~325 chars under default 280
    assert DEFAULT_MAX_BEAT_CHARS == 280
    assert len(segment_script(two)) == 2
