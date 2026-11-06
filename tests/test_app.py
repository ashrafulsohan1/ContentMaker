"""Seam: app.main.create_app — HTTP surface of the skeleton.

Catches: app factory broken, index route missing or erroring; (v1.0.1) the
/healthz diagnostics endpoint reporting resolved binaries + the frozen flag.
"""

from __future__ import annotations

import sys

from fastapi.testclient import TestClient

from app.core import binaries
from app.main import create_app


def test_index_serves_placeholder_page():
    client = TestClient(create_app())
    resp = client.get("/")
    assert resp.status_code == 200
    assert "ContentMaker" in resp.text


def test_healthz_reports_resolved_binaries_and_frozen_flag():
    client = TestClient(create_app())
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    # This suite requires real ffmpeg/ffprobe on PATH (spec §7): the endpoint
    # must report the same paths the resolver finds, and dev is not frozen.
    assert body["ffmpeg"] == binaries.resolve("ffmpeg")
    assert body["ffprobe"] == binaries.resolve("ffprobe")
    assert body["frozen"] is False


def test_healthz_binaries_are_null_when_nothing_resolves(monkeypatch):
    monkeypatch.delenv("CONTENTMAKER_FFMPEG_DIR", raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(binaries.shutil, "which", lambda tool: None)
    client = TestClient(create_app())
    body = client.get("/healthz").json()
    assert body == {"ffmpeg": None, "ffprobe": None, "frozen": False}


def test_healthz_frozen_flag_tracks_sys(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    client = TestClient(create_app())
    assert client.get("/healthz").json()["frozen"] is True
