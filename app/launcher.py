"""Executable/dev entry point (v1.0.1 packaging): parse args, start uvicorn,
open the UI in a browser, stop cleanly on Ctrl+C.

The packaged ContentMaker.exe runs this module as its entry script; in the
repo it doubles as the friendlier ``python -m uvicorn`` alternative:

    python -m app.launcher                # http://127.0.0.1:8765 + browser
    python -m app.launcher --port 9000    # custom port
    python -m app.launcher --no-browser   # headless start

Server binds 127.0.0.1 only (§6.5). The browser opens on a short delay so
uvicorn has bound before the first request; Ctrl+C is a normal, clean stop.
Writable content (projects/, state/) lands next to the executable in frozen
builds (app.config.app_home) or in the repo root in dev.
"""
from __future__ import annotations

import argparse
import threading
import webbrowser
from collections.abc import Sequence

import uvicorn

from app.config import DEFAULT_HOST, DEFAULT_PORT

# uvicorn needs a moment to bind before the browser dials the URL.
_BROWSER_OPEN_DELAY_S = 1.0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ContentMaker",
        description="Start the ContentMaker local web app (http://127.0.0.1:<port>).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"port to listen on (default {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="do not open the UI in a browser on start",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error(f"--port must be 1-65535 (got {args.port})")
    return args


def _open_browser_when_up(url: str) -> None:
    timer = threading.Timer(_BROWSER_OPEN_DELAY_S, webbrowser.open, (url,))
    timer.daemon = True
    timer.start()


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    url = f"http://{DEFAULT_HOST}:{args.port}"
    if not args.no_browser:
        _open_browser_when_up(url)
    from app.main import app  # deferred: heavy import, only needed to serve

    try:
        uvicorn.run(app, host=DEFAULT_HOST, port=args.port)
    except KeyboardInterrupt:
        pass  # Ctrl+C is a normal stop; uvicorn shuts the worker down cleanly
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
