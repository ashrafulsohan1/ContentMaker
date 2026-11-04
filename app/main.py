"""FastAPI application factory (BUILD_PLAN 1.10 wiring, monitor added in 1.11).

Assembles the local single-user app:
- ``app.api`` — JSON routes (job status + the SSE live-progress stream;
  settings arrive in M2);
- ``app.web`` — server-rendered htmx UI flow (input → review → options →
  estimate + confirm → generate → job monitor);
- ``app.state`` — shared singletons: provider ``Registry`` (mocks by default
  — the dry-run mode of §7), the job engine (persisted, worker thread), the
  in-memory wizard ``FlowStore``, the projects root (§7 layout), and the SSE
  poll interval.

Dependencies are injectable for tests: ``create_app(registry=…,
projects_root=…, sse_poll_s=…)``. Defaults point at the repo's ``projects/``
folder and a registry holding only the zero-network mock providers (§1.2:
dry-run is the safe default until the user adds real keys in M2).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from starlette.staticfiles import StaticFiles

from app.api import jobs as api_jobs
from app.api import sse as api_sse
from app.core.jobs import JobEngine
from app.engines.mocks import MockImageProvider, MockTTSProvider, MockVideoProvider
from app.engines.registry import Registry
from app.web import routes as web_routes
from app.web.flow import FlowStore

WEB_DIR = Path(web_routes.__file__).resolve().parent


def default_registry() -> Registry:
    """Mock-only registry: every engine available at zero cost, zero network."""
    registry = Registry()
    registry.register("tts", MockTTSProvider())
    registry.register("image", MockImageProvider())
    registry.register("video", MockVideoProvider())
    return registry


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    engine = getattr(app.state, "jobs", None)
    if engine is not None:
        engine.stop()


def create_app(
    *,
    registry: Registry | None = None,
    projects_root: Path | None = None,
    sse_poll_s: float = 0.5,
) -> FastAPI:
    app = FastAPI(title="ContentMaker", version="0.1.0", lifespan=_lifespan)
    app.state.registry = registry if registry is not None else default_registry()
    app.state.projects_root = (
        Path(projects_root) if projects_root is not None else _default_projects_root()
    )
    app.state.flow_store = FlowStore()
    app.state.jobs = JobEngine(app.state.registry, projects_root=app.state.projects_root)
    app.state.sse_poll_s = float(sse_poll_s)
    app.include_router(api_jobs.router)
    app.include_router(api_sse.router)
    app.include_router(web_routes.router)
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    return app


def _default_projects_root() -> Path:
    from app.config import projects_root

    return projects_root()


app = create_app()
