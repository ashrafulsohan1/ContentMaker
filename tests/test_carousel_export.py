"""Seams: carousel deliverables + the export API (BUILD_PLAN 3.4; spec §6.4).

- Carousel formats (instagram_carousel 2–10, linkedin_carousel 2–10,
  x_multi_image 2–4) are multi-image deliverables: the slide *set* is the
  deliverable a user uploads, so ``GET /api/jobs/{id}/export`` bundles it
  into one ZIP.
- The job plan records the format's image-count contract (min_images /
  max_images) beside the resolution; assembly refuses — loudly, naming the
  remedy — to emit a set the platform would reject: below the minimum (a
  partial job whose failed beats were skipped) or above the maximum.
  Single-image formats are deliberately NOT count-capped: every beat exports
  and the user picks (selection policy, DECISIONS D14).
- Over HTTP: 404 unknown job, 409 job not terminal yet, 404 nothing on disk
  to bundle.
"""

from __future__ import annotations

import io
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.assembly import AssemblyError
from app.assembly.pipeline import assemble_job, read_job_plan, write_job_plan
from app.core.jobs import Asset, AssetSpec, Job
from app.core.projects import create_project
from app.engines.base import ImageRequest
from app.engines.mocks import MockImageProvider
from app.main import create_app

CAROUSEL_OPTIONS = {
    "platform_id": "instagram",
    "format_id": "instagram_carousel",
    "audio": "on",
    "visual": "images",
    "style": "flat vector illustration, warm palette, thick outlines",
    "dry_run": "on",
}
X_MULTI_OPTIONS = {
    "platform_id": "x",
    "format_id": "x_multi_image",
    "audio": "on",
    "visual": "images",
    "dry_run": "on",
}
SCRIPT = "Opening beat with a couple of words.\n\nClosing beat with more words here.\n"
THREE_BEAT_SCRIPT = (
    "First carousel slide with a hook.\n\n"
    "Second slide carries the argument.\n\n"
    "Third slide closes with the call to action.\n"
)


def make_client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(projects_root=tmp_path / "projects"))


def run_job_to_terminal(
    client: TestClient, options: dict[str, str], *, script: str = SCRIPT
) -> tuple[str, str]:
    """Script → options → confirm → terminal; returns (job_id, project_slug)."""
    resp = client.post("/input", data={"project": "demo", "script": script}, follow_redirects=False)
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


# --- plan: the format's image-count contract travels with the job -------------


class TestPlanImageCount:
    def test_plan_roundtrips_min_max_images(self, tmp_path: Path):
        write_job_plan(
            tmp_path,
            job_id="job_x",
            platform_id="instagram",
            format_id="instagram_carousel",
            kind="carousel",
            width=1080,
            height=1350,
            container="jpg",
            fps=30,
            min_images=2,
            max_images=10,
        )
        plan = read_job_plan(tmp_path, "job_x")
        assert plan is not None
        assert (plan.min_images, plan.max_images) == (2, 10)

    def test_pre_3_4_plan_file_reads_with_legacy_defaults(self, tmp_path: Path):
        """Plans written before 3.4 carry no count keys: min 1, uncapped max."""
        (tmp_path / "plan").mkdir()
        (tmp_path / "plan" / "job_old_plan.json").write_text(
            '{"job_id": "job_old", "platform_id": "x", "format_id": "x_multi_image",'
            ' "kind": "carousel", "width": 1200, "height": 675, "container": "jpg",'
            ' "fps": 30}',
            encoding="utf-8",
        )
        plan = read_job_plan(tmp_path, "job_old")
        assert plan is not None
        assert plan.min_images == 1 and plan.max_images == 0  # 0 = uncapped

    def test_options_step_records_the_chosen_formats_contract(self, tmp_path: Path):
        client = make_client(tmp_path)
        job_id, _ = run_job_to_terminal(client, CAROUSEL_OPTIONS)
        wait_terminal(client, job_id)
        plan = read_job_plan(tmp_path / "projects" / "demo", job_id)
        assert plan is not None
        assert (plan.min_images, plan.max_images) == (2, 10)  # platforms.json IG carousel


# --- assembly: a carousel below minimum / above maximum is never emitted -------


def _visual_asset(root: Path, index: int, *, status: str = "done") -> Asset:
    path = root / "visuals" / f"beat_{index:02d}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    MockImageProvider().generate(
        ImageRequest(
            prompt=f"slide {index}",
            style_hint=None,
            seed=index,
            width=320,
            height=400,
            out_path=path,
        )
    )
    return Asset(
        id=index,
        job_id="job_x",
        kind="image",
        beat_index=index,
        params={"prompt": f"slide {index}", "width": 320, "height": 400},
        out_path=path,
        status=status,
    )


def _carousel_job(root: Path, beats: int, *, failed: int = 0) -> Job:
    """A visual-only job with ``beats`` slides, the last ``failed`` of them failed."""
    assets = [
        _visual_asset(root, index, status="failed" if index > beats - failed else "done")
        for index in range(1, beats + 1)
    ]
    return Job(
        id="job_x",
        project=root.name,
        status="running",
        created_at="t",
        updated_at="t",
        assets=tuple(assets),
    )


class TestCarouselCountEnforcement:
    def _plan(self, root: Path, *, min_images: int, max_images: int) -> None:
        write_job_plan(
            root,
            job_id="job_x",
            platform_id="instagram",
            format_id="instagram_carousel",
            kind="carousel",
            width=320,
            height=400,
            container="jpg",
            fps=30,
            min_images=min_images,
            max_images=max_images,
        )

    def test_partial_carousel_below_minimum_refuses_to_assemble(self, tmp_path: Path):
        """1 of 2 slides done (min 2): no invalid single-slide set is shipped."""
        root = create_project("demo", root=tmp_path / "projects").path
        job = _carousel_job(root, 2, failed=1)
        self._plan(root, min_images=2, max_images=10)
        with pytest.raises(AssemblyError, match="at least 2"):
            assemble_job(root, job)
        assert not (root / "output" / "instagram_carousel_beat_01.jpg").exists()

    def test_carousel_at_minimum_assembles(self, tmp_path: Path):
        root = create_project("demo", root=tmp_path / "projects").path
        job = _carousel_job(root, 2)
        self._plan(root, min_images=2, max_images=10)
        result = assemble_job(root, job)
        assert [p.name for p in result.deliverables] == [
            "instagram_carousel_beat_01.jpg",
            "instagram_carousel_beat_02.jpg",
        ]

    def test_carousel_above_maximum_refuses(self, tmp_path: Path):
        root = create_project("demo", root=tmp_path / "projects").path
        job = _carousel_job(root, 3)
        self._plan(root, min_images=2, max_images=2)
        with pytest.raises(AssemblyError, match="at most 2"):
            assemble_job(root, job)

    def test_single_image_format_is_not_count_capped(self, tmp_path: Path):
        """D14: image formats export every beat so the user picks — max never bites."""
        root = create_project("demo", root=tmp_path / "projects").path
        job = _carousel_job(root, 3)
        write_job_plan(
            root,
            job_id="job_x",
            platform_id="instagram",
            format_id="instagram_feed_square",
            kind="image",
            width=320,
            height=400,
            container="jpg",
            fps=30,
            min_images=1,
            max_images=1,
        )
        result = assemble_job(root, job)
        assert [p.name for p in result.deliverables] == [
            "instagram_feed_square_beat_01.jpg",
            "instagram_feed_square_beat_02.jpg",
            "instagram_feed_square_beat_03.jpg",
        ]


# --- export API: the slide set as one ZIP download ------------------------------


class TestExportEndpoint:
    def test_exports_carousel_set_as_zip(self, tmp_path: Path):
        client = make_client(tmp_path)
        job_id, _ = run_job_to_terminal(client, CAROUSEL_OPTIONS, script=THREE_BEAT_SCRIPT)
        payload = wait_terminal(client, job_id)
        assert payload["status"] == "completed", payload["error"]
        resp = client.get(f"/api/jobs/{job_id}/export")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"
        assert (
            resp.headers["content-disposition"] == 'attachment; filename="instagram_carousel.zip"'
        )
        bundle = zipfile.ZipFile(io.BytesIO(resp.content))
        out_dir = tmp_path / "projects" / "demo" / "output"
        assert bundle.namelist() == [
            "instagram_carousel_beat_01.jpg",
            "instagram_carousel_beat_02.jpg",
            "instagram_carousel_beat_03.jpg",
        ]
        for name in bundle.namelist():
            assert bundle.read(name) == (out_dir / name).read_bytes()

    def test_export_bundle_named_after_job_without_plan(self, tmp_path: Path):
        """Plan-less (engine-direct) jobs export too, bundled under the job id."""
        app = create_app(projects_root=tmp_path / "projects")
        client = TestClient(app)
        project = create_project("demo", root=tmp_path / "projects")
        engine = app.state.jobs
        spec = AssetSpec(
            kind="image",
            beat_index=1,
            out_path=project.subdir("visuals") / "beat_01.png",
            params={"prompt": "p", "style_hint": None, "width": 320, "height": 400},
        )
        job = engine.create_job(project.slug, [spec], providers={"image": "mock"})
        engine.estimate(job.id)
        engine.start(job.id)
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            if client.get(f"/api/jobs/{job.id}").json()["status"] in {
                "completed",
                "partial",
                "failed",
                "cancelled",
            }:
                break
            time.sleep(0.05)
        resp = client.get(f"/api/jobs/{job.id}/export")
        assert resp.status_code == 200
        assert f'filename="{job.id}.zip"' in resp.headers["content-disposition"]
        bundle = zipfile.ZipFile(io.BytesIO(resp.content))
        assert bundle.namelist() == [f"{job.id}.mp4"]  # plan-less → video fallback

    def test_unknown_job_is_404(self, tmp_path: Path):
        client = make_client(tmp_path)
        assert client.get("/api/jobs/job_nope/export").status_code == 404

    def test_non_terminal_job_is_409(self, tmp_path: Path):
        client = make_client(tmp_path)
        resp = client.post(
            "/input", data={"project": "demo", "script": SCRIPT}, follow_redirects=False
        )
        assert resp.status_code == 303
        resp = client.post("/options", data=CAROUSEL_OPTIONS, follow_redirects=False)
        job_id = resp.headers["location"].rsplit("/", 1)[-1]  # awaiting_confirmation
        resp = client.get(f"/api/jobs/{job_id}/export")
        assert resp.status_code == 409
        assert "terminal" in resp.text

    def test_terminal_job_without_deliverables_is_404(self, tmp_path: Path):
        client = make_client(tmp_path)
        job_id, project = run_job_to_terminal(client, X_MULTI_OPTIONS)
        wait_terminal(client, job_id)
        out_dir = tmp_path / "projects" / project / "output"
        for file in out_dir.iterdir():
            file.unlink()
        resp = client.get(f"/api/jobs/{job_id}/export")
        assert resp.status_code == 404
        assert "no deliverables" in resp.text
