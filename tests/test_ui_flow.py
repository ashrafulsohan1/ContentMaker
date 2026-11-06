"""Seam: the generation UI flow over HTTP (BUILD_PLAN 1.10) — TestClient only.

Walks the spec §4/§8 flow at the API level: input (paste/upload + project
name) → segmentation review (edit/merge/split, paginated for long scripts —
risk #8 pins a 100+ beat script) → options (platform/format from
platforms.json, presets from presets.json, audio on/off, visual type,
availability from the registry) → estimate + explicit confirm → generate on
the job engine with the projects/<name>/ §7 layout on disk.

Catches: broken wizard state (cookie/flow loss), unescaped script text,
pagination slicing errors, beat ops corrupting neighbors, options that
generate nothing or violate the selected format, silent spend (estimate +
explicit confirm gate), and job creation outside the project tree.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.assembly.pipeline import read_job_plan
from app.engines.mocks import MockTTSProvider
from app.engines.registry import Registry
from app.main import create_app

SCRIPT = (
    "[scene: hook]\n"
    "Open with the one-line promise of this piece.\n"
    "\n"
    "Second paragraph beat opens here. It carries a couple of sentences to read aloud.\n"
    "\n"
    "<!-- img: a red square on a white desk -->\n"
    "Third beat carries a visual hint for its image.\n"
)

PAGE_SIZE = 10  # app.web.flow.PAGE_SIZE — pinned by the pagination tests


def make_client(tmp_path: Path, registry: Registry | None = None) -> TestClient:
    app = create_app(registry=registry, projects_root=tmp_path / "projects")
    return TestClient(app)


def start_flow(client: TestClient, project: str = "demo", script: str = SCRIPT) -> None:
    resp = client.post(
        "/input", data={"project": project, "script": script}, follow_redirects=False
    )
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == "/review"


def beat_ids(html: str) -> list[str]:
    # match the plain form action only — hx-post mirrors it and would double-count
    return re.findall(r'action="/review/beat/([A-Za-z0-9]+)/edit"', html)


def visible_texts(html: str) -> list[str]:
    return re.findall(r"<textarea[^>]*>(.*?)</textarea>", html, re.DOTALL)


# 60s: task 1.12 wired real-ffmpeg assembly into finalize, so full-flow jobs
# now encode platform-sized deliverables — the 15s pre-assembly budget is
# too tight under full-suite load.
def wait_terminal(client: TestClient, job_id: str, timeout_s: float = 60.0) -> dict:
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        payload = client.get(f"/api/jobs/{job_id}").json()
        if payload["status"] in {"completed", "partial", "failed", "cancelled"}:
            return payload
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never reached a terminal status: {payload}")


# --- step 1: input -----------------------------------------------------------


class TestInput:
    def test_index_shows_input_form(self, tmp_path):
        client = make_client(tmp_path)
        resp = client.get("/")
        assert resp.status_code == 200
        assert "ContentMaker" in resp.text
        assert 'name="project"' in resp.text
        assert 'name="script"' in resp.text
        assert 'name="script_file"' in resp.text

    def test_htmx_and_css_assets_served(self, tmp_path):
        client = make_client(tmp_path)
        index = client.get("/")
        assert "/static/htmx.min.js" in index.text
        assert client.get("/static/htmx.min.js").status_code == 200
        assert client.get("/static/app.css").status_code == 200

    def test_pasted_script_creates_flow_and_redirects_to_review(self, tmp_path):
        client = make_client(tmp_path)
        start_flow(client)
        review = client.get("/review")
        assert review.status_code == 200
        assert "promise of this piece" in review.text
        assert "a red square" in review.text  # img hint carried through

    def test_uploaded_file_is_accepted(self, tmp_path):
        client = make_client(tmp_path)
        upload = {
            "script_file": ("script.md", "From a file beat one.\n\nBeat two.", "text/markdown")
        }
        resp = client.post(
            "/input", data={"project": "uploaded"}, files=upload, follow_redirects=False
        )
        assert resp.status_code == 303
        review = client.get("/review")
        assert "From a file beat one." in review.text

    def test_empty_script_re_renders_with_error(self, tmp_path):
        client = make_client(tmp_path)
        resp = client.post("/input", data={"project": "x", "script": "   \n  "})
        assert resp.status_code == 200
        assert "script" in resp.text.lower()
        assert resp.history == []  # no redirect

    def test_empty_project_name_re_renders_with_error(self, tmp_path):
        client = make_client(tmp_path)
        resp = client.post("/input", data={"project": "  ", "script": "Hello beat."})
        assert resp.status_code == 200
        assert "name" in resp.text.lower()

    def test_reserved_windows_name_re_renders_with_clear_error(self, tmp_path):
        client = make_client(tmp_path)
        resp = client.post("/input", data={"project": "CON", "script": "Hello beat."})
        assert resp.status_code == 200
        assert "reserved" in resp.text.lower()

    def test_existing_project_name_re_renders_with_clear_error(self, tmp_path):
        client = make_client(tmp_path)
        start_flow(client, project="taken")
        resp = client.post("/input", data={"project": "taken", "script": SCRIPT})
        assert resp.status_code == 200
        assert "already exists" in resp.text

    def test_overlong_paragraph_is_sentence_split(self, tmp_path):
        client = make_client(tmp_path)
        long_para = " ".join(
            f"Sentence number {i} rambles on well past the beat limit for a while."
            for i in range(10)
        )
        start_flow(client, project="long", script=f"One short beat.\n\n{long_para}")
        texts = visible_texts(client.get("/review").text)
        assert len(texts) >= 4  # 1 short beat + paragraph split into >= 3 chunks

    def test_script_is_saved_into_project_script_folder(self, tmp_path):
        client = make_client(tmp_path)
        start_flow(client, project="saver")
        saved = tmp_path / "projects" / "saver" / "script" / "script.txt"
        assert saved.read_text(encoding="utf-8") == SCRIPT


# --- step 2: beat review ------------------------------------------------------


class TestReviewPagination:
    def test_review_without_flow_redirects_to_input(self, tmp_path):
        client = make_client(tmp_path)
        resp = client.get("/review", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/")

    def test_beats_are_paginated(self, tmp_path):
        client = make_client(tmp_path)
        script = "\n\n".join(f"Beat {n} text for the pager." for n in range(1, 121))
        start_flow(client, project="pager", script=script)
        page1 = client.get("/review")
        assert f"Page 1 of {120 // PAGE_SIZE}" in page1.text
        assert "Beat 1 text" in page1.text
        assert "Beat 11 text" not in page1.text  # page 1 holds beats 1..10 only
        page12 = client.get("/review", params={"page": 12})
        assert "Beat 120 text" in page12.text
        assert "Beat 111 text" in page12.text
        assert "Beat 1 text" not in page12.text

    def test_out_of_range_page_is_clamped(self, tmp_path):
        client = make_client(tmp_path)
        script = "\n\n".join(f"Beat {n} here." for n in range(1, 26))
        start_flow(client, project="clamp", script=script)
        resp = client.get("/review", params={"page": 999})
        assert resp.status_code == 200
        assert "Beat 25 here." in resp.text

    def test_page_zero_is_clamped_to_first(self, tmp_path):
        client = make_client(tmp_path)
        start_flow(client, project="clamp0")
        resp = client.get("/review", params={"page": 0})
        assert resp.status_code == 200
        assert "promise of this piece" in resp.text


class TestBeatOperations:
    def test_edit_updates_text_and_label(self, tmp_path):
        client = make_client(tmp_path)
        start_flow(client)
        beat_id = beat_ids(client.get("/review").text)[0]
        resp = client.post(
            f"/review/beat/{beat_id}/edit",
            data={"text": "Rewritten hook text.", "label": "new-hook"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        page = client.get("/review")
        assert "Rewritten hook text." in page.text
        assert 'value="new-hook"' in page.text

    def test_edit_rejects_blank_text(self, tmp_path):
        client = make_client(tmp_path)
        start_flow(client)
        beat_id = beat_ids(client.get("/review").text)[0]
        resp = client.post(
            f"/review/beat/{beat_id}/edit",
            data={"text": "   ", "label": ""},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error" in resp.headers["location"]
        assert "promise of this piece" in client.get("/review").text  # unchanged

    def test_merge_joins_beat_with_next(self, tmp_path):
        client = make_client(tmp_path)
        start_flow(client)
        first_id = beat_ids(client.get("/review").text)[0]
        before = client.get("/review")
        resp = client.post(f"/review/beat/{first_id}/merge", follow_redirects=False)
        assert resp.status_code == 303
        page = client.get("/review")
        assert "promise of this piece.\nSecond paragraph beat" in page.text
        assert len(beat_ids(page.text)) == len(beat_ids(before.text)) - 1

    def test_merge_last_beat_is_rejected_with_notice(self, tmp_path):
        client = make_client(tmp_path)
        start_flow(client)
        last_id = beat_ids(client.get("/review").text)[-1]
        resp = client.post(f"/review/beat/{last_id}/merge", follow_redirects=False)
        assert resp.status_code == 303
        assert "notice" in resp.headers["location"]
        assert len(beat_ids(client.get("/review").text)) == 3

    def test_split_divides_beat_into_sentences(self, tmp_path):
        client = make_client(tmp_path)
        start_flow(client)
        target = beat_ids(client.get("/review").text)[1]  # two-sentence beat
        resp = client.post(f"/review/beat/{target}/split", follow_redirects=False)
        assert resp.status_code == 303
        page = client.get("/review")
        texts = visible_texts(page.text)
        assert len(texts) == 4
        assert "Second paragraph beat opens here." in texts[1]
        assert "It carries a couple of sentences" in texts[2]

    def test_split_single_sentence_beat_is_rejected_with_notice(self, tmp_path):
        client = make_client(tmp_path)
        start_flow(client)
        first_id = beat_ids(client.get("/review").text)[0]
        resp = client.post(f"/review/beat/{first_id}/split", follow_redirects=False)
        assert resp.status_code == 303
        assert "notice" in resp.headers["location"]
        assert len(beat_ids(client.get("/review").text)) == 3

    def test_htmx_request_gets_fragment_not_redirect(self, tmp_path):
        client = make_client(tmp_path)
        start_flow(client)
        beat_id = beat_ids(client.get("/review").text)[0]
        resp = client.post(
            f"/review/beat/{beat_id}/edit",
            data={"text": "Htmx swapped text.", "label": ""},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "<html" not in resp.text.lower()
        assert 'id="beat-list"' in resp.text
        assert "Htmx swapped text." in resp.text

    def test_unknown_beat_id_is_rejected(self, tmp_path):
        client = make_client(tmp_path)
        start_flow(client)
        resp = client.post(
            "/review/beat/bdoesnotexist/edit",
            data={"text": "x"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error" in resp.headers["location"]


# --- step 3: options ----------------------------------------------------------


class TestOptions:
    def test_options_without_flow_redirects_to_input(self, tmp_path):
        client = make_client(tmp_path)
        resp = client.get("/options", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/")

    def test_options_lists_platforms_formats_presets(self, tmp_path):
        client = make_client(tmp_path)
        start_flow(client)
        resp = client.get("/options")
        assert resp.status_code == 200
        assert 'value="youtube"' in resp.text
        assert 'value="instagram"' in resp.text
        assert 'value="pinterest"' in resp.text
        assert 'value="youtube_shorts"' in resp.text  # first platform's formats
        assert 'value="clean-minimal"' in resp.text
        assert 'name="audio"' in resp.text
        assert 'name="visual"' in resp.text
        assert 'name="spend_cap_usd"' in resp.text
        assert 'name="dry_run"' in resp.text and "checked" in resp.text

    def test_audio_and_dry_run_default_on(self, tmp_path):
        client = make_client(tmp_path)
        start_flow(client)
        html = client.get("/options").text
        audio = html.split('name="audio"', 1)[1][:80]
        dry = html.split('name="dry_run"', 1)[1][:80]
        assert "checked" in audio
        assert "checked" in dry

    def test_formats_fragment_reads_the_param_htmx_sends(self, tmp_path):
        """ui-f1 regression: the Platform select (options.html name=platform_id,
        hx-include=self) GETs /options/formats?platform_id=... on change — the
        fragment must key off that exact param. Keying off anything else always
        renders the empty 'Choose a platform first' placeholder and dead-ends
        the options step."""
        client = make_client(tmp_path)
        start_flow(client)
        frag = client.get("/options/formats", params={"platform_id": "instagram"})
        assert frag.status_code == 200
        assert 'value="instagram_reels"' in frag.text
        assert 'value="instagram_carousel"' in frag.text
        assert "youtube_shorts" not in frag.text
        assert "Choose a platform first" not in frag.text

    def test_registry_availability_is_shown(self, tmp_path):
        client = make_client(tmp_path)
        start_flow(client)
        html = client.get("/options").text
        assert "mock" in html  # the registered provider id is visible per engine

    def test_missing_engine_availability_is_reported(self, tmp_path):
        registry = Registry()
        registry.register("tts", MockTTSProvider())  # no image / video providers
        client = make_client(tmp_path, registry=registry)
        start_flow(client)
        html = client.get("/options").text
        assert "not available" in html.lower()

    def test_valid_options_create_job_and_show_estimate(self, tmp_path):
        client = make_client(tmp_path)
        start_flow(client)
        resp = client.post(
            "/options",
            data={
                "platform_id": "youtube",
                "format_id": "youtube_shorts",
                "audio": "on",
                "visual": "video",
                "dry_run": "on",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        confirm_url = resp.headers["location"]
        assert confirm_url.startswith("/confirm/")
        job_id = confirm_url.rsplit("/", 1)[1]
        confirm = client.get(confirm_url)
        assert confirm.status_code == 200
        assert "$0.0000" in confirm.text  # dry-run estimate is exactly zero
        assert f"/jobs/{job_id}/confirm" in confirm.text  # explicit confirm button

        payload = client.get(f"/api/jobs/{job_id}").json()
        assert payload["status"] == "awaiting_confirmation"
        kinds = sorted(a["kind"] for a in payload["assets"])
        assert kinds == ["tts", "tts", "tts", "video", "video", "video"]
        assert payload["estimate_usd"] == 0.0

    def test_options_with_preset_apply_style_and_voice(self, tmp_path):
        client = make_client(tmp_path)
        start_flow(client)
        resp = client.post(
            "/options",
            data={
                "platform_id": "instagram",
                "format_id": "instagram_feed_square",
                "preset_id": "clean-minimal",
                "audio": "on",
                "visual": "images",
                "dry_run": "on",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        job_id = resp.headers["location"].rsplit("/", 1)[1]
        payload = client.get(f"/api/jobs/{job_id}").json()
        images = [a for a in payload["assets"] if a["kind"] == "image"]
        assert len(images) == 3
        assert images[0]["params"]["style_hint"]  # preset visual style applied
        assert images[0]["params"]["width"] == 1080  # platform-correct dims
        assert images[0]["params"]["height"] == 1080
        tts = [a for a in payload["assets"] if a["kind"] == "tts"]
        assert tts[0]["params"]["voice"]  # preset voice applied

    def test_audio_off_and_visual_none_is_rejected(self, tmp_path):
        client = make_client(tmp_path)
        start_flow(client)
        resp = client.post(
            "/options",
            data={"platform_id": "youtube", "format_id": "youtube_shorts"},
        )
        assert resp.status_code == 200
        assert "nothing to generate" in resp.text.lower()

    def test_video_visual_on_image_format_is_rejected(self, tmp_path):
        client = make_client(tmp_path)
        start_flow(client)
        resp = client.post(
            "/options",
            data={
                "platform_id": "instagram",
                "format_id": "instagram_feed_square",
                "audio": "on",
                "visual": "video",
                "dry_run": "on",
            },
        )
        assert resp.status_code == 200
        assert "video" in resp.text.lower() and "image" in resp.text.lower()

    def test_video_format_requires_visuals(self, tmp_path):
        client = make_client(tmp_path)
        start_flow(client)
        resp = client.post(
            "/options",
            data={
                "platform_id": "youtube",
                "format_id": "youtube_shorts",
                "audio": "on",
                "visual": "none",
                "dry_run": "on",
            },
        )
        assert resp.status_code == 200
        assert "visual" in resp.text.lower()

    def test_carousel_minimum_images_enforced(self, tmp_path):
        client = make_client(tmp_path)
        start_flow(client, project="tiny", script="Only one beat here.")
        resp = client.post(
            "/options",
            data={
                "platform_id": "instagram",
                "format_id": "instagram_carousel",
                "audio": "on",
                "visual": "images",
                "dry_run": "on",
            },
        )
        assert resp.status_code == 200
        assert "at least 2" in resp.text

    def test_missing_engine_blocks_job_creation(self, tmp_path):
        registry = Registry()
        registry.register("tts", MockTTSProvider())
        client = make_client(tmp_path, registry=registry)
        start_flow(client)
        resp = client.post(
            "/options",
            data={
                "platform_id": "youtube",
                "format_id": "youtube_shorts",
                "audio": "on",
                "visual": "video",
                "dry_run": "on",
            },
        )
        assert resp.status_code == 200
        assert "video" in resp.text.lower()
        assert "no provider" in resp.text.lower()

    def test_unknown_platform_is_rejected(self, tmp_path):
        client = make_client(tmp_path)
        start_flow(client)
        resp = client.post(
            "/options",
            data={
                "platform_id": "myspace",
                "format_id": "whatever",
                "audio": "on",
                "visual": "images",
                "dry_run": "on",
            },
        )
        assert resp.status_code == 200
        assert "myspace" in resp.text.lower()

    def test_bad_spend_cap_is_rejected(self, tmp_path):
        client = make_client(tmp_path)
        start_flow(client)
        resp = client.post(
            "/options",
            data={
                "platform_id": "youtube",
                "format_id": "youtube_shorts",
                "audio": "on",
                "visual": "video",
                "dry_run": "on",
                "spend_cap_usd": "-3",
            },
        )
        assert resp.status_code == 200
        assert "spend cap" in resp.text.lower()

    def test_non_finite_spend_cap_is_rejected(self, tmp_path):
        # NaN compares False against everything — it would silently switch the
        # hard cap off; "inf" would serialize into the API payload as invalid
        # JSON. Both must be refused like any other malformed cap.
        for raw in ("nan", "inf", "-inf"):
            client = make_client(tmp_path / raw)  # fresh projects root per value
            start_flow(client)
            resp = client.post(
                "/options",
                data={
                    "platform_id": "youtube",
                    "format_id": "youtube_shorts",
                    "audio": "on",
                    "visual": "video",
                    "dry_run": "on",
                    "spend_cap_usd": raw,
                },
            )
            assert resp.status_code == 200
            assert "spend cap" in resp.text.lower()

    def test_spend_cap_is_recorded_on_job(self, tmp_path):
        client = make_client(tmp_path)
        start_flow(client)
        resp = client.post(
            "/options",
            data={
                "platform_id": "youtube",
                "format_id": "youtube_shorts",
                "audio": "on",
                "visual": "video",
                "dry_run": "on",
                "spend_cap_usd": "5",
            },
            follow_redirects=False,
        )
        job_id = resp.headers["location"].rsplit("/", 1)[1]
        payload = client.get(f"/api/jobs/{job_id}").json()
        assert payload["spend_cap_usd"] == pytest.approx(5.0)

    def test_pace_outside_sanity_band_is_rejected_server_side(self, tmp_path):
        # the UI input bounds pace to 0.1–3 (options.html) and presets.py
        # enforces the same band — a direct form post must not sneak past it
        client = make_client(tmp_path)
        start_flow(client)
        for pace in ("50", "0.05"):
            resp = client.post(
                "/options",
                data={
                    "platform_id": "youtube",
                    "format_id": "youtube_shorts",
                    "audio": "on",
                    "visual": "video",
                    "dry_run": "on",
                    "pace": pace,
                },
            )
            assert resp.status_code == 200, f"pace={pace} was accepted"
            assert "pace" in resp.text.lower()
        ok = client.post(
            "/options",
            data={
                "platform_id": "youtube",
                "format_id": "youtube_shorts",
                "audio": "on",
                "visual": "video",
                "dry_run": "on",
                "pace": "2",
            },
            follow_redirects=False,
        )
        assert ok.status_code == 303  # in-band pace is accepted

    def test_overlay_text_offered_and_recorded_in_the_job_plan(self, tmp_path):
        """§6.4 optional per-image text overlay (task 3.2) is reachable in the
        flow: the options form offers it and the chosen text lands in the job
        plan, where assembly draws it onto every exported image."""
        client = make_client(tmp_path)
        start_flow(client)
        page = client.get("/options")
        assert 'name="overlay_text"' in page.text
        resp = client.post(
            "/options",
            data={
                "platform_id": "instagram",
                "format_id": "instagram_carousel",
                "audio": "on",
                "visual": "images",
                "overlay_text": "Hook line 標題",
                "dry_run": "on",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        job_id = resp.headers["location"].rsplit("/", 1)[1]
        plan = read_job_plan(tmp_path / "projects" / "demo", job_id)
        assert plan is not None
        assert plan.overlay_text == "Hook line 標題"

    def test_blank_overlay_text_is_recorded_as_none(self, tmp_path):
        client = make_client(tmp_path)
        start_flow(client)
        resp = client.post(
            "/options",
            data={
                "platform_id": "instagram",
                "format_id": "instagram_carousel",
                "audio": "on",
                "visual": "images",
                "overlay_text": "   ",
                "dry_run": "on",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        job_id = resp.headers["location"].rsplit("/", 1)[1]
        plan = read_job_plan(tmp_path / "projects" / "demo", job_id)
        assert plan is not None
        assert plan.overlay_text is None


# --- step 4: confirm + generate ------------------------------------------------


@pytest.fixture()
def estimated_job(tmp_path) -> Iterator[tuple[TestClient, str]]:
    client = make_client(tmp_path)
    start_flow(client)
    resp = client.post(
        "/options",
        data={
            "platform_id": "youtube",
            "format_id": "youtube_shorts",
            "audio": "on",
            "visual": "video",
            "dry_run": "on",
        },
        follow_redirects=False,
    )
    yield client, resp.headers["location"].rsplit("/", 1)[1]


class TestConfirmAndGenerate:
    def test_confirm_starts_job(self, estimated_job):
        client, job_id = estimated_job
        resp = client.post(f"/jobs/{job_id}/confirm", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/jobs/{job_id}"

    def test_generated_job_completes_with_assets_on_disk(self, estimated_job, tmp_path):
        client, job_id = estimated_job
        client.post(f"/jobs/{job_id}/confirm")
        payload = wait_terminal(client, job_id)
        assert payload["status"] == "completed"
        assert all(a["status"] == "done" for a in payload["assets"])

        project = tmp_path / "projects" / "demo"
        for name in ("script", "plan", "audio", "visuals", "output", "state"):
            assert (project / name).is_dir(), f"missing §7 folder: {name}"
        wavs = sorted((project / "audio").glob("beat_*.wav"))
        assert len(wavs) == 3
        clips = sorted((project / "visuals").glob("beat_*.mp4"))
        assert len(clips) == 3
        assert (project / "state" / "job.db").exists()

    def test_job_page_renders_status(self, estimated_job):
        client, job_id = estimated_job
        client.post(f"/jobs/{job_id}/confirm")
        wait_terminal(client, job_id)
        page = client.get(f"/jobs/{job_id}")
        assert page.status_code == 200
        assert "completed" in page.text
        assert job_id in page.text

    def test_job_page_unknown_job_is_404(self, tmp_path):
        client = make_client(tmp_path)
        assert client.get("/jobs/job_nope").status_code == 404

    def test_confirm_unknown_job_is_404(self, tmp_path):
        client = make_client(tmp_path)
        resp = client.post("/jobs/job_nope/confirm", follow_redirects=False)
        assert resp.status_code == 404

    def test_double_confirm_is_conflict(self, estimated_job):
        client, job_id = estimated_job
        first = client.post(f"/jobs/{job_id}/confirm", follow_redirects=False)
        assert first.status_code == 303
        second = client.post(f"/jobs/{job_id}/confirm", follow_redirects=False)
        assert second.status_code == 409

    def test_confirm_page_redirects_once_job_is_running(self, estimated_job):
        client, job_id = estimated_job
        client.post(f"/jobs/{job_id}/confirm")
        resp = client.get(f"/confirm/{job_id}", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/jobs/{job_id}"

    def test_json_api_serves_job_status(self, estimated_job):
        client, job_id = estimated_job
        payload = client.get(f"/api/jobs/{job_id}").json()
        assert payload["id"] == job_id
        assert payload["project"] == "demo"
        assert payload["status"] == "awaiting_confirmation"
        asset = payload["assets"][0]
        assert {"id", "kind", "beat_index", "status", "params", "out_path"} <= set(asset)
        assert payload["providers"]  # engine → provider map recorded

    def test_json_api_unknown_job_is_404(self, tmp_path):
        client = make_client(tmp_path)
        resp = client.get("/api/jobs/job_nope")
        assert resp.status_code == 404
        assert resp.json()["detail"]

    def test_estimated_assets_live_under_the_project_tree(self, estimated_job, tmp_path):
        client, job_id = estimated_job
        payload = client.get(f"/api/jobs/{job_id}").json()
        project = tmp_path / "projects" / "demo"
        for asset in payload["assets"]:
            out = Path(asset["out_path"])
            assert out.is_absolute()
            assert project in out.parents
            assert asset["kind"] == "tts" or "visuals" in out.parts[-2:]


# --- confirm after a restart / monitor confirm affordance (review findings) ----


class TestConfirmWithoutWizardFlow:
    """The wizard flow is in-memory; the job is persisted. A server restart
    between estimate and confirm (or LRU eviction) must leave the estimated
    job startable: the confirm page renders from the persisted job and the
    monitor offers a confirm affordance (spec §6.6 gate must not dead-end)."""

    OPTIONS = {
        "platform_id": "youtube",
        "format_id": "youtube_shorts",
        "audio": "on",
        "visual": "video",
        "dry_run": "on",
    }

    def test_confirm_page_renders_from_persisted_job_after_restart(self, tmp_path):
        client = make_client(tmp_path)
        start_flow(client)
        resp = client.post("/options", data=self.OPTIONS, follow_redirects=False)
        assert resp.status_code == 303
        job_id = resp.headers["location"].rsplit("/", 1)[1]

        # a fresh app over the same projects_root: job persisted, flow gone
        fresh = make_client(tmp_path)
        page = fresh.get(f"/confirm/{job_id}")
        assert page.status_code == 200, page.text  # was jinja2 UndefinedError
        assert f'action="/jobs/{job_id}/confirm"' in page.text  # still confirmable
        assert "$0.0000" in page.text  # persisted estimate shown
        assert "demo" in page.text  # project named from the persisted job

        started = fresh.post(f"/jobs/{job_id}/confirm", follow_redirects=False)
        assert started.status_code == 303
        assert wait_terminal(fresh, job_id)["status"] == "completed"

    def test_monitor_page_offers_confirm_for_awaiting_confirmation(self, estimated_job):
        client, job_id = estimated_job
        page = client.get(f"/jobs/{job_id}")
        assert page.status_code == 200
        assert f'action="/jobs/{job_id}/confirm"' in page.text
        assert "Confirm" in page.text

        started = client.post(f"/jobs/{job_id}/confirm", follow_redirects=False)
        assert started.status_code == 303
        wait_terminal(client, job_id)
        page = client.get(f"/jobs/{job_id}")
        assert f'action="/jobs/{job_id}/confirm"' not in page.text  # terminal: gone

    def test_monitor_confirm_is_hidden_on_running_jobs(self, estimated_job):
        client, job_id = estimated_job
        client.post(f"/jobs/{job_id}/confirm", follow_redirects=False)
        payload = wait_terminal(client, job_id)
        assert payload["status"] in {"completed", "partial", "failed", "cancelled"}
        page = client.get(f"/jobs/{job_id}")
        assert f'action="/jobs/{job_id}/confirm"' not in page.text
