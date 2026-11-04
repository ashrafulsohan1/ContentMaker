"""Seam: preview/export over HTTP (BUILD_PLAN 1.12) — TestClient only.

Walks the tail of the spec §4 flow on the mocked engines: a confirmed job
finalizes → assembly ran with real ffmpeg (manifest in ``plan/``, deliverables
in ``output/``) → the monitor page shows a deliverables section with inline
preview links, ``GET /api/jobs/{id}/deliverables`` reports the files as JSON,
``GET /projects/{p}/output/{file}`` serves them (traversal-proof), and
``POST /jobs/{id}/assemble`` re-runs assembly on a terminal job without
touching assets (regenerate-then-export, §6.6/§8).
"""
from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.assembly.pipeline import read_job_plan
from app.main import create_app

VIDEO_OPTIONS = {
    "platform_id": "youtube",
    "format_id": "youtube_shorts",
    "audio": "on",
    "visual": "images",
    "dry_run": "on",
}
IMAGE_OPTIONS = {
    "platform_id": "instagram",
    "format_id": "instagram_feed_square",
    "audio": "on",
    "visual": "images",
    "dry_run": "on",
}
SCRIPT = "Opening beat with a couple of words.\n\nClosing beat with more words here.\n"


def make_client(tmp_path: Path) -> TestClient:
    app = create_app(projects_root=tmp_path / "projects")
    return TestClient(app)


def run_job_to_terminal(client: TestClient, options: dict[str, str]) -> tuple[str, str]:
    """Script → options → confirm → terminal; returns (job_id, project_slug)."""
    resp = client.post("/input", data={"project": "demo", "script": SCRIPT},
                       follow_redirects=False)
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


def test_full_flow_assembles_real_deliverable(tmp_path: Path):
    client = make_client(tmp_path)
    job_id, project = run_job_to_terminal(client, VIDEO_OPTIONS)
    payload = wait_terminal(client, job_id)
    assert payload["status"] == "completed", payload["error"]

    root = tmp_path / "projects" / project
    mp4 = root / "output" / "youtube_shorts.mp4"
    assert mp4.is_file() and mp4.stat().st_size > 0
    manifest_path = root / "plan" / f"{job_id}_manifest.json"
    assert manifest_path.is_file()
    assert '"job_id"' in manifest_path.read_text(encoding="utf-8")


def test_monitor_page_shows_deliverables_with_preview_links(tmp_path: Path):
    client = make_client(tmp_path)
    job_id, _ = run_job_to_terminal(client, VIDEO_OPTIONS)
    wait_terminal(client, job_id)
    page = client.get(f"/jobs/{job_id}")
    assert page.status_code == 200
    assert "Deliverables" in page.text
    assert 'href="/projects/demo/output/youtube_shorts.mp4"' in page.text
    assert "<video controls" in page.text  # inline preview for the mp4
    assert "Re-assemble deliverables" in page.text


def test_deliverables_json_lists_output_files(tmp_path: Path):
    client = make_client(tmp_path)
    job_id, _ = run_job_to_terminal(client, VIDEO_OPTIONS)
    wait_terminal(client, job_id)
    resp = client.get(f"/api/jobs/{job_id}/deliverables")
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == job_id and body["project"] == "demo"
    names = [d["name"] for d in body["deliverables"]]
    assert names == ["youtube_shorts.mp4"]
    item = body["deliverables"][0]
    assert item["media_type"] == "video/mp4" and item["kind"] == "video"
    assert item["url"] == "/projects/demo/output/youtube_shorts.mp4"
    assert item["size_bytes"] > 0


def test_deliverables_json_unknown_job_is_404(tmp_path: Path):
    client = make_client(tmp_path)
    assert client.get("/api/jobs/job_nope/deliverables").status_code == 404


def test_output_file_is_served_with_media_type(tmp_path: Path):
    client = make_client(tmp_path)
    job_id, _ = run_job_to_terminal(client, VIDEO_OPTIONS)
    wait_terminal(client, job_id)
    resp = client.get("/projects/demo/output/youtube_shorts.mp4")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("video/mp4")
    assert resp.content.startswith(b"\x00\x00\x00")  # mp4 box header


def test_image_format_flow_exports_platform_jpegs(tmp_path: Path):
    client = make_client(tmp_path)
    job_id, project = run_job_to_terminal(client, IMAGE_OPTIONS)
    payload = wait_terminal(client, job_id)
    assert payload["status"] == "completed", payload["error"]
    listing = client.get(f"/api/jobs/{job_id}/deliverables").json()
    names = [d["name"] for d in listing["deliverables"]]
    assert names == ["instagram_feed_square_beat_01.jpg", "instagram_feed_square_beat_02.jpg"]
    assert all(d["kind"] == "image" for d in listing["deliverables"])
    resp = client.get("/projects/demo/output/instagram_feed_square_beat_01.jpg")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/jpeg")
    page = client.get(f"/jobs/{job_id}")
    assert "<img" in page.text  # inline image preview


def test_output_endpoint_404_for_unknown_file(tmp_path: Path):
    client = make_client(tmp_path)
    run_job_to_terminal(client, VIDEO_OPTIONS)
    assert client.get("/projects/demo/output/nope.mp4").status_code == 404


def test_output_endpoint_blocks_traversal(tmp_path: Path):
    client = make_client(tmp_path)
    run_job_to_terminal(client, VIDEO_OPTIONS)
    for evil in ("..%2F..%2Fstate%2Fjob.db", "..%2Fscript%2Fscript.txt", "../../state/job.db"):
        resp = client.get(f"/projects/demo/output/{evil}")
        assert resp.status_code == 404, evil
    assert client.get("/projects/demo/output").status_code in {404, 200}  # never a dir listing leak
    assert client.get("/projects/..%2Fetc/output/passwd").status_code == 404


def test_plan_file_written_from_chosen_format(tmp_path: Path):
    client = make_client(tmp_path)
    resp = client.post("/input", data={"project": "demo", "script": SCRIPT},
                       follow_redirects=False)
    assert resp.status_code == 303
    resp = client.post("/options", data=VIDEO_OPTIONS, follow_redirects=False)
    job_id = resp.headers["location"].rsplit("/", 1)[-1]
    plan = read_job_plan(tmp_path / "projects" / "demo", job_id)
    assert plan is not None
    assert (plan.platform_id, plan.format_id, plan.kind) == ("youtube", "youtube_shorts", "video")
    assert (plan.width, plan.height) == (1080, 1920)
    assert plan.container == "mp4"


def test_assemble_endpoint_reruns_on_terminal_job(tmp_path: Path):
    client = make_client(tmp_path)
    job_id, project = run_job_to_terminal(client, VIDEO_OPTIONS)
    wait_terminal(client, job_id)
    mp4 = tmp_path / "projects" / project / "output" / "youtube_shorts.mp4"
    before = mp4.stat().st_mtime_ns
    time.sleep(0.02)
    resp = client.post(f"/jobs/{job_id}/assemble", follow_redirects=False)
    assert resp.status_code == 303  # plain form: redirect back to the monitor
    assert resp.headers["location"] == f"/jobs/{job_id}"
    assert mp4.stat().st_mtime_ns != before  # deliverables actually rebuilt


def test_assemble_endpoint_htmx_returns_monitor_fragment(tmp_path: Path):
    client = make_client(tmp_path)
    job_id, _ = run_job_to_terminal(client, VIDEO_OPTIONS)
    wait_terminal(client, job_id)
    resp = client.post(f"/jobs/{job_id}/assemble", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert 'id="monitor"' in resp.text
    assert "Deliverables" in resp.text
    assert "re-assembled" in resp.text  # success notice in the swapped fragment


def test_assemble_endpoint_rejects_non_terminal_job(tmp_path: Path):
    client = make_client(tmp_path)
    resp = client.post("/input", data={"project": "demo", "script": SCRIPT},
                       follow_redirects=False)
    assert resp.status_code == 303
    resp = client.post("/options", data=VIDEO_OPTIONS, follow_redirects=False)
    job_id = resp.headers["location"].rsplit("/", 1)[-1]  # awaiting_confirmation
    resp = client.post(f"/jobs/{job_id}/assemble", follow_redirects=False)
    assert resp.status_code == 409
    assert "terminal" in resp.text


def test_assemble_endpoint_404_for_unknown_job(tmp_path: Path):
    client = make_client(tmp_path)
    assert client.post("/jobs/job_nope/assemble", follow_redirects=False).status_code == 404


def test_monitor_hides_assemble_button_until_terminal(tmp_path: Path):
    client = make_client(tmp_path)
    resp = client.post("/input", data={"project": "demo", "script": SCRIPT},
                       follow_redirects=False)
    assert resp.status_code == 303
    resp = client.post("/options", data=VIDEO_OPTIONS, follow_redirects=False)
    job_id = resp.headers["location"].rsplit("/", 1)[-1]
    page = client.get(f"/confirm/{job_id}")
    assert "Re-assemble" not in page.text
    client.post(f"/jobs/{job_id}/confirm")
    wait_terminal(client, job_id)
    assert "Re-assemble deliverables" in client.get(f"/jobs/{job_id}").text
