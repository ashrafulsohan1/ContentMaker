"""Seam: app.scripting.parse — annotation convention (spec §6.1).

Convention: blank-line paragraphs = default beat boundaries;
[scene: label] on its own line starts a named beat (merging paragraphs until
the next marker); <!-- img: description --> hints a beat's visual.
Catches: marker/hint misattachment, BOM/CRLF breakage, crashes on malformed
input (review-focus #1).
"""
from app.scripting.parse import parse_script


def test_blank_line_paragraphs_become_unnamed_beats():
    beats = parse_script("Alpha line.\n\nBeta line.\n\nGamma line.")
    assert [(b.label, b.text) for b in beats] == [
        (None, "Alpha line."),
        (None, "Beta line."),
        (None, "Gamma line."),
    ]


def test_scene_marker_names_following_paragraph():
    beats = parse_script("[scene: hook]\nHello there.\n\nWorld continues.")
    assert beats[0].label == "hook"
    assert beats[0].text == "Hello there."
    assert beats[1].label is None
    assert beats[1].text == "World continues."


def test_scene_marker_names_only_the_next_beat():
    # Blank lines always bound beats; a marker names the next beat only.
    beats = parse_script("[scene: a]\nP1.\n\nP2.\n[scene: b]\nP3.")
    assert [(b.label, b.text) for b in beats] == [
        ("a", "P1."),
        (None, "P2."),
        ("b", "P3."),
    ]


def test_marker_after_text_in_same_paragraph_starts_new_beat():
    beats = parse_script("Intro text.\n[scene: next]\nMore.")
    assert [(b.label, b.text) for b in beats] == [
        (None, "Intro text."),
        ("next", "More."),
    ]


def test_marker_and_hint_in_same_block_combine():
    beats = parse_script("<!-- img: x -->\n[scene: a]\nBody.")
    assert beats[0].label == "a"
    assert beats[0].img_hint == "x"
    assert beats[0].text == "Body."


def test_img_hint_attaches_to_beat_and_is_stripped_from_text():
    beats = parse_script("<!-- img: red apple -->\n\nText one.\n\n<!-- img: two -->Text two.")
    assert beats[0].img_hint == "red apple"
    assert beats[0].text == "Text one."
    assert beats[1].img_hint == "two"
    assert beats[1].text == "Text two."
    assert all("<!--" not in b.text for b in beats)


def test_img_hint_before_any_beat_applies_to_first_beat():
    beats = parse_script("<!-- img: sunrise -->\n\nFirst.\n\nSecond.")
    assert beats[0].img_hint == "sunrise"
    assert beats[1].img_hint is None


def test_crlf_and_bom_normalized():
    beats = parse_script("\ufeffAlpha.\r\n\r\nBeta.\r\n")
    assert [(b.label, b.text) for b in beats] == [(None, "Alpha."), (None, "Beta.")]


def test_empty_script_yields_no_beats():
    assert parse_script("") == []
    assert parse_script("  \n \n\t\n") == []


def test_malformed_scene_marker_degrades_to_text():
    beats = parse_script("[scene: broken\n\nReal text.")
    assert len(beats) == 2  # no crash; the broken line stays as beat text
    assert "[scene: broken" in beats[0].text


def test_unclosed_img_comment_still_hints():
    beats = parse_script("<!-- img: sunset over hills\n\nBody text.")
    assert beats[0].img_hint == "sunset over hills"
    assert beats[0].text == "Body text."


def test_non_img_html_comments_stripped_without_becoming_hints():
    beats = parse_script("Alpha. <!-- production note -->\n\nBeta.")
    assert beats[0].text == "Alpha."
    assert beats[0].img_hint is None
    assert beats[1].text == "Beta."


def test_label_whitespace_collapsed():
    (beat,) = parse_script("[scene:  my   label ]\nText.")
    assert beat.label == "my label"
