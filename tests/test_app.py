"""Seam: app.main.create_app — HTTP surface of the skeleton.

Catches: app factory broken, index route missing or erroring.
"""
from fastapi.testclient import TestClient

from app.main import create_app


def test_index_serves_placeholder_page():
    client = TestClient(create_app())
    resp = client.get("/")
    assert resp.status_code == 200
    assert "ContentMaker" in resp.text
