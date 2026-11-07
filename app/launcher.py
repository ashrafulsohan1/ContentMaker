"""Executable/dev entry point (v1.0.1 packaging; port-collision fix v1.0.2):
pick a free port, start uvicorn, open the UI in a browser once the app itself
answers, stop cleanly on Ctrl+C.

The packaged ContentMaker.exe runs this module as its entry script; in the
repo it doubles as the friendlier ``python -m uvicorn`` alternative:

    python -m app.launcher                # http://127.0.0.1:8765 + browser
    python -m app.launcher --port 9000    # custom port
    python -m app.launcher --no-browser   # headless start

Server binds 127.0.0.1 only (§6.5). v1.0.2 hardens two field failures seen
when a foreign server intermittently squats 127.0.0.1:8765:

- the launcher no longer dies on bind: before uvicorn starts it probes the
  requested port with a short-lived socket bind and, when it is taken, shifts
  to the first free port upward (8766..8795 for the default), announcing the
  move on the console;
- the browser opens only when GET /healthz answers 200 with JSON carrying a
  ``frozen`` key — never on a bare TCP connect, which any squatter also
  provides (the browser once opened onto a foreign directory listing).

Writable content (projects/, state/) lands next to the executable in frozen
builds (app.config.app_home) or in the repo root in dev.
"""

from __future__ import annotations

import argparse
import http.client
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Callable, Sequence

import uvicorn

from app.config import DEFAULT_HOST, DEFAULT_PORT

# Poll cadence while waiting for the server, how long to wait before giving up
# (a server that never comes up must not leave a tab on a dead URL), and the
# per-attempt HTTP timeout (a hung squatter must not stall the poll loop).
_BROWSER_POLL_S = 0.1
_BROWSER_OPEN_TIMEOUT_S = 30.0
_HEALTHZ_ATTEMPT_TIMEOUT_S = 1.0

# How far past the requested port the fallback may shift (8765..8795 for the
# default; a --port 9000 start scans 9000..9030).
_PORT_SCAN_EXTRA = 30

# Env proxies must never intercept a localhost health check (a corporate
# HTTP_PROXY would otherwise break or hijack the poll).
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ContentMaker",
        description="Start the ContentMaker local web app (http://127.0.0.1:<port>).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"port to listen on (default {DEFAULT_PORT}; auto-shifts when busy)",
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


def _port_free(host: str, port: int) -> bool:
    """True when a plain bind to (host, port) succeeds — i.e. uvicorn could
    take it. Deliberately no SO_REUSEADDR: on Windows that flag lets a bind
    succeed even while another process holds the port, which is exactly the
    situation this probe exists to detect."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def pick_port(requested: int, host: str = DEFAULT_HOST) -> int | None:
    """First port in ``requested..requested + _PORT_SCAN_EXTRA`` we can bind.

    Returns None when the whole window is busy. Prints a console notice when
    it had to shift off the requested port, so the user can read the real URL
    the browser will open on.
    """
    for port in range(requested, min(requested + _PORT_SCAN_EXTRA, 65535) + 1):
        if _port_free(host, port):
            if port != requested:
                print(f"Port {requested} in use — using {port}")
            return port
    return None


def _healthz_ready(host: str, port: int) -> bool:
    """True only when OUR app answers GET /healthz: HTTP 200 and a JSON object
    carrying a ``frozen`` key. Anything else — connection refused, a foreign
    server's 200 HTML listing, a non-JSON body — keeps the browser shut."""
    url = f"http://{host}:{port}/healthz"
    try:
        with _OPENER.open(url, timeout=_HEALTHZ_ATTEMPT_TIMEOUT_S) as resp:
            if resp.status != 200:
                return False
            body = resp.read(65536)
    except (OSError, http.client.HTTPException):
        # URLError/HTTPError/socket.timeout ride on OSError; a non-HTTP
        # squatter can also produce BadStatusLine & co. (HTTPException).
        return False
    try:
        payload = json.loads(body)
    except ValueError:
        return False
    return isinstance(payload, dict) and "frozen" in payload


def wait_for_healthz(
    port: int,
    timeout: float,
    open_browser: Callable[[str], object],
    *,
    host: str = DEFAULT_HOST,
    poll: float = _BROWSER_POLL_S,
) -> bool:
    """Poll /healthz until the app is actually up, then open the browser once.

    Returns True when the browser was opened, False after giving up at
    ``timeout`` seconds (no tab on a dead URL). Every stage prints to the
    console: on a cold one-file exe start the unpacking alone can take up to a
    minute, and a silent console window must never look dead.
    """
    url = f"http://{host}:{port}"
    print("First launch can take up to a minute (unpacking).")
    print(f"Waiting for server... {url}")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _healthz_ready(host, port):
            open_browser(url)
            return True
        time.sleep(poll)
    print(f"Server did not come up within {timeout:.0f}s — not opening a browser.")
    return False


def _open_browser_when_up(host: str, port: int) -> threading.Thread:
    """Open the UI in a browser as soon as the app answers /healthz (pkg-f2,
    hardened in v1.0.2: healthz-gated, not a bare TCP connect). The globals
    are read at call time so tests can shrink the timeout."""
    def _open() -> None:
        wait_for_healthz(
            port,
            _BROWSER_OPEN_TIMEOUT_S,
            webbrowser.open,
            host=host,
            poll=_BROWSER_POLL_S,
        )

    thread = threading.Thread(target=_open, name="open-browser-when-up", daemon=True)
    thread.start()
    return thread


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    port = pick_port(args.port)
    if port is None:
        last = min(args.port + _PORT_SCAN_EXTRA, 65535)
        print(
            f"No free port in {args.port}-{last} — close what is holding them "
            f"or start with --port <free port>.",
            file=sys.stderr,
        )
        return 1
    if not args.no_browser:
        _open_browser_when_up(DEFAULT_HOST, port)
    from app.main import app  # deferred: heavy import, only needed to serve

    try:
        uvicorn.run(app, host=DEFAULT_HOST, port=port)
    except KeyboardInterrupt:
        pass  # Ctrl+C is a normal stop; uvicorn shuts the worker down cleanly
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
