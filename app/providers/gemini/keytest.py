"""Gemini key probe — zero-spend key validation for Settings (BUILD_PLAN 2.1).

Probes ``GET /v1beta/voices`` with the key in the ``x-goog-api-key`` header —
the documented, zero-spend probe from research spike 2.0 (research/gemini-api.md
§6; ``/v1beta/models`` never appeared in the official docs and is not assumed).
The key travels to Google's endpoint only, only in that header, and never into
messages, logs, or return values.

Error mapping follows the spike's §7 rule — **branch on HTTP status first**,
treat the ``error.code`` string as advisory detail — and reports exactly what's
wrong (§6.5): 401 invalid/expired key, 403 access denied (including the
unrestricted-standard-key rejection every new AI Studio key can hit), 429
quota, 5xx and network errors retryable.
"""

from __future__ import annotations

import httpx

from app.providers.catalog import KeyTestResult

BASE_URL = "https://generativelanguage.googleapis.com"
PROBE_PATH = "/v1beta/voices"
_AUTH_HEADER = "x-goog-api-key"


def test_key(
    api_key: str,
    *,
    transport: httpx.BaseTransport | None = None,
    timeout_s: float = 10.0,
) -> KeyTestResult:
    """Ask Google's API whether this key is accepted — no spend, no side effects."""
    headers = {_AUTH_HEADER: api_key}
    try:
        with httpx.Client(transport=transport, timeout=timeout_s) as client:
            response = client.get(BASE_URL + PROBE_PATH, headers=headers)
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        return KeyTestResult(
            ok=False,
            message=f"Could not reach Google's API ({exc.__class__.__name__}) —"
            " check your internet connection and try again.",
            retryable=True,
        )
    detail = _error_detail(response)
    if response.status_code == 200:
        return KeyTestResult(
            ok=True,
            message="Key works — Google accepted it (checked without any spend).",
        )
    if response.status_code == 401:
        return KeyTestResult(
            ok=False,
            message=(
                "Google rejected this key (401) — it is missing, invalid, or expired."
                f"{detail} Create a new key at https://aistudio.google.com/apikey."
            ),
        )
    if response.status_code == 403:
        return KeyTestResult(
            ok=False,
            message=(
                "Google denied access with this key (403) — the key exists but lacks"
                " permission. Most common cause: an *unrestricted* standard key, which"
                " the API rejects; re-create the key with a Gemini API restriction."
                f"{detail}"
            ),
        )
    if response.status_code == 429:
        return KeyTestResult(
            ok=False,
            message=(
                "The key is valid but Google rate-limited or quota-limited it (429)."
                " Wait a moment and test again. This is about Google's limits, not"
                " about the key itself."
                f"{detail}"
            ),
            retryable=True,
        )
    if response.status_code >= 500:
        return KeyTestResult(
            ok=False,
            message=(
                f"Google's API had a server error ({response.status_code}) while"
                " validating the key — try again in a moment."
                f"{detail}"
            ),
            retryable=True,
        )
    return KeyTestResult(
        ok=False,
        message=f"Unexpected response from Google's API (HTTP {response.status_code}).{detail}",
    )


def _error_detail(response: httpx.Response) -> str:
    """The API's own error sentence, if any — Google's envelope is
    ``{"error": {"code": "<snake_case>", "message": "…"}}`` (spike §7)."""
    try:
        message = response.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return ""
    text = str(message).strip()
    if not text:
        return ""
    return f" Google says: {text[:300]}"
