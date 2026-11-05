"""JSON API: deliverable export (BUILD_PLAN 3.4; spec §6.4).

``GET /api/jobs/{job_id}/export`` bundles the job's deliverable set into one
ZIP download. For a carousel (Instagram / LinkedIn / X multi-image) the slide
*set* — not any single file — is what gets uploaded, so the bundle is the
export surface; single-image formats keep their per-beat file links as the
picker (every beat exports; selection policy in DECISIONS D14).

Files are read from ``output/`` per request — the same source of truth as the
deliverables listing — so the bundle always reflects the latest assembly run
(finalize or ``POST /jobs/{id}/assemble``). Errors say exactly what's wrong
(§6.5): unknown job → 404, a job that has not reached a terminal status →
409, a terminal job with nothing on disk → 404.
"""

from __future__ import annotations

import io
import zipfile

from fastapi import APIRouter, HTTPException, Request, Response

from app.assembly.pipeline import list_deliverables, read_job_plan
from app.core.jobs import TERMINAL_JOB_STATUSES, JobEngine, JobNotFound

router = APIRouter(prefix="/api")


def engine_of(request: Request) -> JobEngine:
    return request.app.state.jobs


@router.get("/jobs/{job_id}/export")
def export_job_deliverables(job_id: str, request: Request) -> Response:
    """The job's deliverables as one ZIP attachment (BUILD_PLAN 3.4)."""
    try:
        job = engine_of(request).job(job_id)
    except JobNotFound:
        raise HTTPException(status_code=404, detail=f"no job with id '{job_id}'") from None
    if job.status not in TERMINAL_JOB_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"job '{job_id}' must be terminal to export (now '{job.status}')"
            " — wait for it to finish, or re-assemble it first",
        )
    project_dir = request.app.state.projects_root / job.project
    items = list_deliverables(project_dir, job)
    if not items:
        raise HTTPException(
            status_code=404,
            detail=f"job '{job_id}' has no deliverables to export — run assembly first"
            f" (POST /jobs/{job_id}/assemble)",
        )
    plan = read_job_plan(project_dir, job.id)
    base = plan.format_id if plan else job.id
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as bundle:
        # ZIP_STORED: the members are JPEG/PNG/MP4 — already compressed, so
        # deflating them would spend CPU for nothing on a local app.
        for item in items:
            bundle.write(item.path, arcname=item.name)
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{base}.zip"'},
    )
