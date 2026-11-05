"""Gemini HTTP client — the one outbound-HTTP layer for every Gemini adapter.

BUILD_PLAN 2.2: httpx, auth from the stored key, timeouts, and error mapping
into messages that name **exactly what is missing or wrong** (spec §6.5).
The adapters (2.3 tts, 3.1 image, 4.1 video) ride on this client, so the
BYOK invariants are enforced in exactly one place:

- **Auth from the stored key** — the key is read from the ``KeyStore`` at
  call time (adding/replacing a key in Settings takes effect on the next
  call, no rebuild), or given directly as a string for probes/tests. It
  travels only in the ``x-goog-api-key`` header and **only to the Gemini API
  host** — never in the query string, never to a third-party download host,
  never into messages, ``str()`` or ``repr()``. A missing/blank key fails
  *before any traffic* with a message saying what to add where.
- **Timeouts** — one ``timeout_s`` becomes a generous read/write budget
  (generation calls are slow) with a short connect budget, exposed as
  ``client.timeout`` and mapped into a retryable error when it fires.
- **Error mapping** (research spike §7: branch on HTTP status *first*,
  treat ``error.code`` as advisory detail) into :class:`GeminiError`, a
  :class:`~app.engines.base.ProviderError` — so ``app.core.jobs`` bounded
  retry sees ``retryable`` and the UI sees ``message``. Every failure mode
  (401 key, 403 permission, 404 model, 400 request/billing, 429 rate vs
  quota, 5xx, timeouts, network, policy blocks, malformed bodies) maps to a
  distinct, user-facing sentence. Policy/safety blocks are permanent no
  matter the status (spike §7: "message the user, do not retry").

All requests run through ``httpx.MockTransport`` in tests — fully offline.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.keys import KeyStore
from app.engines.base import ProviderError
from app.providers.catalog import GEMINI

PROVIDER_ID = "gemini"
BASE_URL = "https://generativelanguage.googleapis.com"
AUTH_HEADER = "x-goog-api-key"

DEFAULT_TIMEOUT_S = 120.0  # generation calls (audio/image/video) can be slow
CONNECT_TIMEOUT_S = 10.0  # but an unreachable host should fail fast

#: Generation blocks (policy/safety) from spike §7 — permanent, never retried.
BLOCKED_CODES = frozenset(
    {
        "safety",
        "recitation",
        "language",
        "prohibited_content",
        "spii",
        "blocklist",
        "image_safety",
        "image_prohibited_content",
        "image_recitation",
        "image_other",
        "content_blocked",
    }
)

#: Known error codes → retryable, for the code-first branch (no HTTP status,
#: e.g. SSE ``event_type: "error"`` events in streaming responses — spike §7).
RETRYABLE_CODES = frozenset(
    {
        "rate_limit_exceeded",
        "too_many_requests",
        "quota_exceeded",  # retryable-but-slow: jobs' backoff + user message
        "api_error",
        "service_unavailable",
        "deadline_exceeded",
    }
)


class GeminiError(ProviderError):
    """A failed Gemini call; ``message`` is user-facing and never names the key.

    ``http_status`` / ``api_code`` carry the raw evidence for tests and logs;
    ``retryable`` follows spike §7 and drives jobs.py's bounded retry.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        http_status: int | None = None,
        api_code: str | None = None,
    ) -> None:
        super().__init__(PROVIDER_ID, message, retryable=retryable)
        self.http_status = http_status
        self.api_code = api_code


class GeminiClient:
    """Synchronous JSON/bytes client for the Gemini API (Interactions surface).

    ``key_source`` is a :class:`~app.core.keys.KeyStore` (key read live, per
    call) or the literal key string. ``transport`` accepts an
    ``httpx.MockTransport`` for offline tests. One client is safe to share
    across worker threads — each call opens its own short-lived httpx client.
    """

    def __init__(
        self,
        key_source: KeyStore | str,
        *,
        transport: httpx.BaseTransport | None = None,
        base_url: str = BASE_URL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        if not isinstance(key_source, (KeyStore, str)):
            raise ValueError(
                "GeminiClient needs a KeyStore or a non-blank key string as its"
                f" key source, got {type(key_source).__name__}."
            )
        self._key_source = key_source
        self._transport = transport
        self._base_url = base_url.rstrip("/")
        self._base_host = httpx.URL(self._base_url).host
        self.timeout = httpx.Timeout(timeout_s, connect=min(CONNECT_TIMEOUT_S, timeout_s))

    def __repr__(self) -> str:  # the key must never leak through repr/str
        state = "configured" if self.api_key() else "missing"
        return f"GeminiClient(base_url={self._base_url!r}, key={state})"

    # --- key resolution -------------------------------------------------------------

    def api_key(self) -> str | None:
        """The resolved key, fetched at call time so Settings changes apply."""
        if isinstance(self._key_source, KeyStore):
            key = self._key_source.get_key(PROVIDER_ID)
        else:
            key = self._key_source
        key = (key or "").strip()
        return key or None

    def _require_key(self) -> str:
        key = self.api_key()
        if not key:
            raise GeminiError(
                f"No {GEMINI.key_label} is configured — add one in Settings"
                f" ({GEMINI.key_url}) and try again.",
                retryable=False,
            )
        return key

    # --- request surface ------------------------------------------------------------

    def post_json(self, path: str, *, json: Any = None) -> dict[str, Any]:
        """POST a JSON body, return the JSON-object response."""
        return self._json_object(self.request("POST", path, json=json))

    def get_json(self, path: str) -> dict[str, Any]:
        """GET a JSON-object response (interactions polling, file metadata)."""
        return self._json_object(self.request("GET", path))

    def download(self, path_or_url: str) -> bytes:
        """GET raw bytes (file downloads); the key is attached only to the
        Gemini API host — third-party/signed download URLs get no credentials."""
        return self.request("GET", path_or_url).content

    def request(self, method: str, path_or_url: str, *, json: Any = None) -> httpx.Response:
        """One authenticated request with mapped errors; 2xx responses return."""
        key = self._require_key()
        url = (
            path_or_url
            if path_or_url.startswith(("http://", "https://"))
            else self._base_url + path_or_url
        )
        headers = {AUTH_HEADER: key} if httpx.URL(url).host == self._base_host else {}
        try:
            with httpx.Client(transport=self._transport, timeout=self.timeout) as http:
                response = http.request(method, url, json=json, headers=headers)
        except httpx.TimeoutException as exc:
            raise GeminiError(
                f"The request to the Gemini API timed out after {self.timeout.read:.0f}s"
                " — check your internet connection; it will be retried automatically.",
                retryable=True,
            ) from exc
        except httpx.TransportError as exc:  # connect/read/write failures
            raise GeminiError(
                f"Could not reach the Gemini API ({exc.__class__.__name__}) —"
                " check your internet connection and try again; it will be"
                " retried automatically.",
                retryable=True,
            ) from exc
        self._raise_for_status(response)
        return response

    # --- error mapping (spike §7: status first, code advisory) -----------------------

    def _raise_for_status(self, response: httpx.Response) -> None:
        if 200 <= response.status_code < 300:
            return
        raise error_from_response(response)

    def _json_object(self, response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            raise GeminiError(
                "The Gemini API returned an unexpected response"
                f" (HTTP {response.status_code}): the body is not JSON.",
                retryable=False,
                http_status=response.status_code,
            ) from None
        if not isinstance(payload, dict):
            raise GeminiError(
                "The Gemini API returned an unexpected response"
                f" (HTTP {response.status_code}): the body is JSON but not an"
                f" object (got {type(payload).__name__}).",
                retryable=False,
                http_status=response.status_code,
            )
        return payload


def error_from_response(response: httpx.Response) -> GeminiError:
    """Map a non-2xx HTTP response to a :class:`GeminiError` (status first)."""
    payload: Any = None
    try:
        payload = response.json()
    except ValueError:
        payload = None
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        error = {}
    return error_from_payload(error, http_status=response.status_code)


def error_from_payload(payload: dict[str, Any], *, http_status: int | None = None) -> GeminiError:
    """Map an ``{"error": {"code", "message"}}`` payload to a :class:`GeminiError`.

    With an ``http_status`` the status decides (spike §7 caveat: legacy
    surfaces may spell codes differently, e.g. ``RESOURCE_EXHAUSTED``).
    Without one (SSE error events), the known ``code`` table decides; unknown
    codes default to retryable — burning bounded retries beats permanently
    failing an asset on a transient hiccup.
    """
    api_code = str(payload.get("code") or "") or None
    detail = _detail(payload)
    if api_code in BLOCKED_CODES:  # policy blocks are permanent at any status
        return GeminiError(
            f"Google blocked this content for policy/safety reasons (code:"
            f" {api_code}) — revise the text or prompt and try again;"
            " retrying as-is will not succeed."
            f"{detail}",
            retryable=False,
            http_status=http_status,
            api_code=api_code,
        )
    if http_status is not None:
        message, retryable = _message_for_status(http_status, api_code, detail)
        return GeminiError(
            message, retryable=retryable, http_status=http_status, api_code=api_code
        )
    retryable = api_code not in KNOWN_PERMANENT_CODES
    if api_code:
        message = (
            f"The Gemini API reported an error ({api_code})"
            + (" — it will be retried automatically." if retryable else
               " — retrying will not help.")
        )
    else:
        message = "The Gemini API reported an error without details."
    return GeminiError(
        f"{message}{detail}", retryable=retryable, http_status=None, api_code=api_code
    )


#: Codes spike §7 marks permanent (used when no HTTP status is available).
KNOWN_PERMANENT_CODES = frozenset(
    {
        "authentication",
        "permission_denied",
        "not_found",
        "model_not_found",
        "invalid_request",
        "parameter_unknown",
        "failed_precondition",
        "out_of_range",
    }
)


def _message_for_status(status: int, api_code: str | None, detail: str) -> tuple[str, bool]:
    """The user-facing sentence for an HTTP status — exactly what's wrong."""
    if status == 401:
        return (
            "Gemini rejected the API key (401) — it is missing, invalid, or"
            " expired. Update the key in Settings."
            f"{detail}"
        ), False
    if status == 403:
        return (
            "Gemini denied access with this key (403) — the key exists but"
            " lacks permission. Most common cause: an *unrestricted* standard"
            " key, which the API rejects — re-create the key with a Gemini API"
            " restriction."
            f"{detail}"
        ), False
    if status == 404:
        return (
            "Gemini could not find the requested resource (404) — usually a"
            " wrong or retired model ID; check the model name in the request."
            f"{detail}"
        ), False
    if status in (400, 416):
        return (
            "Gemini rejected the request as invalid"
            f" ({status}) — this is a bug in ContentMaker or an unsupported"
            " parameter; if Google's detail below mentions billing, enable"
            " billing on your Google Cloud project."
            f"{detail}"
        ), False
    if status == 429:
        if api_code and "quota" in api_code:
            return (
                "Gemini quota exhausted (429) — the key hit its usage quota"
                " (often the daily one). It will be retried automatically, but"
                " you may need to wait for the quota window to reset or use a"
                " different key."
                f"{detail}"
            ), True
        return (
            "Gemini rate-limited the request (429) — too many requests in a"
            " short window; it will be retried automatically."
            f"{detail}"
        ), True
    if status >= 500:
        return (
            f"Gemini's API had a server error ({status}) — it will be retried"
            " automatically; if it keeps failing, try again later."
            f"{detail}"
        ), True
    return f"Unexpected response from the Gemini API (HTTP {status}).{detail}", False


def _detail(payload: dict[str, Any]) -> str:
    """Google's own error sentence, if any — ``{"error": {"message": …}}``."""
    try:
        message = payload["message"]
    except (KeyError, TypeError):
        return ""
    text = str(message).strip()
    if not text:
        return ""
    return f" Google says: {text[:300]}"
