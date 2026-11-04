"""JSON API: read-only job status (BUILD_PLAN 1.10, extended by 1.11).

The UI, the SSE monitor and any API client read job state here instead of
scraping HTML. Mutations (estimate / confirm / cancel / retry) are driven by
the web flow and job engine; this router only reports what is persisted, plus
the derived per-job progress the monitor displays.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.core.jobs import Job, JobEngine, JobNotFound

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


@router.get("/projects/{project}/jobs")
def list_project_jobs(project: str, request: Request) -> list[dict[str, Any]]:
    engine = engine_of(request)
    return [job_payload(job) for job in engine.list_jobs(project)]
