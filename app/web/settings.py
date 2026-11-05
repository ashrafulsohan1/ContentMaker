"""Settings screen: BYOK key management (BUILD_PLAN 2.1, spec §6.5/§8).

Server-rendered htmx like the rest of the UI (no build step): every POST is a
plain form submission that 303-redirects back to ``/settings`` with a one-shot
notice/error; htmx requests get the ``#settings-body`` fragment instead.
Behind the forms sits the same surface as ``app.api.settings`` — the
``KeyStore`` (gitignored ``state/keys.json``, §6.5) and the catalog's
per-provider key probe, which dials **only the provider the key belongs to**.

Key hygiene in this layer:
- the ``api_key`` form field is write-only — no handler or template ever
  receives a raw key back; the page renders ``KeyStore.mask_key`` output;
- test results and save/remove/default errors are shown verbatim from the
  store/probe messages, which never contain the key.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.api.settings import probe_of, settings_payload, store_of
from app.core.keys import KeyStoreError
from app.providers.catalog import KeyTestResult

router = APIRouter()

templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")


def _context(
    request: Request,
    *,
    notice: str | None = None,
    error: str | None = None,
    test_result: KeyTestResult | None = None,
    test_provider: str | None = None,
) -> dict[str, Any]:
    return settings_payload(store_of(request)) | {
        "notice": notice,
        "error": error,
        "test_result": test_result,
        "test_provider": test_provider,
    }


def _response(
    request: Request,
    *,
    notice: str | None = None,
    error: str | None = None,
    test_result: KeyTestResult | None = None,
    test_provider: str | None = None,
) -> HTMLResponse | RedirectResponse:
    """htmx gets the updated fragment; plain forms redirect back to /settings."""
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request,
            "_settings_body.html",
            _context(
                request,
                notice=notice,
                error=error,
                test_result=test_result,
                test_provider=test_provider,
            ),
        )
    message = notice or error
    url = "/settings" + (f"?notice={quote(message)}" if message else "")
    return RedirectResponse(url, status_code=303)


@router.get("/settings")
def settings_page(request: Request):
    return templates.TemplateResponse(
        request,
        "settings.html",
        _context(
            request,
            notice=request.query_params.get("notice"),
            error=request.query_params.get("error"),
        ),
    )


@router.post("/settings/keys/{provider_id}")
async def save_key(provider_id: str, request: Request):
    form = await request.form()
    api_key = str(form.get("api_key") or "")
    try:
        store_of(request).set_key(provider_id, api_key)
    except KeyStoreError as exc:
        return _response(request, error=str(exc))
    label = provider_id
    info = settings_payload(store_of(request))
    for entry in info["providers"]:
        if entry["id"] == provider_id:
            label = entry["label"]
    return _response(
        request,
        notice=f"{label} key saved — it stays in your local state/ folder and"
        " is sent only to Google's API when you generate or test.",
    )


@router.post("/settings/keys/{provider_id}/remove")
def remove_key(provider_id: str, request: Request):
    try:
        removed = store_of(request).remove_key(provider_id)
    except KeyStoreError as exc:
        return _response(request, error=str(exc))
    if not removed:
        return _response(request, error=f"No key is saved for '{provider_id}' — nothing to remove.")
    return _response(
        request,
        notice="Key removed — the engines it served fall back to dry-run mocks"
        " until another key is added.",
    )


@router.post("/settings/keys/{provider_id}/test")
def test_key(provider_id: str, request: Request):
    store = store_of(request)
    key = store.get_key(provider_id)
    if key is None:
        return _response(
            request,
            error=f"No key is saved for '{provider_id}' yet — add one first, then test it.",
        )
    try:
        result: KeyTestResult = probe_of(request)(provider_id, key)
    except KeyError as exc:
        return _response(request, error=str(exc))
    if result.ok:
        return _response(
            request,
            notice=f"Key test: {result.message}",
            test_result=result,
            test_provider=provider_id,
        )
    return _response(
        request,
        error=f"Key test: {result.message}",
        test_result=result,
        test_provider=provider_id,
    )


@router.post("/settings/defaults/{engine}")
async def set_default(engine: str, request: Request):
    form = await request.form()
    provider_id = str(form.get("provider") or "")
    if not provider_id:
        return _response(request, error="Pick a provider to set as the default.")
    try:
        store_of(request).set_default(engine, provider_id)
    except KeyStoreError as exc:
        return _response(request, error=str(exc))
    return _response(request, notice=f"Default provider for the {engine} engine set.")
