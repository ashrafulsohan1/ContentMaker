"""Seam: app.launcher — the exe/dev entry point (v1.0.1 packaging).

Arg parsing and the start/stop choreography are tested here with the server and
browser mocked out (unit-level); the real uvicorn boot is exercised by the
launcher smoke against the running process, not by these tests.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Callable

import pytest

from app import launcher
from app.config import DEFAULT_PORT


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


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


def test_open_browser_opens_only_after_the_server_binds(monkeypatch: pytest.MonkeyPatch):
    # pkg-f2: a fixed 1s delay opens the browser before uvicorn has bound on a
    # cold exe start (connection refused in the first tab). The opener must
    # poll the port and fire only once something is actually listening.
    opened: list[str] = []
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: opened.append(url) or True)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    thread = launcher._open_browser_when_up("127.0.0.1", port)
    try:
        # bound but NOT listening yet: nothing may accept the connection
        assert not _wait_until(lambda: opened != [], timeout_s=0.4)
        listener.listen(1)
        assert _wait_until(lambda: opened != [])
        assert opened == [f"http://127.0.0.1:{port}"]
    finally:
        listener.close()
        thread.join(timeout=2.0)
    assert not thread.is_alive()


def test_open_browser_gives_up_when_the_port_never_binds(monkeypatch: pytest.MonkeyPatch):
    # A server that never comes up must not leave a stray browser tab pointing
    # at a dead URL (and must not spin forever).
    opened: list[str] = []
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: opened.append(url) or True)
    monkeypatch.setattr(launcher, "_BROWSER_OPEN_TIMEOUT_S", 0.3)
    monkeypatch.setattr(launcher, "_BROWSER_POLL_S", 0.05)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        free_port = sock.getsockname()[1]  # released below: nothing will listen
    thread = launcher._open_browser_when_up("127.0.0.1", free_port)
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert opened == []


def test_main_starts_uvicorn_on_127_0_0_1_and_opens_browser(
    monkeypatch: pytest.MonkeyPatch,
):
    opened: list[str] = []
    runs: list[dict] = []
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: opened.append(url) or True)

    def fake_run(app: object, **kwargs: object) -> None:
        runs.append(kwargs)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind((kwargs["host"], kwargs["port"]))
            listener.listen(1)  # "uvicorn bound": only now may the browser open
            assert _wait_until(lambda: opened != [])

    monkeypatch.setattr(launcher.uvicorn, "run", fake_run)
    assert launcher.main(["--port", "9100"]) == 0
    assert opened == ["http://127.0.0.1:9100"]
    assert runs[0]["host"] == "127.0.0.1"
    assert runs[0]["port"] == 9100


def test_main_no_browser_never_opens_browser(monkeypatch: pytest.MonkeyPatch):
    opened: list[str] = []
    monkeypatch.setattr(launcher.uvicorn, "run", lambda app, **kwargs: None)
    assert launcher.main(["--no-browser"]) == 0
    assert opened == []


def test_main_treats_ctrl_c_as_a_clean_shutdown(monkeypatch: pytest.MonkeyPatch):
    def interrupt(app: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(launcher.uvicorn, "run", interrupt)
    assert launcher.main(["--no-browser"]) == 0  # returns instead of raising
