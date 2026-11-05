"""FastAPI application factory (BUILD_PLAN 1.10 wiring, monitor added in 1.11).

Assembles the local single-user app:
- ``app.api`` — JSON routes (job status, the SSE live-progress stream, and —
  task 2.1 — the BYOK settings surface);
- ``app.web`` — server-rendered htmx UI flow (input → review → options →
  estimate + confirm → generate → job monitor) plus the Settings screen;
- ``app.state`` — shared singletons: provider ``Registry`` (mocks by default
  — the dry-run mode of §7), the ``KeyStore`` holding the user's API keys in
  the gitignored ``state/keys.json`` (§6.5, task 2.1), the job engine
  (persisted, worker thread), the in-memory wizard ``FlowStore``, the
  projects root (§7 layout), the SSE poll interval, and the maintenance task
  (§6.7 startup + interval sweep, BUILD_PLAN 1.13).

Dependencies are injectable for tests: ``create_app(registry=…, key_store=…,
key_probe=…, projects_root=…)``. Defaults point at the repo's ``projects/``
folder, a registry holding only the zero-network mock providers (§1.2), and a
key store at ``<repo>/state/keys.json``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from starlette.staticfiles import StaticFiles

from app.api import jobs as api_jobs
from app.api import settings as api_settings
from app.api import sse as api_sse
from app.assembly.pipeline import assemble_job
from app.core.jobs import Job, JobEngine
from app.core.keys import KEYS_FILENAME, KeyStore
from app.core.maintenance import DEFAULT_INTERVAL_S, DEFAULT_MIN_AGE_S, MaintenanceTask
from app.engines.mocks import MockImageProvider, MockTTSProvider, MockVideoProvider
from app.engines.registry import ENGINES, Registry
from app.providers import catalog
from app.web import routes as web_routes
from app.web import settings as web_settings
from app.web.flow import FlowStore

WEB_DIR = Path(web_routes.__file__).resolve().parent


def _assembler_for(projects_root: Path):
    """Local assembly hook (task 1.12): the worker builds deliverables right
    before a job finalizes, so ``output/`` is populated when it turns terminal."""

    def _assemble(job: Job) -> None:
        assemble_job(projects_root / job.project, job)

    return _assemble


def default_registry() -> Registry:
    """Mock-only registry: every engine available at zero cost, zero network."""
    registry = Registry()
    registry.register("tts", MockTTSProvider())
    registry.register("image", MockImageProvider())
    registry.register("video", MockVideoProvider())
    return registry


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    maintenance = getattr(app.state, "maintenance", None)
    if maintenance is not None:
        maintenance.start()  # startup sweep + interval loop (§6.7), own thread
    try:
        yield
    finally:
        # Maintenance stops before the job worker: the sweeper must never race
        # a worker that is still finishing a job during shutdown.
        if maintenance is not None:
            maintenance.stop()
        engine = getattr(app.state, "jobs", None)
        if engine is not None:
            engine.stop()


def create_app(
    *,
    registry: Registry | None = None,
    projects_root: Path | None = None,
    sse_poll_s: float = 0.5,
    maintenance_enabled: bool = True,
    maintenance_interval_s: float = DEFAULT_INTERVAL_S,
    maintenance_min_age_s: float = DEFAULT_MIN_AGE_S,
    key_store: KeyStore | None = None,
    key_probe=None,
) -> FastAPI:
    app = FastAPI(title="ContentMaker", version="0.1.0", lifespan=_lifespan)
    app.state.registry = registry if registry is not None else default_registry()
    app.state.projects_root = (
        Path(projects_root) if projects_root is not None else _default_projects_root()
    )
    app.state.key_store = key_store if key_store is not None else _default_key_store()
    # The key probe dials the provider the key belongs to (§6.5); injectable so
    # tests never touch the network.
    app.state.key_probe = key_probe if key_probe is not None else catalog.test_provider_key
    app.state.flow_store = FlowStore()
    app.state.jobs = JobEngine(
        app.state.registry,
        projects_root=app.state.projects_root,
        assembler=_assembler_for(app.state.projects_root),
    )
    app.state.maintenance = (
        MaintenanceTask(
            app.state.projects_root,
            interval_s=maintenance_interval_s,
            min_age_s=maintenance_min_age_s,
        )
        if maintenance_enabled
        else None
    )
    app.state.sse_poll_s = float(sse_poll_s)
    app.include_router(api_jobs.router)
    app.include_router(api_settings.router)
    app.include_router(api_sse.router)
    app.include_router(web_settings.router)
    app.include_router(web_routes.router)
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    return app


def _default_projects_root() -> Path:
    from app.config import projects_root

    return projects_root()


def _default_key_store() -> KeyStore:
    from app.config import app_state_dir

    return KeyStore(
        app_state_dir() / KEYS_FILENAME,
        provider_engines={info.id: info.engines for info in catalog.PROVIDERS.values()},
        engines=ENGINES,
    )


app = create_app()
