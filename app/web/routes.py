"""UI flow routes: input → review → options → confirm → generate (BUILD_PLAN 1.10).

Server-rendered htmx (no build step): every POST works as a plain form
submission (303 redirect back to the step), and htmx requests (``HX-Request``
header) get the updated HTML fragment instead — progressive enhancement, so
the flow stays usable with scripts disabled. Wizard drafts live in
``FlowStore`` keyed by the ``cm_flow`` cookie; jobs themselves are persisted
by the job engine (§6.6).

The estimate + explicit confirm gate (§6.6) is structural: ``POST /options``
creates the job in ``draft`` and immediately estimates it
(``awaiting_confirmation``); only ``POST /jobs/{id}/confirm`` starts it.

The job monitor (§8, task 1.11) renders per-asset status rows, overall
progress, running spend, a cancel button and per-asset retry buttons — always
from the persisted state, so a plain reload reattaches to the running job; with
JS enabled, ``/static/monitor.js`` streams the SSE feed (``app.api.sse``).
"""

from __future__ import annotations

import html
import math
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.datastructures import FormData, UploadFile

from app.api.jobs import engine_of, job_progress
from app.assembly.pipeline import list_deliverables, media_type_for, write_job_plan
from app.core.jobs import (
    TERMINAL_JOB_STATUSES,
    AssetNotFound,
    IllegalTransition,
    Job,
    JobEngine,
    JobNotFound,
)
from app.core.projects import ProjectError, create_project
from app.data.presets import MAX_PACE, MIN_PACE, PresetError, load_presets
from app.data.specs import FormatSpec, load_platforms, lookup_format
from app.engines.registry import ENGINES, Registry
from app.scripting.manifest import DEFAULT_FPS
from app.web.flow import (
    PAGE_SIZE,
    FlowError,
    FlowOptions,
    FlowState,
    FlowStore,
    beats_from_script,
    build_assets,
    engine_availability,
    engines_needed,
    merge_with_next,
    page_slice,
    resolve_providers,
    split_beat,
    update_beat,
    validate_options,
)

router = APIRouter()

WEB_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=WEB_DIR / "templates")

FLOW_COOKIE = "cm_flow"


# --- helpers -----------------------------------------------------------------


def _flow(request: Request) -> FlowState | None:
    store: FlowStore = request.app.state.flow_store
    return store.get(request.cookies.get(FLOW_COOKIE))


def _redirect_home(message: str) -> RedirectResponse:
    return RedirectResponse(f"/?error={quote(message)}", status_code=303)


def _usd(amount: float | None) -> str:
    return f"${(amount or 0.0):.4f}"


def _formats_for(platform_id: str, platforms: list) -> list[FormatSpec]:
    for platform in platforms:
        if platform.id == platform_id:
            return list(platform.formats)
    return []


def _beat_response(
    request: Request,
    flow: FlowState,
    beat_id: str,
    *,
    notice: str | None = None,
    error: str | None = None,
):
    """Redirect back to the beat's page — or swap the fragment for htmx."""
    try:
        page = flow.position(beat_id) // PAGE_SIZE + 1
    except FlowError:
        page = 1
    if request.headers.get("HX-Request"):
        return _render_beat_list(request, flow, page, notice=notice, error=error)
    params: dict[str, str] = {"page": str(page)}
    if notice:
        params["notice"] = notice
    if error:
        params["error"] = error
    return RedirectResponse(f"/review?{urlencode(params)}", status_code=303)


def _render_beat_list(
    request: Request,
    flow: FlowState,
    page: int,
    *,
    notice: str | None = None,
    error: str | None = None,
):
    page_beats, current, total = page_slice(flow.beats, page)
    return templates.TemplateResponse(
        request,
        "_beat_list.html",
        {
            "beats": page_beats,
            "offset": (current - 1) * PAGE_SIZE,
            "page": current,
            "total_pages": total,
            "beats_total": len(flow.beats),
            "notice": notice,
            "error": error,
        },
    )


# --- step 1: input -------------------------------------------------------------


@router.get("/")
def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "error": request.query_params.get("error"),
            "notice": request.query_params.get("notice"),
        },
    )


@router.post("/input")
async def submit_input(request: Request):
    form = await request.form()
    name = str(form.get("project") or "").strip()
    pasted = str(form.get("script") or "")
    script = pasted
    upload = form.get("script_file")
    if isinstance(upload, UploadFile) and upload.filename:
        raw = await upload.read()
        script = raw.decode("utf-8", errors="replace").strip() or pasted
    if not name:
        return _render_index(request, "A project name is needed — it names the project folder.")
    if not script.strip():
        return _render_index(request, "Paste a script or upload a .txt / .md file.")
    beats = beats_from_script(script)
    if not beats:
        return _render_index(request, "The script parsed to zero beats — add some text.")
    try:
        project = create_project(name, root=request.app.state.projects_root)
    except ProjectError as exc:
        return _render_index(request, str(exc))
    # the original text is preserved verbatim under script/ (§7)
    (project.subdir("script") / "script.txt").write_text(script, encoding="utf-8")
    flow = request.app.state.flow_store.create(project, script, beats)
    response = RedirectResponse("/review", status_code=303)
    response.set_cookie(FLOW_COOKIE, flow.id, httponly=True, samesite="lax")
    return response


def _render_index(request: Request, error: str):
    return templates.TemplateResponse(request, "index.html", {"error": error})


# --- step 2: beat review ---------------------------------------------------------


@router.get("/review")
def review(request: Request):
    flow = _flow(request)
    if flow is None:
        return _redirect_home("Start by adding a script.")
    try:
        page = int(request.query_params.get("page", "1"))
    except ValueError:
        page = 1
    page_beats, current, total = page_slice(flow.beats, page)
    return templates.TemplateResponse(
        request,
        "review.html",
        {
            "flow": flow,
            "beats": page_beats,
            "offset": (current - 1) * PAGE_SIZE,
            "page": current,
            "total_pages": total,
            "beats_total": len(flow.beats),
            "notice": request.query_params.get("notice"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/review/beat/{beat_id}/edit")
async def edit_beat(beat_id: str, request: Request):
    flow = _flow(request)
    if flow is None:
        return _redirect_home("Start by adding a script.")
    form = await request.form()
    try:
        update_beat(
            flow,
            beat_id,
            text=str(form.get("text") or ""),
            label=str(form.get("label") or "") or None,
        )
    except FlowError as exc:
        return _beat_response(request, flow, beat_id, error=str(exc))
    return _beat_response(request, flow, beat_id)


@router.post("/review/beat/{beat_id}/merge")
def merge_beat(beat_id: str, request: Request):
    flow = _flow(request)
    if flow is None:
        return _redirect_home("Start by adding a script.")
    try:
        notice = merge_with_next(flow, beat_id)
    except FlowError as exc:
        return _beat_response(request, flow, beat_id, error=str(exc))
    return _beat_response(request, flow, beat_id, notice=notice)


@router.post("/review/beat/{beat_id}/split")
def split_beat_route(beat_id: str, request: Request):
    flow = _flow(request)
    if flow is None:
        return _redirect_home("Start by adding a script.")
    try:
        notice = split_beat(flow, beat_id)
    except FlowError as exc:
        return _beat_response(request, flow, beat_id, error=str(exc))
    return _beat_response(request, flow, beat_id, notice=notice)


# --- step 3: options --------------------------------------------------------------


@router.get("/options")
def options(request: Request):
    flow = _flow(request)
    if flow is None:
        return _redirect_home("Start by adding a script.")
    return _render_options(request, flow)


@router.get("/options/formats")
def formats_fragment(request: Request):
    platform_id = request.query_params.get("platform", "")
    formats = _formats_for(platform_id, load_platforms())
    flow = _flow(request)
    format_id = flow.options.format_id if flow and flow.options else ""
    return templates.TemplateResponse(
        request, "_formats.html", {"formats": formats, "format_id": format_id}
    )


@router.post("/options")
async def submit_options(request: Request):
    flow = _flow(request)
    if flow is None:
        return _redirect_home("Start by adding a script.")
    form = await request.form()
    options, errors = _parse_options(form)
    if errors:
        return _render_options(request, flow, errors=errors, sticky=options)
    try:
        format_spec = lookup_format(options.platform_id, options.format_id)
    except ValueError as exc:
        return _render_options(request, flow, errors=[str(exc)], sticky=options)
    errors.extend(
        validate_options(
            options,
            format_kind=format_spec.kind,
            min_images=format_spec.min_images,
            max_images=format_spec.max_images,
            n_beats=len(flow.beats),
            registry=request.app.state.registry,
        )
    )
    try:
        providers = resolve_providers(
            options, request.app.state.registry, request.app.state.key_store
        )
    except FlowError as exc:
        errors.append(str(exc))
    if errors:
        return _render_options(request, flow, errors=errors, sticky=options)

    assets = build_assets(flow, options, resolution=format_spec.resolution)
    engine: JobEngine = engine_of(request)
    job = engine.create_job(
        flow.project.slug,
        assets,
        providers=providers,
        spend_cap_usd=options.spend_cap_usd,
    )
    job = engine.estimate(job.id)
    if job.status == "failed":
        return _render_options(
            request, flow, errors=[job.error or "Cost estimation failed."], sticky=options
        )
    # Persist what assembly will need when the job finalizes (restart-safe,
    # §6.6): the chosen format's kind/resolution/container and its carousel
    # image-count contract live in plan/.
    write_job_plan(
        flow.project.path,
        job_id=job.id,
        platform_id=options.platform_id,
        format_id=format_spec.id,
        kind=format_spec.kind,
        width=format_spec.resolution[0],
        height=format_spec.resolution[1],
        container=format_spec.container,
        fps=DEFAULT_FPS,
        min_images=format_spec.min_images,
        max_images=format_spec.max_images,
    )
    flow.options = options
    flow.job_id = job.id
    return RedirectResponse(f"/confirm/{job.id}", status_code=303)


def _render_options(
    request: Request,
    flow: FlowState,
    *,
    errors: list[str] | None = None,
    sticky: FlowOptions | None = None,
):
    platforms = load_platforms()
    options = sticky or flow.options or _default_options(platforms)
    formats = _formats_for(options.platform_id, platforms)
    presets = load_presets()
    registry: Registry = request.app.state.registry
    selected_preset = next((p for p in presets if p.id == options.preset_id), None)
    return templates.TemplateResponse(
        request,
        "options.html",
        {
            "flow": flow,
            "platforms": platforms,
            "platform_id": options.platform_id,
            "formats": formats,
            "format_id": options.format_id,
            "presets": presets,
            "preset_id": options.preset_id,
            "selected_preset": selected_preset,
            "style": options.style,
            "audio": options.audio,
            "voice": options.voice,
            "pace": options.pace,
            "tone": options.tone,
            "visual": options.visual,
            "dry_run": options.dry_run,
            "spend_cap_usd": options.spend_cap_usd,
            "engines": list(ENGINES),
            "availability": engine_availability(registry, request.app.state.key_store),
            "needed": engines_needed(options),
            "errors": errors or [],
        },
    )


def _default_options(platforms: list) -> FlowOptions:
    first = platforms[0]
    fmt = first.formats[0]
    return FlowOptions(
        platform_id=first.id,
        format_id=fmt.id,
        audio=True,
        visual="video" if fmt.kind == "video" else "images",
        dry_run=True,
    )


def _parse_options(form: FormData) -> tuple[FlowOptions, list[str]]:
    errors: list[str] = []

    def checked(key: str) -> bool:
        return str(form.get(key) or "") == "on"

    pace_raw = str(form.get("pace") or "").strip()
    pace = 1.0
    if pace_raw:
        try:
            pace = float(pace_raw)
            # The same sanity band the UI input and presets.py enforce — a
            # direct form post must not smuggle an absurd pace into TTS params.
            if not math.isfinite(pace) or not MIN_PACE <= pace <= MAX_PACE:
                raise ValueError
        except ValueError:
            errors.append(f"Pace must be between {MIN_PACE} and {MAX_PACE} (1.0 is normal speed).")
            pace = 1.0
            pace_raw = ""

    cap_raw = str(form.get("spend_cap_usd") or "").strip()
    spend_cap: float | None = None
    if cap_raw:
        try:
            spend_cap = float(cap_raw)
        except ValueError:
            errors.append("The spend cap must be a number of US dollars.")
            spend_cap = None

    options = FlowOptions(
        platform_id=str(form.get("platform_id") or ""),
        format_id=str(form.get("format_id") or ""),
        preset_id=str(form.get("preset_id") or "") or None,
        audio=checked("audio"),
        visual=str(form.get("visual") or "none"),
        style=str(form.get("style") or "").strip() or None,
        voice=str(form.get("voice") or "").strip() or None,
        tone=str(form.get("tone") or "").strip() or None,
        pace=pace,
        dry_run=checked("dry_run"),
        spend_cap_usd=spend_cap,
    )
    options.providers = {
        engine: str(form.get(f"provider_{engine}") or "").strip() for engine in ENGINES
    }
    options.providers = {e: p for e, p in options.providers.items() if p}

    if options.preset_id:
        try:
            presets = load_presets()
        except PresetError as exc:
            errors.append(str(exc))
        else:
            preset = next((p for p in presets if p.id == options.preset_id), None)
            if preset is None:
                errors.append(f"Unknown preset {options.preset_id!r} — pick one from the list.")
            else:
                options.style = options.style or preset.visual_style
                if preset.voice:
                    options.voice = options.voice or preset.voice.voice
                    if not pace_raw:
                        options.pace = preset.voice.pace
                    options.tone = options.tone or preset.voice.tone
    return options, errors


# --- step 4: estimate + confirm → generate ------------------------------------------


@router.get("/confirm/{job_id}")
def confirm_page(job_id: str, request: Request):
    try:
        job = engine_of(request).job(job_id)
    except JobNotFound:
        raise HTTPException(status_code=404, detail=f"no job with id '{job_id}'") from None
    if job.status != "awaiting_confirmation":
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)
    flow = request.app.state.flow_store.find_by_job(job_id)
    options = flow.options if flow else None
    platform_label = format_label = ""
    if options is not None:
        try:
            fmt = lookup_format(options.platform_id, options.format_id)
            platform_label = options.platform_id
            format_label = fmt.label
        except ValueError:
            pass
    # The wizard flow is in-memory only — after a restart (or LRU eviction) the
    # persisted job must still be confirmable (§6.6 gate must not dead-end), so
    # every setting below falls back to what the job row itself records.
    if options is not None:
        audio_display = (
            "on — voice " + options.voice
            if options.audio and options.voice
            else ("on" if options.audio else "off")
        )
        visual_display = options.visual
        style_display = options.style
    else:
        kinds = {asset.kind for asset in job.assets}
        audio_display = "on" if "tts" in kinds else "off"
        visual_display = "video" if "video" in kinds else ("images" if "image" in kinds else "none")
        style_display = None
    dry_run = (
        options.dry_run
        if options is not None
        else bool(job.providers) and all(p == "mock" for p in job.providers.values())
    )
    rows = [
        {"kind": asset.kind, "beat": asset.beat_index, "cost": _usd(asset.estimated_cost_usd)}
        for asset in job.assets
    ]
    provider_pairs = [
        {"engine": engine, "provider": provider}
        for engine, provider in sorted(job.providers.items())
    ]
    platform_label = platform_label or (options.platform_id if options else "")
    return templates.TemplateResponse(
        request,
        "confirm.html",
        {
            "job": job,
            "flow": flow,
            "beats_count": len(flow.beats) if flow else len(job.assets),
            "platform_label": platform_label,
            "format_label": format_label,
            "audio_display": audio_display,
            "visual_display": visual_display,
            "style_display": style_display,
            "rows": rows,
            "total": _usd(job.estimate_usd),
            "cap_display": "no cap" if job.spend_cap_usd is None else _usd(job.spend_cap_usd),
            "dry_run": dry_run,
            "provider_pairs": provider_pairs,
        },
    )


@router.post("/jobs/{job_id}/confirm")
def confirm_job(job_id: str, request: Request):
    engine = engine_of(request)
    try:
        engine.start(job_id)
    except JobNotFound:
        raise HTTPException(status_code=404, detail=f"no job with id '{job_id}'") from None
    except IllegalTransition as exc:
        return HTMLResponse(
            f"<h1>Cannot start job</h1><p>{html.escape(str(exc))}</p>", status_code=409
        )
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


# --- job monitor (BUILD_PLAN 1.11, spec §6.6/§8) ------------------------------------
#
# The page always renders the persisted state, so a plain reload reattaches to
# the running job; with JS, /static/monitor.js attaches to the SSE stream
# (GET /api/jobs/{id}/events) and mirrors every snapshot into this DOM.
# Cancel + per-asset retry are plain form posts (htmx gets the fragment back).

RETRYABLE_JOB_STATUSES = ("running", "partial", "failed", "completed")


def _fetch_job(request: Request, job_id: str) -> Job:
    try:
        return engine_of(request).job(job_id)
    except JobNotFound:
        raise HTTPException(status_code=404, detail=f"no job with id '{job_id}'") from None


def _deliverable_rows(request: Request, job: Job) -> list[dict[str, Any]]:
    """The job's deliverables in output/ for the preview/export section (§4.7)."""
    project_dir = request.app.state.projects_root / job.project
    return [
        {
            "name": item.name,
            "url": f"/projects/{job.project}/output/{item.name}",
            "size_bytes": item.size_bytes,
            "kind": item.kind,
        }
        for item in list_deliverables(project_dir, job)
    ]


def _monitor_context(request: Request, job: Job) -> dict[str, Any]:
    return {
        "job": job,
        "terminal": job.status in TERMINAL_JOB_STATUSES,
        "cancellable": job.status not in TERMINAL_JOB_STATUSES,
        "job_retries": job.status in RETRYABLE_JOB_STATUSES,
        "progress": job_progress(job),
        "estimate_display": _usd(job.estimate_usd),
        "spent_display": _usd(job.spent_usd),
        "cap_display": "no cap" if job.spend_cap_usd is None else _usd(job.spend_cap_usd),
        "asset_costs": {asset.id: _usd(asset.spent_usd) for asset in job.assets},
        "deliverables": _deliverable_rows(request, job),
    }


def _monitor_render(
    request: Request,
    job: Job,
    *,
    status_code: int = 200,
    notice: str | None = None,
    error: str | None = None,
):
    """Full monitor page for plain requests; the #monitor fragment for htmx."""
    context = _monitor_context(request, job) | {"notice": notice, "error": error}
    template = "_monitor.html" if request.headers.get("HX-Request") else "job.html"
    return templates.TemplateResponse(request, template, context, status_code=status_code)


def _monitor_response(
    request: Request,
    job: Job,
    *,
    status_code: int = 200,
    notice: str | None = None,
    error: str | None = None,
):
    """Outcome of a monitor POST: 303 back to the monitor for plain forms, the
    updated #monitor fragment for htmx requests. Non-200 outcomes render the
    page (or fragment) with the error banner — the persisted state is the
    truth the user needs to see."""
    if status_code == 200 and not request.headers.get("HX-Request"):
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)
    return _monitor_render(request, job, status_code=status_code, notice=notice, error=error)


@router.get("/jobs/{job_id}")
def job_page(job_id: str, request: Request):
    job = _fetch_job(request, job_id)
    return _monitor_render(request, job)


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, request: Request):
    engine = engine_of(request)
    try:
        job = engine.cancel(job_id)
    except JobNotFound:
        raise HTTPException(status_code=404, detail=f"no job with id '{job_id}'") from None
    except ValueError as exc:  # IllegalTransition: the job is already terminal
        job = _fetch_job(request, job_id)
        return _monitor_response(request, job, status_code=409, error=str(exc))
    return _monitor_response(request, job, notice="Job cancelled — completed assets are kept.")


@router.post("/jobs/{job_id}/assets/{asset_id}/retry")
def retry_asset(job_id: str, asset_id: int, request: Request):
    engine = engine_of(request)
    try:
        job = engine.retry_asset(job_id, asset_id)
    except JobNotFound:
        raise HTTPException(status_code=404, detail=f"no job with id '{job_id}'") from None
    except AssetNotFound:
        raise HTTPException(
            status_code=404, detail=f"no asset with id {asset_id} in job '{job_id}'"
        ) from None
    except ValueError as exc:  # not failed|skipped, or the asset belongs to another job
        job = _fetch_job(request, job_id)
        return _monitor_response(request, job, status_code=409, error=str(exc))
    return _monitor_response(request, job, notice=f"Asset {asset_id} re-queued for generation.")


# --- preview / export (BUILD_PLAN 1.12, spec §4.7/§8) --------------------------------
#
# Assembly runs at finalize (engine assembler hook) and rebuilds on per-asset
# retry; POST /jobs/{id}/assemble re-runs it on demand without touching assets.
# Deliverables are previewed inline (img/video) and served from output/ —
# traversal is neutralized by resolving inside the project's output dir only.


@router.post("/jobs/{job_id}/assemble")
def assemble_job_route(job_id: str, request: Request):
    engine = engine_of(request)
    try:
        job = engine.assemble(job_id)
    except JobNotFound:
        raise HTTPException(status_code=404, detail=f"no job with id '{job_id}'") from None
    except ValueError as exc:  # non-terminal job, no assembler, or AssemblyError
        job = _fetch_job(request, job_id)
        return _monitor_response(request, job, status_code=409, error=str(exc))
    count = len(_deliverable_rows(request, job))
    return _monitor_response(
        request,
        job,
        notice=f"Deliverables re-assembled ({count} file{'s' if count != 1 else ''})"
        " from the job's assets — preview below.",
    )


@router.get("/projects/{project}/output/{file_path:path}")
def serve_output_file(project: str, file_path: str, request: Request):
    """Serve one deliverable for preview/download — strictly inside output/."""
    out_dir = (request.app.state.projects_root / project / "output").resolve()
    target = (out_dir / file_path).resolve()
    if not target.is_relative_to(out_dir) or not target.is_file():
        raise HTTPException(
            status_code=404, detail=f"no deliverable {file_path!r} in project '{project}'"
        )
    return FileResponse(target, media_type=media_type_for(target.name))
