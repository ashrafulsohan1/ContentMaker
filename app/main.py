"""FastAPI application factory.

Startup responsibilities arrive in later milestones: ffmpeg env check on
boot, background job worker, in-app maintenance task (BUILD_PLAN §1.2).
"""
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

INDEX_HTML = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>ContentMaker</title></head>
<body>
  <h1>ContentMaker</h1>
  <p>Local generation studio — UI flow arrives in Milestone 1.</p>
</body>
</html>"""


def create_app() -> FastAPI:
    app = FastAPI(title="ContentMaker", version="0.1.0")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return INDEX_HTML

    return app


app = create_app()
