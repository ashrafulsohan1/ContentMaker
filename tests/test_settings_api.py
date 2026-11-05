"""Seams: provider catalog + Gemini key probe + /api/settings JSON (BUILD_PLAN 2.1).

Three layers pinned here:

- ``app.providers.catalog`` — the known BYOK providers as data: which engines
  each serves, where to get a key, and how to probe one. The catalog is what
  "engine availability from configured keys" (§6.5) is derived from.
- ``app.providers.gemini.keytest`` — the zero-spend key probe against
  Google's documented ``GET /v1beta/voices`` (research spike 2.0, §6): the key
  travels **only** in the ``x-goog-api-key`` header to Google's endpoint, and
  every failure maps to a message that says exactly what's wrong (401 invalid,
  403 unrestricted/restricted-key, 429 quota, 5xx/network retryable) without
  ever echoing the key.
- ``/api/settings`` — the JSON surface the UI drives: add/remove/test keys per
  provider, per-engine defaults, availability from keys. Raw keys never
  appear in any payload.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.keys import KeyStore
from app.engines.registry import ENGINES
from app.main import create_app
from app.providers import catalog
from app.providers.gemini import keytest

PROVIDER_ENGINES = {pid: info.engines for pid, info in catalog.PROVIDERS.items()}
KEY = "AIza" + "0123456789abcdef" * 3  # realistic shape, 52 chars


def make_store(tmp_path: Path) -> KeyStore:
    return KeyStore(tmp_path / "keys.json", provider_engines=PROVIDER_ENGINES, engines=ENGINES)


def make_client(
    tmp_path: Path,
    *,
    key_store: KeyStore | None = None,
    key_probe: Any = None,
) -> TestClient:
    app = create_app(
        key_store=key_store if key_store is not None else make_store(tmp_path),
        key_probe=key_probe,
        projects_root=tmp_path / "projects",
    )
    return TestClient(app)


def probe_transport(
    handler: Any,
) -> tuple[list[httpx.Request], httpx.MockTransport]:
    requests: list[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    return requests, httpx.MockTransport(recording_handler)


def run_probe(
    handler: Any,
    *,
    api_key: str = KEY,
) -> tuple[list[httpx.Request], Any]:
    requests, transport = probe_transport(handler)
    result = keytest.test_key(api_key, transport=transport)
    return requests, result


# --- catalog -------------------------------------------------------------------


def test_gemini_is_the_first_catalog_provider_and_serves_all_three_engines():
    info = catalog.provider_info("gemini")
    assert info is not None
    assert info.label == "Google Gemini"
    assert set(info.engines) == set(ENGINES)
    assert info.key_url.startswith("https://")
    assert info.key_hint  # the UI explains where a key comes from


def test_catalog_lookup_of_unknown_provider_is_none():
    assert catalog.provider_info("sora") is None


def test_probe_dispatch_goes_through_the_catalog():
    requests, result = run_probe(lambda request: httpx.Response(200, json={"voices": []}))
    assert requests, "catalog dispatch must reach the gemini probe"
    assert result.ok is True


def test_probe_dispatch_of_unknown_provider_raises():
    with pytest.raises(KeyError, match="sora"):
        catalog.test_provider_key("sora", KEY)


# --- gemini probe: request shape (§6.5: the key goes only to its provider) --------


def test_probe_sends_key_only_in_header_to_the_documented_endpoint():
    requests, result = run_probe(lambda request: httpx.Response(200, json={"voices": []}))
    request = requests[0]
    assert request.method == "GET"
    assert request.url.host == "generativelanguage.googleapis.com"
    assert request.url.path == "/v1beta/voices"
    assert request.headers["x-goog-api-key"] == KEY
    assert "key" not in request.url.params  # never in the query string
    assert result.ok is True


def test_probe_success_message_reports_the_check_without_spend():
    _, result = run_probe(lambda request: httpx.Response(200, json={"voices": [1, 2, 3]}))
    assert result.ok is True
    assert "key" in result.message.lower()


# --- gemini probe: error mapping (research spike §7: status first) -----------------


def gemini_error(code: str, message: str, *, status: int) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message}})


def test_probe_401_maps_to_invalid_key_with_the_api_detail():
    requests, result = run_probe(
        lambda request: gemini_error(
            "authentication", "API key not valid. Please pass a valid API key.", status=401
        )
    )
    assert result.ok is False
    assert result.retryable is False
    assert "API key not valid" in result.message
    assert "401" in result.message
    assert KEY not in result.message


def test_probe_403_surfaces_the_unrestricted_key_cause():
    # Research §6: unrestricted standard keys are rejected; first-key UX must
    # surface it or the user churns on a valid-but-misconfigured key.
    _, result = run_probe(
        lambda request: gemini_error("permission_denied", "Permission denied.", status=403)
    )
    assert result.ok is False
    assert result.retryable is False
    assert "restricted" in result.message.lower()


def test_probe_429_is_reported_as_retryable_quota():
    _, result = run_probe(
        lambda request: gemini_error("rate_limit_exceeded", "Resource exhausted.", status=429)
    )
    assert result.ok is False
    assert result.retryable is True


def test_probe_5xx_is_reported_as_retryable_provider_error():
    for status in (500, 503):
        _, result = run_probe(
            lambda request, status=status: gemini_error("api_error", "Server error.", status=status)
        )
        assert result.ok is False
        assert result.retryable is True


def test_probe_unreachable_network_is_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _, result = run_probe(handler)
    assert result.ok is False
    assert result.retryable is True
    assert "reach" in result.message.lower()
    assert KEY not in result.message


def test_probe_timeout_is_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    _, result = run_probe(handler)
    assert result.ok is False
    assert result.retryable is True


def test_probe_unexpected_status_is_reported_plainly():
    _, result = run_probe(lambda request: httpx.Response(418, json={}))
    assert result.ok is False
    assert "418" in result.message


def test_probe_non_json_error_body_still_produces_a_message():
    _, result = run_probe(lambda request: httpx.Response(502, text="<html>bad gateway</html>"))
    assert result.ok is False
    assert result.retryable is True


# --- /api/settings JSON -------------------------------------------------------------


def test_settings_payload_lists_providers_engines_and_empty_defaults(tmp_path: Path):
    client = make_client(tmp_path)
    payload = client.get("/api/settings").json()
    gemini = next(p for p in payload["providers"] if p["id"] == "gemini")
    assert gemini["label"] == "Google Gemini"
    assert gemini["key_configured"] is False
    assert gemini["key_masked"] is None
    for engine in ENGINES:
        entry = payload["engines"][engine]
        ids = [p["id"] for p in entry["providers"]]
        assert "gemini" in ids
        assert entry["mock_available"] is True
        assert entry["default"] is None


def test_add_key_round_trip_masks_and_persists(tmp_path: Path):
    client = make_client(tmp_path)
    resp = client.put("/api/settings/keys/gemini", json={"api_key": KEY})
    assert resp.status_code == 200
    body = resp.json()
    assert body["key_configured"] is True
    assert body["key_masked"].endswith(KEY[-4:])
    assert KEY not in resp.text  # the raw key never comes back over the wire
    payload = client.get("/api/settings").json()
    gemini = next(p for p in payload["providers"] if p["id"] == "gemini")
    assert gemini["key_configured"] is True
    stored = make_store(tmp_path)  # fresh instance sees the persisted file
    assert stored.get_key("gemini") == KEY


def test_replacing_a_key_updates_the_mask(tmp_path: Path):
    client = make_client(tmp_path)
    client.put("/api/settings/keys/gemini", json={"api_key": "A" * 40})
    resp = client.put("/api/settings/keys/gemini", json={"api_key": "B" * 40})
    assert resp.json()["key_masked"].endswith("B" * 4)


def test_add_key_unknown_provider_is_404(tmp_path: Path):
    client = make_client(tmp_path)
    resp = client.put("/api/settings/keys/sora", json={"api_key": KEY})
    assert resp.status_code == 404


def test_add_blank_key_is_400_and_persists_nothing(tmp_path: Path):
    client = make_client(tmp_path)
    resp = client.put("/api/settings/keys/gemini", json={"api_key": "   "})
    assert resp.status_code == 400
    assert "non-blank" in resp.json()["detail"]
    assert client.get("/api/settings").json()["providers"][0]["key_configured"] is False


def test_remove_key_round_trip(tmp_path: Path):
    client = make_client(tmp_path)
    client.put("/api/settings/keys/gemini", json={"api_key": KEY})
    assert client.delete("/api/settings/keys/gemini").json() == {"removed": True}
    assert client.delete("/api/settings/keys/gemini").status_code == 404


def test_remove_unknown_provider_is_404(tmp_path: Path):
    assert make_client(tmp_path).delete("/api/settings/keys/sora").status_code == 404


def test_default_round_trip(tmp_path: Path):
    client = make_client(tmp_path)
    client.put("/api/settings/keys/gemini", json={"api_key": KEY})
    resp = client.post("/api/settings/defaults/tts", json={"provider": "gemini"})
    assert resp.status_code == 200
    payload = client.get("/api/settings").json()
    assert payload["engines"]["tts"]["default"] == "gemini"
    assert payload["defaults"] == {"tts": "gemini"}


def test_default_without_a_key_is_400(tmp_path: Path):
    client = make_client(tmp_path)
    resp = client.post("/api/settings/defaults/tts", json={"provider": "gemini"})
    assert resp.status_code == 400
    assert "no api key" in resp.json()["detail"].lower()


def test_default_for_unknown_engine_or_provider_is_404(tmp_path: Path):
    client = make_client(tmp_path)
    client.put("/api/settings/keys/gemini", json={"api_key": KEY})
    subtitles = client.post("/api/settings/defaults/subtitles", json={"provider": "gemini"})
    assert subtitles.status_code == 404
    assert client.post("/api/settings/defaults/tts", json={"provider": "sora"}).status_code == 404


def test_removing_a_key_clears_its_default(tmp_path: Path):
    client = make_client(tmp_path)
    client.put("/api/settings/keys/gemini", json={"api_key": KEY})
    client.post("/api/settings/defaults/tts", json={"provider": "gemini"})
    client.delete("/api/settings/keys/gemini")
    payload = client.get("/api/settings").json()
    assert payload["engines"]["tts"]["default"] is None


def test_test_endpoint_needs_a_saved_key(tmp_path: Path):
    client = make_client(tmp_path)
    resp = client.post("/api/settings/keys/gemini/test")
    assert resp.status_code == 400
    assert "add" in resp.json()["detail"].lower()


def test_test_endpoint_probes_with_the_stored_key(tmp_path: Path):
    seen: list[tuple[str, str]] = []

    def probe(provider_id: str, api_key: str):
        seen.append((provider_id, api_key))
        return catalog.KeyTestResult(ok=True, message="Key works.")

    client = make_client(tmp_path, key_probe=probe)
    client.put("/api/settings/keys/gemini", json={"api_key": KEY})
    resp = client.post("/api/settings/keys/gemini/test")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "message": "Key works.", "retryable": False}
    assert seen == [("gemini", KEY)]


def test_test_endpoint_reports_probe_failure_as_data(tmp_path: Path):
    def probe(provider_id: str, api_key: str):
        return catalog.KeyTestResult(ok=False, message="Google rejected this key (401).")

    client = make_client(tmp_path, key_probe=probe)
    client.put("/api/settings/keys/gemini", json={"api_key": KEY})
    resp = client.post("/api/settings/keys/gemini/test")
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_test_endpoint_of_unknown_provider_is_404(tmp_path: Path):
    assert make_client(tmp_path).post("/api/settings/keys/sora/test").status_code == 404


def test_settings_payload_never_contains_the_raw_key(tmp_path: Path):
    client = make_client(tmp_path)
    client.put("/api/settings/keys/gemini", json={"api_key": KEY})
    text = client.get("/api/settings").text
    assert KEY not in text
    assert json.loads(text)["providers"][0]["key_masked"].endswith(KEY[-4:])
