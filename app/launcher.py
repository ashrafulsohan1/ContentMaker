"""Executable/dev entry point (v1.0.1 packaging): parse args, start uvicorn,
open the UI in a browser, stop cleanly on Ctrl+C.

The packaged ContentMaker.exe runs this module as its entry script; in the
repo it doubles as the friendlier ``python -m uvicorn`` alternative:

    python -m app.launcher                # http://127.0.0.1:8765 + browser
    python -m app.launcher --port 9000    # custom port
    python -m app.launcher --no-browser   # headless start

Server binds 127.0.0.1 only (§6.5). The browser opens once the server actually
answers on its port (polled, so a cold one-file exe start can never open the
tab too early); Ctrl+C is a normal, clean stop. Writable content (projects/,
state/) lands next to the executable in frozen builds (app.config.app_home)
or in the repo root in dev.
"""
from __future__ import annotations

import argparse
import socket
import threading
import time
import webbrowser
from collections.abc import Sequence

import uvicorn

from app.config import DEFAULT_HOST, DEFAULT_PORT

# Poll cadence while waiting for uvicorn to bind, and how long to wait before
# giving up (a server that never comes up must not leave a tab on a dead URL).
_BROWSER_POLL_S = 0.1
_BROWSER_OPEN_TIMEOUT_S = 30.0


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


def _port_accepts(host: str, port: int) -> bool:
    """True once something answers on (host, port) — i.e. uvicorn has bound."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(_BROWSER_POLL_S)
        return sock.connect_ex((host, port)) == 0


def _open_browser_when_up(host: str, port: int) -> threading.Thread:
    """Open the UI in a browser as soon as the server accepts on its port.

    Polls instead of sleeping a fixed delay (pkg-f2): on a cold one-file exe
    start the app import alone can outlast any constant, and opening earlier
    shows the user a connection-refused tab. Gives up after
    :data:`_BROWSER_OPEN_TIMEOUT_S` so a failed boot never opens a dead URL.
    """
    url = f"http://{host}:{port}"

    def _open() -> None:
        deadline = time.monotonic() + _BROWSER_OPEN_TIMEOUT_S
        while time.monotonic() < deadline:
            if _port_accepts(host, port):
                webbrowser.open(url)
                return
            time.sleep(_BROWSER_POLL_S)

    thread = threading.Thread(target=_open, name="open-browser-when-up", daemon=True)
    thread.start()
    return thread


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.no_browser:
        _open_browser_when_up(DEFAULT_HOST, args.port)
    from app.main import app  # deferred: heavy import, only needed to serve

    try:
        uvicorn.run(app, host=DEFAULT_HOST, port=args.port)
    except KeyboardInterrupt:
        pass  # Ctrl+C is a normal stop; uvicorn shuts the worker down cleanly
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
