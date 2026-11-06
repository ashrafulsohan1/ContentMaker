"""Seam: app.launcher — the exe/dev entry point (v1.0.1 packaging).

Arg parsing and the start/stop choreography are tested here with the server and
browser mocked out (unit-level); the real uvicorn boot is exercised by the
launcher smoke against the running process, not by these tests.
"""

from __future__ import annotations

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


def test_main_starts_uvicorn_on_127_0_0_1_and_opens_browser(
    monkeypatch: pytest.MonkeyPatch,
):
    opened: list[str] = []
    runs: list[dict] = []
    monkeypatch.setattr(launcher, "_BROWSER_OPEN_DELAY_S", 0.0)
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: opened.append(url) or True)
    monkeypatch.setattr(launcher.uvicorn, "run", lambda app, **kwargs: runs.append(kwargs))
    assert launcher.main(["--port", "9100"]) == 0
    # The browser opens on a delayed thread so uvicorn binds first — wait for it.
    assert _wait_until(lambda: opened != [])
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
