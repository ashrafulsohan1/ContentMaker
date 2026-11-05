"""Fixture: 3-beat visual-consistency sample run (BUILD_PLAN 3.5; spec §6.3).

Visual consistency is a hard requirement (§6.3): every visual in one piece
must share a coherent style — "independent, uncoordinated generation calls
are not acceptable output". The mechanisms live in the provider layer (task
3.1: the fixed ``<style>`` block prepended to every beat prompt + the
beat-1 reference anchor re-attached on every later beat —
research/gemini-image.md §4, contract-pinned offline in
``tests/test_gemini_image.py``). What a zero-network fixture can and must
prove is that the *wiring* survives the whole flow: the full 3-beat sample
script runs through input → options → confirm → generation → assembly on the
mock providers with real ffmpeg and lands as the platform-correct carousel
set, with one identical style block and identical platform dimensions
recorded on every beat's generation params (§6.3 coordination) and per-beat
prompts that stay distinct (the things anchoring coordinates).

**Real-provider variant (documented; needs a Gemini key, ~3 images of
spend).** Pixels cannot be judged on mocks — the visual style-regression
review is a human pass on real output:

1. Settings → add and test the Gemini key (BYOK, §6.5).
2. Input the 3-beat sample script (``SAMPLE_SCRIPT`` below); pick
   Instagram → Carousel; visual type images; a concrete style line (e.g.
   "flat vector illustration, warm palette, thick outlines"); dry-run OFF.
3. Confirm the estimate; wait for completion.
4. Open ``output/instagram_carousel_beat_01..03.jpg`` side by side and
   review: same medium/palette/lighting across all three; beats 2–3 should
   read as re-renders of beat 1's style (the anchor instruction,
   ``app/providers/gemini/image.py`` ANCHOR_INSTRUCTION).
5. Record the outcome (pass / what drifted) in PROJECT_LOG.md.

Until that review runs on real keys, this fixture is the consistency gate of
record for the M3 milestone wording "visual-consistency check passes".
"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.assembly.ffmpeg import FFmpeg
from app.assembly.pipeline import read_job_plan
from app.main import create_app

#: The 3-beat sample run of record (BUILD_PLAN 3.5). Distinct subjects per
#: slide — exactly the independent-content/durable-style split anchoring exists
#: for — reused verbatim by the documented real-key review above.
SAMPLE_SCRIPT = (
    "Slide one introduces the topic with a bold statement.\n\n"
    "Slide two explains the mechanism in plain words.\n\n"
    "Slide three closes with the call to action.\n"
)
STYLE = "flat vector illustration, warm palette, thick outlines"
CAROUSEL_OPTIONS = {
    "platform_id": "instagram",
    "format_id": "instagram_carousel",
    "audio": "on",
    "visual": "images",
    "style": STYLE,
    "dry_run": "on",
}


def make_client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(projects_root=tmp_path / "projects"))


def run_job_to_terminal(client: TestClient, options: dict[str, str]) -> tuple[str, str]:
    """Script → options → confirm → terminal; returns (job_id, project_slug)."""
    resp = client.post(
        "/input", data={"project": "demo", "script": SAMPLE_SCRIPT}, follow_redirects=False
    )
    assert resp.status_code == 303, resp.text
    resp = client.post("/options", data=options, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    job_id = resp.headers["location"].rsplit("/", 1)[-1]
    assert client.post(f"/jobs/{job_id}/confirm", follow_redirects=False).status_code == 303
    return job_id, "demo"


def wait_terminal(client: TestClient, job_id: str, timeout_s: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout_s
    payload: dict = {}
    while time.monotonic() < deadline:
        payload = client.get(f"/api/jobs/{job_id}").json()
        if payload["status"] in {"completed", "partial", "failed", "cancelled"}:
            return payload
        time.sleep(0.05)
    raise AssertionError(f"job never reached terminal status: {payload}")


def test_three_beat_sample_assembles_the_platform_carousel_set(tmp_path: Path):
    """The fixture run of record: 3 beats → 3 exact 1080×1350 JPEG slides."""
    client = make_client(tmp_path)
    job_id, project = run_job_to_terminal(client, CAROUSEL_OPTIONS)
    payload = wait_terminal(client, job_id)
    assert payload["status"] == "completed", payload["error"]

    root = tmp_path / "projects" / project
    expected = [root / "output" / f"instagram_carousel_beat_{index:02d}.jpg" for index in (1, 2, 3)]
    for slide in expected:
        assert slide.is_file() and slide.stat().st_size > 0, slide

    # Platform-correct: exact instagram_carousel resolution, JPEG container.
    ff = FFmpeg()
    for slide in expected:
        streams = ff.probe(slide)["streams"]
        video = next(s for s in streams if s.get("codec_type") == "video")
        assert (video["codec_name"], video["width"], video["height"]) == (
            "mjpeg",
            1080,
            1350,
        )

    # The manifest of record covers exactly the 3 beats (§6.2).
    manifest_path = root / "plan" / f"{job_id}_manifest.json"
    assert manifest_path.is_file()
    assert f'"{job_id}"' in manifest_path.read_text(encoding="utf-8")

    # The plan mirrors the chosen carousel format (its count contract included).
    plan = read_job_plan(root, job_id)
    assert plan is not None
    assert (plan.kind, plan.container) == ("carousel", "jpg")
    assert (plan.width, plan.height) == (1080, 1350)
    assert (plan.min_images, plan.max_images) == (2, 10)


def test_style_block_and_dimensions_identical_across_all_three_beats(tmp_path: Path):
    """§6.3 coordination, asserted at the generation-request seam: one style
    block, one pair of platform dims on every beat; subjects stay distinct."""
    client = make_client(tmp_path)
    job_id, _ = run_job_to_terminal(client, CAROUSEL_OPTIONS)
    payload = wait_terminal(client, job_id)
    assert payload["status"] == "completed", payload["error"]

    slides = [a for a in payload["assets"] if a["kind"] == "image"]
    slides.sort(key=lambda a: a["beat_index"])
    assert [a["beat_index"] for a in slides] == [1, 2, 3]

    style_hints = {a["params"]["style_hint"] for a in slides}
    assert style_hints == {STYLE}  # the fixed style block reaches every beat

    dims = {(a["params"]["width"], a["params"]["height"]) for a in slides}
    assert dims == {(1080, 1350)}  # platform-correct dimensions on every slide

    prompts = [a["params"]["prompt"] for a in slides]
    assert len(set(prompts)) == 3  # distinct subjects — anchoring coordinates them
