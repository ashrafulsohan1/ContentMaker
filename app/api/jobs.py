"""JSON API: job status + per-asset retry (BUILD_PLAN 1.10, extended by 1.11/1.12; 4.4).

The UI, the SSE monitor and any API client read job state here instead of
scraping HTML. Flow mutations (estimate / confirm / cancel) are driven by
the web flow; this router reports what is persisted plus the derived per-job
progress and (1.12) the deliverables local assembly wrote into the project's
``output/`` folder. Since 4.4 it also carries the one generation mutation
API clients need: ``POST /jobs/{id}/assets/{asset_id}/retry`` re-queues a
failed|skipped asset on the real provider without redoing the paid ones
(§6.6) — the same engine path the monitor's retry button drives.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.assembly.pipeline import list_deliverables
from app.core.jobs import AssetNotFound, Job, JobEngine, JobNotFound

router = APIRouter(prefix="/api")


def job_progress(job: Job) -> dict[str, int]:
    """Overall monitor progress: done assets / total assets, as a percent."""
    total = len(job.assets)
    done = sum(1 for asset in job.assets if asset.status == "done")
    percent = round(100 * done / total) if total else 0
    return {"total": total, "done": done, "percent": percent}


def job_payload(job: Job) -> dict[str, Any]:
    """One job as JSON: lifecycle fields plus the per-asset rows + progress."""
    return {
        "id": job.id,
        "project": job.project,
        "status": job.status,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "providers": job.providers,
        "spend_cap_usd": job.spend_cap_usd,
        "spent_usd": job.spent_usd,
        "estimate_usd": job.estimate_usd,
        "error": job.error,
        "progress": job_progress(job),
        "assets": [
            {
                "id": asset.id,
                "kind": asset.kind,
                "beat_index": asset.beat_index,
                "status": asset.status,
                "attempts": asset.attempts,
                "params": asset.params,
                "out_path": str(asset.out_path),
                "estimated_cost_usd": asset.estimated_cost_usd,
                "spent_usd": asset.spent_usd,
                "error": asset.error,
            }
            for asset in job.assets
        ],
    }


def engine_of(request: Request) -> JobEngine:
    return request.app.state.jobs


@router.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request) -> dict[str, Any]:
    try:
        job = engine_of(request).job(job_id)
    except JobNotFound:
        raise HTTPException(status_code=404, detail=f"no job with id '{job_id}'") from None
    return job_payload(job)


@router.post("/jobs/{job_id}/assets/{asset_id}/retry")
def retry_asset(job_id: str, asset_id: int, request: Request) -> dict[str, Any]:
    """Re-queue one failed|skipped asset for generation (BUILD_PLAN 4.4, §6.6).

    The JSON mutation surface for the monitor's per-asset retry button: the
    job engine re-runs exactly this asset on its provider — completed assets
    are never re-generated, their spend is not re-recorded, and a terminal
    job is re-opened through the sanctioned ``reopen_for_retry`` move to pick
    the asset up. Errors answer JSON: 404 unknown job/asset; 409 when the
    asset is not failed|skipped, belongs to another job, or the job is
    cancelled (a cancelled job stays final).
    """
    engine = engine_of(request)
    try:
        job = engine.retry_asset(job_id, asset_id)
    except JobNotFound:
        raise HTTPException(status_code=404, detail=f"no job with id '{job_id}'") from None
    except AssetNotFound:
        raise HTTPException(
            status_code=404, detail=f"no asset with id {asset_id} in job '{job_id}'"
        ) from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return job_payload(job)


@router.get("/projects/{project}/jobs")
def list_project_jobs(project: str, request: Request) -> list[dict[str, Any]]:
    engine = engine_of(request)
    return [job_payload(job) for job in engine.list_jobs(project)]


@router.get("/jobs/{job_id}/deliverables")
def get_job_deliverables(job_id: str, request: Request) -> dict[str, Any]:
    """The deliverables local assembly produced for this job (BUILD_PLAN 1.12).

    Each entry names the file in the project's ``output/`` folder with its
    preview URL, size and media kind; the list is read from disk per request,
    so it reflects the latest assembly run (finalize or POST …/assemble).
    """
    engine = engine_of(request)
    try:
        job = engine.job(job_id)
    except JobNotFound:
        raise HTTPException(status_code=404, detail=f"no job with id '{job_id}'") from None
    project_dir = request.app.state.projects_root / job.project
    return {
        "job_id": job.id,
        "project": job.project,
        "manifest_file": f"{job.id}_manifest.json",
        "deliverables": [
            {
                "name": item.name,
                "url": f"/projects/{job.project}/output/{item.name}",
                "size_bytes": item.size_bytes,
                "media_type": item.media_type,
                "kind": item.kind,
            }
            for item in list_deliverables(project_dir, job)
        ],
    }
