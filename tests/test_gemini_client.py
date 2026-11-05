"""Gemini HTTP client contract (BUILD_PLAN 2.2) — fully offline via MockTransport.

The client is the one HTTP layer every Gemini adapter (2.3 tts, 3.1 image,
4.1 video) rides on, so these tests pin the seams the adapters inherit:

- **Auth from the stored key** (§6.5): the key comes from the ``KeyStore`` at
  call time — never baked in at construction — and travels only in the
  ``x-goog-api-key`` header to Google's endpoint, never in the query string,
  messages, or repr. No key → a clear "what's missing" error and zero traffic.
- **Timeouts**: built from ``timeout_s`` (generous read for slow generation
  calls, short connect), observable on the instance, mapped when they fire.
- **Error mapping** (research spike §7): branch on HTTP status *first*, treat
  ``error.code`` as advisory detail; every failure becomes a
  ``GeminiError`` (a ``ProviderError``, so jobs.py's bounded retry sees it)
  whose message says exactly what's wrong/missing — and never echoes the key.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.keys import KeyStore
from app.engines.base import ProviderError
from app.providers.gemini import client as gemini_client
from app.providers.gemini.client import BASE_URL, GeminiClient, GeminiError

KEY = "AIza" + "0123456789abcdef" * 3  # realistic shape, never a real key
OTHER_KEY = "AIza" + "fedcba9876543210" * 3
INTERACTIONS_PATH = "/v1beta/interactions"


def make_store(tmp_path: Path) -> KeyStore:
    return KeyStore(tmp_path / "keys.json", provider_engines={"gemini": ("tts", "image", "video")})


def recording_transport(handler: Any) -> tuple[list[httpx.Request], httpx.MockTransport]:
    requests: list[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    return requests, httpx.MockTransport(recording_handler)


def make_client(
    handler: Any,
    *,
    key_source: Any = KEY,
    **kwargs: Any,
) -> tuple[list[httpx.Request], GeminiClient]:
    requests, transport = recording_transport(handler)
    return requests, GeminiClient(key_source, transport=transport, **kwargs)


def gemini_error_response(code: str, message: str, *, status: int) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message}})


# --- auth from the stored key -------------------------------------------------------


def test_post_json_sends_stored_key_only_in_the_header(tmp_path: Path):
    store = make_store(tmp_path)
    store.set_key("gemini", KEY)
    requests, client = make_client(
        lambda request: httpx.Response(200, json={"id": "v1_abc", "status": "completed"}),
        key_source=store,
    )
    body = client.post_json(INTERACTIONS_PATH, json={"model": "gemini-3.8-flash-tts"})
    assert body["id"] == "v1_abc"
    request = requests[0]
    assert request.method == "POST"
    assert request.url.host == "generativelanguage.googleapis.com"
    assert request.url.path == INTERACTIONS_PATH
    assert request.headers["x-goog-api-key"] == KEY
    assert "key" not in request.url.params  # never in the query string


def test_key_is_read_from_the_store_at_call_time(tmp_path: Path):
    store = make_store(tmp_path)
    requests, client = make_client(
        lambda request: httpx.Response(200, json={}), key_source=store
    )
    # No key yet → clear error, and no traffic left the machine.
    with pytest.raises(GeminiError) as exc_info:
        client.get_json(INTERACTIONS_PATH + "/v1_x")
    assert requests == []
    assert exc_info.value.retryable is False

    # Adding the key in Settings makes the *same* client work — no rebuild.
    store.set_key("gemini", KEY)
    assert client.get_json(INTERACTIONS_PATH + "/v1_x") == {}
    assert requests[0].headers["x-goog-api-key"] == KEY

    # Replacing the key in Settings takes effect on the next call too.
    store.set_key("gemini", OTHER_KEY)
    client.get_json(INTERACTIONS_PATH + "/v1_x")
    assert requests[-1].headers["x-goog-api-key"] == OTHER_KEY


def test_non_key_source_is_rejected_at_construction():
    # A key source is a KeyStore or a literal key string — anything else is a
    # programming error and must be rejected up front, before any call.
    with pytest.raises(ValueError, match="KeyStore or a non-blank key"):
        GeminiClient(None)
    with pytest.raises(ValueError, match="KeyStore or a non-blank key"):
        GeminiClient(123)  # type: ignore[arg-type]


def test_no_stored_key_raises_a_clear_error_without_any_http_call(tmp_path: Path):
    requests, client = make_client(
        lambda request: httpx.Response(200, json={}), key_source=make_store(tmp_path)
    )
    with pytest.raises(GeminiError) as exc_info:
        client.post_json(INTERACTIONS_PATH, json={})
    assert requests == []
    error = exc_info.value
    assert isinstance(error, ProviderError)
    assert error.provider_id == "gemini"
    assert error.retryable is False
    message = error.message.lower()
    assert "no gemini api key" in message
    assert "settings" in message
    assert "aistudio.google.com" in message  # where the key comes from


def test_blank_key_string_is_treated_as_missing():
    requests, client = make_client(lambda request: httpx.Response(200, json={}), key_source="   ")
    with pytest.raises(GeminiError) as exc_info:
        client.get_json(INTERACTIONS_PATH)
    assert requests == []
    assert "no gemini api key" in exc_info.value.message.lower()


def test_error_messages_and_repr_never_contain_the_key(tmp_path: Path):
    store = make_store(tmp_path)
    store.set_key("gemini", KEY)
    for key_source in (KEY, store):
        client = GeminiClient(key_source, base_url=BASE_URL)
        assert KEY not in repr(client)
        assert KEY not in str(client)


# --- timeouts ------------------------------------------------------------------------


def test_timeout_is_built_from_timeout_s_with_a_short_connect():
    default_client = GeminiClient(KEY)
    assert default_client.timeout.connect == 10.0
    assert default_client.timeout.read == gemini_client.DEFAULT_TIMEOUT_S
    fast = GeminiClient(KEY, timeout_s=5.0)
    assert fast.timeout.connect == 5.0  # min(10, 5) — never longer than the total
    assert fast.timeout.read == 5.0


def test_read_timeout_maps_to_a_retryable_message():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    _, client = make_client(handler)
    with pytest.raises(GeminiError) as exc_info:
        client.post_json(INTERACTIONS_PATH, json={})
    error = exc_info.value
    assert error.retryable is True
    assert "timed out" in error.message.lower()


# --- error mapping: status first, code advisory (research spike §7) --------------------


def test_401_is_a_permanent_invalid_key_error_with_the_api_detail():
    requests, client = make_client(
        lambda request: gemini_error_response(
            "authentication", "API key not valid. Please pass a valid API key.", status=401
        )
    )
    with pytest.raises(GeminiError) as exc_info:
        client.post_json(INTERACTIONS_PATH, json={})
    error = exc_info.value
    assert error.retryable is False
    assert error.http_status == 401
    assert error.api_code == "authentication"
    assert "401" in error.message
    assert "invalid" in error.message.lower()
    assert "API key not valid" in error.message  # Google's own sentence
    assert KEY not in error.message
    assert str(error) == error.message


def test_403_surfaces_the_permission_cause_including_the_unrestricted_key_gotcha():
    _, client = make_client(
        lambda request: gemini_error_response("permission_denied", "Permission denied.", status=403)
    )
    with pytest.raises(GeminiError) as exc_info:
        client.post_json(INTERACTIONS_PATH, json={})
    error = exc_info.value
    assert error.retryable is False
    assert error.http_status == 403
    assert "403" in error.message
    assert "restricted" in error.message.lower()


def test_404_points_at_the_model_id():
    _, client = make_client(
        lambda request: gemini_error_response("model_not_found", "Model not found.", status=404)
    )
    with pytest.raises(GeminiError) as exc_info:
        client.post_json(INTERACTIONS_PATH, json={})
    error = exc_info.value
    assert error.retryable is False
    assert "404" in error.message
    assert "model" in error.message.lower()


def test_400_is_permanent_and_names_the_request_and_the_billing_case():
    _, client = make_client(
        lambda request: gemini_error_response("invalid_request", "Bad parameter.", status=400)
    )
    with pytest.raises(GeminiError) as exc_info:
        client.post_json(INTERACTIONS_PATH, json={})
    error = exc_info.value
    assert error.retryable is False
    assert "400" in error.message
    assert "billing" in error.message.lower()


@pytest.mark.parametrize(
    ("code", "needle"),
    [("rate_limit_exceeded", "rate"), ("quota_exceeded", "quota")],
)
def test_429_is_retryable_and_distinguishes_rate_limit_from_quota(code: str, needle: str):
    _, client = make_client(
        lambda request, code=code: gemini_error_response(code, "Resource exhausted.", status=429)
    )
    with pytest.raises(GeminiError) as exc_info:
        client.post_json(INTERACTIONS_PATH, json={})
    error = exc_info.value
    assert error.retryable is True
    assert error.http_status == 429
    assert needle in error.message.lower()


@pytest.mark.parametrize("status", [500, 503, 504])
def test_5xx_is_retryable(status: int):
    _, client = make_client(
        lambda request, status=status: gemini_error_response(
            "api_error", "Server error.", status=status
        )
    )
    with pytest.raises(GeminiError) as exc_info:
        client.post_json(INTERACTIONS_PATH, json={})
    error = exc_info.value
    assert error.retryable is True
    assert str(status) in error.message


def test_unexpected_status_is_reported_plainly_and_is_not_retried():
    _, client = make_client(lambda request: httpx.Response(418, json={}))
    with pytest.raises(GeminiError) as exc_info:
        client.post_json(INTERACTIONS_PATH, json={})
    assert exc_info.value.retryable is False
    assert "418" in exc_info.value.message


def test_non_json_error_body_still_produces_a_retryable_message():
    _, client = make_client(lambda request: httpx.Response(502, text="<html>bad gateway</html>"))
    with pytest.raises(GeminiError) as exc_info:
        client.post_json(INTERACTIONS_PATH, json={})
    assert exc_info.value.retryable is True
    assert "502" in exc_info.value.message


def test_blocked_content_code_is_permanent_regardless_of_status():
    _, client = make_client(
        lambda request: gemini_error_response(
            "content_blocked", "The response was blocked.", status=400
        )
    )
    with pytest.raises(GeminiError) as exc_info:
        client.post_json(INTERACTIONS_PATH, json={})
    error = exc_info.value
    assert error.retryable is False
    assert "blocked" in error.message.lower()
    assert "content_blocked" in error.message


def test_connect_error_is_retryable_and_names_the_network():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _, client = make_client(handler)
    with pytest.raises(GeminiError) as exc_info:
        client.post_json(INTERACTIONS_PATH, json={})
    error = exc_info.value
    assert error.retryable is True
    assert "reach" in error.message.lower()
    assert KEY not in error.message


# --- success-envelope contract ---------------------------------------------------------


def test_get_json_returns_the_parsed_object():
    requests, client = make_client(
        lambda request: httpx.Response(200, json={"interaction": {"id": "v1_x"}})
    )
    body = client.get_json(INTERACTIONS_PATH + "/v1_x")
    assert body == {"interaction": {"id": "v1_x"}}
    assert requests[0].method == "GET"
    assert requests[0].headers["x-goog-api-key"] == KEY


def test_200_body_that_is_not_json_is_a_clear_error():
    _, client = make_client(lambda request: httpx.Response(200, text="<html>hi</html>"))
    with pytest.raises(GeminiError) as exc_info:
        client.get_json(INTERACTIONS_PATH + "/v1_x")
    assert "not JSON" in exc_info.value.message


def test_200_body_that_is_not_a_json_object_is_a_clear_error():
    _, client = make_client(lambda request: httpx.Response(200, json=[1, 2, 3]))
    with pytest.raises(GeminiError) as exc_info:
        client.get_json(INTERACTIONS_PATH + "/v1_x")
    assert "object" in exc_info.value.message.lower()


def test_download_returns_bytes_and_maps_errors():
    payload = b"\x00\x01binary-audio"
    requests, client = make_client(lambda request: httpx.Response(200, content=payload))
    assert client.download(INTERACTIONS_PATH + "/v1_x/file") == payload
    assert requests[0].headers["x-goog-api-key"] == KEY

    # Absolute URLs (delivery=uri file downloads) are supported as-is — but the
    # key never travels to a host other than the Gemini API itself (§6.5).
    requests2, client2 = make_client(lambda request: httpx.Response(200, content=b"abc"))
    assert client2.download("https://example.com/file.wav") == b"abc"
    assert requests2[0].headers.get("x-goog-api-key") is None

    _, client3 = make_client(
        lambda request: gemini_error_response("permission_denied", "Denied.", status=403)
    )
    with pytest.raises(GeminiError) as exc_info:
        client3.download("https://example.com/file.wav")
    assert exc_info.value.retryable is False
    assert "403" in exc_info.value.message


# --- payload mapping for non-HTTP error delivery (SSE events, 2.3/4.1) ------------------


def test_error_from_payload_maps_codes_without_an_http_status():
    from_payload = gemini_client.error_from_payload
    rate = from_payload({"code": "rate_limit_exceeded", "message": "Too fast."})
    assert rate.retryable is True
    blocked = from_payload({"code": "safety", "message": "Blocked."})
    assert blocked.retryable is False
    assert "blocked" in blocked.message.lower()
    invalid = from_payload({"code": "authentication", "message": "Key not valid."})
    assert invalid.retryable is False
    unknown = from_payload({"message": "Something odd."})
    assert "Something odd." in unknown.message


def test_error_from_payload_with_a_status_defers_to_the_status_first():
    # A legacy 503 carrying a misspelled code must stay retryable (spike §7 caveat).
    err = gemini_client.error_from_payload(
        {"code": "RESOURCE_EXHAUSTED", "message": "Quota."}, http_status=503
    )
    assert err.retryable is True
    assert err.http_status == 503
