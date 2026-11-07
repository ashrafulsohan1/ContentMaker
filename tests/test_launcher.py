"""Seam: app.launcher — the exe/dev entry point.

Arg parsing, port fallback and the start/stop choreography are tested here.
The v1.0.2 port-collision fix is exercised against real sockets: a stub HTTP
server plays the foreign squatter (200 on /, wrong or missing /healthz), and
one test boots the real FastAPI app under uvicorn to prove /healthz is served
on the fallback port. The v1.0.2 review findings (v102-f1/f2/f4) harden the
choreography further: the port found by ``reserve_port`` stays HELD (a bound,
listening socket handed to uvicorn) across the deferred heavy import, so a
racer can no longer take it in the probe-to-bind gap; console notices must
survive a redirected stdout on a non-ASCII ANSI code page; and the plain
Ctrl+C / --no-browser choreography keeps its guards. The frozen-exe smoke
remains scripts/smoke_exe.py's job.
"""

from __future__ import annotations

import io
import json
import socket
import sys
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import uvicorn

from app import launcher
from app.config import DEFAULT_PORT

HOST = "127.0.0.1"


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _free_ephemeral_port() -> int:
    """A port the OS just handed us and we released — free at return time."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind((HOST, 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _occupy(port: int) -> socket.socket | None:
    """Bind+listen on 127.0.0.1:<port>; None when something already holds it.

    None still means "occupied": the launcher must skip the port either way,
    so callers treat both outcomes identically.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((HOST, port))
        sock.listen(1)
    except OSError:
        sock.close()
        return None
    return sock


def _first_free(start: int, end: int) -> int | None:
    """First port in [start, end] a plain test-side bind can take.

    Computed independently of launcher.pick_port so the fallback tests assert
    real discovery, not the implementation against itself.
    """
    for port in range(start, end + 1):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind((HOST, port))
        except OSError:
            probe.close()
            continue
        probe.close()
        return port
    return None


class _StubHttp:
    """Tiny HTTP server on an ephemeral port serving fixed per-path answers —
    stands in for both our app and the foreign squatter of the field report."""

    def __init__(self, routes: dict[str, tuple[int, str, bytes]], port: int = 0):
        hits: dict[str, int] = {}

        class _Handler(BaseHTTPRequestHandler):
            def _respond(self) -> None:
                path = self.path.split("?", 1)[0]
                hits[path] = hits.get(path, 0) + 1
                status, ctype, body = routes.get(path, (404, "text/plain", b"not found"))
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = _respond
            do_POST = _respond

            def log_message(self, *args: object) -> None:
                pass  # keep test output clean

        self._hits = hits
        self._server = ThreadingHTTPServer((HOST, port), _Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def hits(self) -> dict[str, int]:
        return self._hits

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)


# --- arg parsing (contract unchanged by v1.0.2) ------------------------------


def test_parse_args_defaults():
    args = launcher.parse_args([])
    assert args.port == DEFAULT_PORT == 8765
    assert args.no_browser is False


def test_parse_args_port_and_no_browser():
    args = launcher.parse_args(["--port", "9100", "--no-browser"])
    assert args.port == 9100
    assert args.no_browser is True


def test_parse_args_rejects_out_of_range_port():
    with pytest.raises(SystemExit):
        launcher.parse_args(["--port", "70000"])
    with pytest.raises(SystemExit):
        launcher.parse_args(["--port", "0"])


# --- v1.0.2 port fallback, hardened per v102-f1 -------------------------------
#
# v1.0.2 discovered the port with a short-lived probe bind and released it long
# before uvicorn bound: the deferred heavy import (from app.main import app)
# sat in that gap, so a racer binding the chosen port there (a second exe
# cold-starting in parallel probes the same fallback port) made uvicorn die
# with exit code 3 (STARTUP_FAILURE) instead of falling back. The discovery
# is now reserve_port: the WINNING bind is never released — it is returned as
# a held, listening socket and uvicorn serves ON it (Server.run(sockets=...)),
# so the port is ours from discovery until serving.


def test_reserve_port_holds_a_free_requested_port(capsys: pytest.CaptureFixture[str]):
    free = _free_ephemeral_port()
    reserved = launcher.reserve_port(free)
    assert reserved is not None
    port, held = reserved
    try:
        assert port == free
        assert capsys.readouterr().out == ""  # normal start stays quiet
    finally:
        held.close()


def test_reserve_port_skips_occupied_ports_and_announces_the_shift(
    capsys: pytest.CaptureFixture[str],
):
    base = _free_ephemeral_port()
    holders = [sock for sock in (_occupy(base), _occupy(base + 1)) if sock is not None]
    try:
        expected = _first_free(base + 2, base + launcher._PORT_SCAN_EXTRA)
        assert expected is not None, "no free port left in the fallback window"
        reserved = launcher.reserve_port(base)
        assert reserved is not None
        port, held = reserved
        try:
            assert port == expected
            out = capsys.readouterr().out
            assert f"Port {base} in use - using {expected}" in out  # ASCII: v102-f2
        finally:
            held.close()
    finally:
        for sock in holders:
            sock.close()


def test_reserve_port_returns_none_when_the_whole_window_is_busy():
    holders: list[socket.socket] = []
    try:
        for port in range(DEFAULT_PORT, DEFAULT_PORT + launcher._PORT_SCAN_EXTRA + 1):
            sock = _occupy(port)
            if sock is not None:
                holders.append(sock)
        # every port in 8765..8795 is now held (ours, or a pre-existing squatter's)
        assert launcher.reserve_port(DEFAULT_PORT) is None
    finally:
        for sock in holders:
            sock.close()


def test_reserve_port_holds_the_winning_bind_against_an_import_window_racer():
    # v102-f1: while the reserved socket is held, NO other bind on the port can
    # succeed — this is exactly the state during the deferred heavy import —
    # and the port only frees again when the holder is closed.
    base = _free_ephemeral_port()
    reserved = launcher.reserve_port(base)
    assert reserved is not None
    port, held = reserved
    try:
        assert port == base
        racer = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(OSError):
                racer.bind((HOST, port))
        finally:
            racer.close()
    finally:
        held.close()
    assert _first_free(base, base) == base  # released only by the explicit close


def test_main_falls_back_to_the_next_free_port_when_the_default_is_taken(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    served: list[tuple[str, int]] = []
    # getsockname is recorded inside the stub: main() closes the socket after
    # _serve returns, and getsockname on a closed socket raises WinError 10038
    monkeypatch.setattr(
        launcher, "_serve", lambda app, sock: served.append(sock.getsockname())
    )
    holder = _occupy(DEFAULT_PORT)  # real socket; None means a squatter got there first
    try:
        expected = _first_free(DEFAULT_PORT, DEFAULT_PORT + launcher._PORT_SCAN_EXTRA)
        assert expected is not None and expected != DEFAULT_PORT
        assert launcher.main(["--no-browser"]) == 0
        assert len(served) == 1
        # uvicorn receives the HELD socket itself, already bound to the fallback
        assert served[0] == (HOST, expected)
        out = capsys.readouterr().out
        assert f"Port {DEFAULT_PORT} in use - using {expected}" in out  # ASCII: v102-f2
    finally:
        if holder is not None:
            holder.close()


def test_main_exits_with_a_clear_error_when_every_port_is_busy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    called: list[socket.socket] = []
    monkeypatch.setattr(launcher, "_serve", lambda app, sock: called.append(sock))
    holders: list[socket.socket] = []
    try:
        for port in range(DEFAULT_PORT, DEFAULT_PORT + launcher._PORT_SCAN_EXTRA + 1):
            sock = _occupy(port)
            if sock is not None:
                holders.append(sock)
        assert launcher.main(["--no-browser"]) == 1
        assert called == []  # uvicorn must never start on a busy window
        err = capsys.readouterr().err
        assert "8765" in err and "8795" in err and "port" in err.lower()
    finally:
        for sock in holders:
            sock.close()


def test_main_keeps_the_port_held_until_uvicorn_serves_it(monkeypatch: pytest.MonkeyPatch):
    # v102-f1 at the main() seam: between the port scan and _serve sits the
    # deferred heavy import (seconds in the frozen exe). Whatever a parallel
    # process does there, the socket _serve receives must still be OURS — bound
    # to the chosen port and listening — so uvicorn serves it instead of
    # rebinding (the old probe-then-release design exited 3 on a lost race).
    seen: dict = {}

    def fake_serve(app: object, sock: socket.socket) -> None:
        seen["bound"] = sock.getsockname()
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(1.0)
        try:
            # a listening socket completes the TCP handshake into its backlog
            seen["listening"] = probe.connect_ex(seen["bound"]) == 0
        finally:
            probe.close()
        racer = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            racer.bind(seen["bound"])  # the import-window racer
            seen["racer_won"] = True
        except OSError:
            seen["racer_won"] = False
        finally:
            racer.close()

    monkeypatch.setattr(launcher, "_serve", fake_serve)
    port = _free_ephemeral_port()
    assert launcher.main(["--port", str(port), "--no-browser"]) == 0
    assert seen["bound"] == (HOST, port)
    assert seen["listening"] is True
    assert seen["racer_won"] is False  # the port was still ours inside _serve


# --- v102-f2: console notices must survive a redirected non-ASCII stdout -------


def test_shift_notice_survives_a_non_ascii_redirect_codepage(
    monkeypatch: pytest.MonkeyPatch,
):
    # Redirected stdout on a Windows ANSI code page that cannot encode U+2014
    # (cp932/cp936/cp949/cp874) made print() raise UnicodeEncodeError with
    # errors='strict' — crashing port discovery BEFORE uvicorn started, so on
    # those locales the collision fallback itself never launched.
    buf = io.BytesIO()
    cp932 = io.TextIOWrapper(buf, encoding="cp932", errors="strict", newline="\n")
    monkeypatch.setattr(sys, "stdout", cp932)
    base = _free_ephemeral_port()
    holder = _occupy(base)
    try:
        reserved = launcher.reserve_port(base)  # prints the shift notice
        assert reserved is not None
        port, held = reserved
        held.close()
    finally:
        if holder is not None:
            holder.close()
        cp932.flush()
    text = buf.getvalue().decode("cp932")
    assert f"Port {base} in use" in text
    assert f"using {port}" in text


def test_give_up_notice_survives_a_non_ascii_redirect_codepage(
    monkeypatch: pytest.MonkeyPatch,
):
    buf = io.BytesIO()
    cp932 = io.TextIOWrapper(buf, encoding="cp932", errors="strict", newline="\n")
    monkeypatch.setattr(sys, "stdout", cp932)
    dead_port = _free_ephemeral_port()
    assert launcher.wait_for_healthz(dead_port, 0.2, lambda url: None, poll=0.05) is False
    cp932.flush()
    assert "not opening a browser" in buf.getvalue().decode("cp932")


def test_no_free_port_error_survives_a_non_ascii_redirect_codepage(
    monkeypatch: pytest.MonkeyPatch,
):
    buf = io.BytesIO()
    cp932 = io.TextIOWrapper(buf, encoding="cp932", errors="strict", newline="\n")
    monkeypatch.setattr(sys, "stderr", cp932)
    holders: list[socket.socket] = []
    try:
        for port in range(DEFAULT_PORT, DEFAULT_PORT + launcher._PORT_SCAN_EXTRA + 1):
            sock = _occupy(port)
            if sock is not None:
                holders.append(sock)
        assert launcher.main(["--no-browser"]) == 1  # prints the error to stderr
    finally:
        for sock in holders:
            sock.close()
        cp932.flush()
    text = buf.getvalue().decode("cp932")
    assert "No free port in 8765-8795" in text


# --- v1.0.2 healthz-gated browser open ----------------------------------------


def test_wait_for_healthz_never_opens_when_healthz_404s():
    # The exact v1.0.1 field failure shape: something answers on the port and
    # even serves /, but /healthz 404s. A bare-TCP or homepage poll opens the
    # browser onto the wrong app; the healthz gate must not.
    stub = _StubHttp({"/": (200, "text/html", b"<html>directory listing</html>")})
    opened: list[str] = []
    try:
        assert launcher.wait_for_healthz(stub.port, 0.5, opened.append) is False
        assert opened == []
        assert stub.hits.get("/healthz", 0) >= 1  # polled and rejected, not skipped
    finally:
        stub.stop()


def test_wait_for_healthz_never_opens_on_non_app_answers():
    # Squatters that answer 200 on /healthz but are not our app: an HTML
    # listing (the reported directory listing) and JSON without the "frozen"
    # marker must both keep the browser shut.
    opened: list[str] = []
    for healthz in (
        (200, "text/html", b"<html>not our app</html>"),
        (200, "application/json", b'{"status": "ok"}'),
    ):
        stub = _StubHttp({"/healthz": healthz})
        try:
            assert launcher.wait_for_healthz(stub.port, 0.5, opened.append) is False
            assert opened == []
        finally:
            stub.stop()


def test_wait_for_healthz_opens_exactly_once_on_frozen_json():
    # 200 + JSON carrying "frozen" is our app: open, exactly once, right URL.
    stub = _StubHttp(
        {"/healthz": (200, "application/json", b'{"frozen": true, "ffmpeg": null}')}
    )
    opened: list[str] = []
    try:
        assert launcher.wait_for_healthz(stub.port, 2.0, opened.append) is True
        assert opened == [f"http://{HOST}:{stub.port}"]
    finally:
        stub.stop()


def test_wait_for_healthz_opens_in_dev_where_frozen_is_false():
    # "frozen" marks the app's diagnostics payload, it does not demand a frozen
    # build: the dev server reports frozen:false and must get its browser too.
    stub = _StubHttp({"/healthz": (200, "application/json", b'{"frozen": false}')})
    opened: list[str] = []
    try:
        assert launcher.wait_for_healthz(stub.port, 2.0, opened.append) is True
        assert opened == [f"http://{HOST}:{stub.port}"]
    finally:
        stub.stop()


def test_wait_for_healthz_prints_progress_and_gives_up_without_opening(
    capsys: pytest.CaptureFixture[str],
):
    # Nothing listens at all: give up inside the budget, never open, and leave
    # console lines for the wait itself so the window never looks dead.
    dead_port = _free_ephemeral_port()
    opened: list[str] = []
    assert launcher.wait_for_healthz(dead_port, 0.3, opened.append, poll=0.05) is False
    assert opened == []
    out = capsys.readouterr().out
    assert "Waiting for server" in out
    assert "First launch can take up to a minute (unpacking)" in out
    assert "not opening a browser" in out


def test_open_browser_when_up_ignores_a_bare_tcp_listener(monkeypatch: pytest.MonkeyPatch):
    # v1.0.1's gate was "something accepts on the port" — exactly what a
    # foreign squatter provides. The thread-level opener must hold the browser
    # until /healthz answers, and give up without opening when it never does.
    opened: list[str] = []
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: opened.append(url) or True)
    monkeypatch.setattr(launcher, "_BROWSER_OPEN_TIMEOUT_S", 0.3)
    monkeypatch.setattr(launcher, "_BROWSER_POLL_S", 0.05)
    listener = _occupy(_free_ephemeral_port())  # accepts TCP, speaks no HTTP
    assert listener is not None
    thread = launcher._open_browser_when_up(HOST, listener.getsockname()[1])
    try:
        assert not _wait_until(lambda: opened != [], timeout_s=0.2)
    finally:
        listener.close()
        thread.join(timeout=5.0)
    assert opened == []
    assert not thread.is_alive()


def test_main_opens_the_browser_once_the_chosen_port_serves_healthz(
    monkeypatch: pytest.MonkeyPatch,
):
    # Full choreography with a REAL uvicorn: main holds the port, then _serve
    # takes the pre-bound socket over (Server.run(sockets=...) — v102-f1), and
    # the browser opens exactly once, on the real URL, only after /healthz
    # answers; the server then shuts down cleanly so main returns 0.
    opened: list[str] = []
    holder: dict[str, uvicorn.Server] = {}

    async def _healthz_asgi(scope: object, receive: object, send: object) -> None:
        body = b'{"frozen": false}'
        await send(  # type: ignore[union-attr]
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})  # type: ignore[union-attr]

    def fake_serve(app: object, sock: socket.socket) -> None:
        server = uvicorn.Server(
            uvicorn.Config(
                _healthz_asgi,
                host=HOST,
                port=sock.getsockname()[1],
                log_level="warning",
                lifespan="off",
            )
        )
        holder["server"] = server
        server.run(sockets=[sock])

    monkeypatch.setattr(launcher, "_serve", fake_serve)
    monkeypatch.setattr(
        launcher.webbrowser,
        "open",
        lambda url: opened.append(url) or holder["server"].__setattr__("should_exit", True),
    )
    port = _free_ephemeral_port()
    assert launcher.main(["--port", str(port)]) == 0
    assert opened == [f"http://{HOST}:{port}"]


# --- restored choreography guards (v102-f4) ------------------------------------
#
# The v1.0.2 port-fallback rewrite deleted these two tests and nothing else
# covered the behaviors, while README's "First run" still promises both.


def test_main_no_browser_never_opens_browser(monkeypatch: pytest.MonkeyPatch):
    # Stronger than the v1.0.1 original: a live /healthz is served during the
    # "run", so a wrongly-started opener thread would have something to open on.
    opened: list[str] = []
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: opened.append(url) or True)

    def serve_and_answer(app: object, sock: socket.socket) -> None:
        port = sock.getsockname()[1]
        sock.close()  # hand the port over the way serving would
        stub = _StubHttp({"/healthz": (200, "application/json", b'{"frozen": false}')}, port=port)
        try:
            time.sleep(0.3)  # a wrongly-started opener polls every 0.1s
        finally:
            stub.stop()

    monkeypatch.setattr(launcher, "_serve", serve_and_answer)
    assert launcher.main(["--no-browser"]) == 0
    assert opened == []


def test_main_treats_ctrl_c_as_a_clean_shutdown(monkeypatch: pytest.MonkeyPatch):
    held: list[socket.socket] = []

    def interrupt(app: object, sock: socket.socket) -> None:
        held.append(sock)
        raise KeyboardInterrupt

    monkeypatch.setattr(launcher, "_serve", interrupt)
    assert launcher.main(["--no-browser"]) == 0  # returns instead of raising
    assert held and held[0].fileno() == -1  # the reserved port was released


# --- end-to-end: fallback port serves the real app -----------------------------


def test_fallback_port_serves_the_real_app_healthz():
    # The default port held by a real socket (or by the machine's own squatter,
    # in which case _occupy reports None and the port is occupied anyway): the
    # launcher's reserve skips it, HOLDS the next free port, and the REAL
    # FastAPI app served on that pre-held socket (Server.run(sockets=...), the
    # exact takeover main performs) answers GET /healthz with 200 + "frozen".
    holder = _occupy(DEFAULT_PORT)
    reserved = launcher.reserve_port(DEFAULT_PORT)
    assert reserved is not None
    picked, held = reserved
    assert picked != DEFAULT_PORT

    from app.main import app as real_app

    server = uvicorn.Server(
        uvicorn.Config(real_app, host=HOST, port=picked, log_level="warning")
    )
    thread = threading.Thread(target=lambda: server.run(sockets=[held]), daemon=True)
    thread.start()
    opened: list[str] = []
    try:
        assert _wait_until(lambda: server.started, timeout_s=15.0), "uvicorn never started"
        assert launcher.wait_for_healthz(picked, 10.0, opened.append) is True
        assert opened == [f"http://{HOST}:{picked}"]
        with launcher._OPENER.open(f"http://{HOST}:{picked}/healthz", timeout=5.0) as resp:
            payload = json.loads(resp.read())
        assert resp.status == 200
        assert "frozen" in payload
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)
        held.close()  # uvicorn closed it on graceful shutdown; close is idempotent
        if holder is not None:
            holder.close()
    assert not thread.is_alive()
