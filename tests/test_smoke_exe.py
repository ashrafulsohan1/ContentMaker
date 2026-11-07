"""Seam: scripts/smoke_exe.py HTTP helpers (v102-f5).

The smoke polls 127.0.0.1 /healthz (and GET /) against the real exe. Those
polls must ignore env/registry HTTP proxies exactly like the launcher's own
health check does (app/launcher.py builds a proxy-free opener for the same
reason) — on a build machine whose proxy does not bypass localhost, a plain
proxy-aware urlopen returns URLError for every poll and the boot/collision
waits false-fail after their timeout although the exe fell back correctly.
Like test_build_exe.py, the script is exercised only at its seams; the smoke
itself never runs here (that needs the built exe).
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / "scripts" / "smoke_exe.py"


def _load_smoke() -> ModuleType:
    spec = importlib.util.spec_from_file_location("smoke_exe_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves string annotations via sys.modules[cls.__module__]
    # (Python 3.14): the module must be registered before exec_module.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def smoke() -> ModuleType:
    return _load_smoke()


class _LocalStub:
    """Tiny HTTP server on an ephemeral port answering /healthz and / — stands
    in for the exe the smoke would poll."""

    def __init__(self) -> None:
        class _Handler(BaseHTTPRequestHandler):
            def _respond(self) -> None:
                if self.path.startswith("/healthz"):
                    body = b'{"frozen": false, "ffmpeg": null}'
                    ctype = "application/json"
                else:
                    body = b"<html>ui root</html>"
                    ctype = "text/html"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = _respond

            def log_message(self, *args: object) -> None:
                pass  # keep test output clean

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)


def _force_dead_env_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A proxy at 127.0.0.1:9 (discard port: nothing listens) that does NOT
    bypass localhost. With env proxies set, urllib takes the proxy_bypass
    _environment branch, so deleting no_proxy makes every 127.0.0.1 request
    route into the dead proxy unless the opener is proxy-free."""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)


def test_healthz_poll_ignores_a_dead_env_proxy(
    smoke: ModuleType, monkeypatch: pytest.MonkeyPatch
):
    # v102-f5: plain urllib.request.urlopen is proxy-aware; with a dead env
    # proxy every _healthz poll returned URLError -> (0, None) and the smoke
    # false-failed its boot/collision waits although the exe was up.
    stub = _LocalStub()
    try:
        _force_dead_env_proxy(monkeypatch)
        status, payload = smoke._healthz(port=stub.port)
        assert status == 200
        assert isinstance(payload, dict)
        assert payload.get("frozen") is False
    finally:
        stub.stop()


def test_healthz_poll_reports_a_dead_port_as_poll_noise(smoke: ModuleType):
    # The companion contract: network errors stay (0, None) so the poll loop
    # keeps waiting for the exe instead of crashing on a not-yet-up server.
    dead = _LocalStub()
    dead.stop()
    assert smoke._healthz(port=dead.port) == (0, None)


def test_ui_root_check_ignores_a_dead_env_proxy(
    smoke: ModuleType, monkeypatch: pytest.MonkeyPatch
):
    # Same defect class as v102-f5 for GET /: the launcher's rule is that env
    # proxies never intercept a localhost check — the smoke's UI check must
    # obey it too or a proxied build machine false-fails here as well.
    stub = _LocalStub()
    try:
        _force_dead_env_proxy(monkeypatch)
        monkeypatch.setattr(smoke, "BASE_URL", f"http://127.0.0.1:{stub.port}")
        assert "200" in smoke.check_ui_root({})
    finally:
        stub.stop()
