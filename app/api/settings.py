"""JSON API: BYOK settings (BUILD_PLAN 2.1, spec §6.5).

Add/remove/test API keys per provider, per-engine defaults, and the
key-derived engine availability — the data behind the Settings screen
(``app.web.settings``). Key rules enforced at this boundary:

- the raw key is accepted on add and **never returned**: every payload and
  error carries at most the ``••••last4`` mask (``KeyStore.mask_key``);
- a key is validated (non-blank, no line breaks) and persisted to the
  gitignored ``state/keys.json`` by the ``KeyStore`` atomically;
- testing a key dials **only the provider the key belongs to** (the catalog's
  probe dispatch), and reports the probe outcome as data, not as an HTTP error;
- defaults require a known engine, a provider serving it, and a configured
  key — so ``default_for`` is always job-safe for readers.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.core.keys import KeyStore, KeyStoreError
from app.engines.registry import ENGINES
from app.providers.catalog import PROVIDERS, ProviderInfo

router = APIRouter(prefix="/api/settings")

KeyProbe = Any  # Callable[[provider_id, api_key], KeyTestResult]; injected for tests


class ApiKeyBody(BaseModel):
    api_key: str


class DefaultBody(BaseModel):
    provider: str


def store_of(request: Request) -> KeyStore:
    return request.app.state.key_store


def probe_of(request: Request) -> KeyProbe:
    return request.app.state.key_probe


def provider_entry(store: KeyStore, info: ProviderInfo) -> dict[str, Any]:
    return {
        "id": info.id,
        "label": info.label,
        "engines": list(info.engines),
        "key_label": info.key_label,
        "key_hint": info.key_hint,
        "key_url": info.key_url,
        "docs_url": info.docs_url,
        "key_configured": store.has_key(info.id),
        "key_masked": store.mask_key(info.id),
    }


def settings_payload(store: KeyStore) -> dict[str, Any]:
    """Providers, key state (masked), and key-derived engine availability."""
    engines: dict[str, Any] = {}
    defaults = store.defaults()
    for engine in ENGINES:
        providers = [
            {
                "id": info.id,
                "label": info.label,
                "key_configured": store.has_key(info.id),
                "is_default": defaults.get(engine) == info.id,
            }
            for info in PROVIDERS.values()
            if engine in info.engines
        ]
        engines[engine] = {
            "providers": providers,
            "mock_available": True,  # dry-run mocks are always on offer (§1.2)
            "default": defaults.get(engine),
        }
    return {
        "providers": [provider_entry(store, info) for info in PROVIDERS.values()],
        "engines": engines,
        "defaults": defaults,
    }


def _require_provider(provider_id: str) -> ProviderInfo:
    info = PROVIDERS.get(provider_id)
    if info is None:
        raise HTTPException(status_code=404, detail=f"unknown provider '{provider_id}'")
    return info


def _require_engine(engine: str) -> None:
    if engine not in ENGINES:
        raise HTTPException(
            status_code=404,
            detail=f"unknown engine '{engine}' (known engines: {', '.join(ENGINES)})",
        )


@router.get("")
def get_settings(request: Request) -> dict[str, Any]:
    return settings_payload(store_of(request))


@router.put("/keys/{provider_id}")
def put_key(provider_id: str, body: ApiKeyBody, request: Request) -> dict[str, Any]:
    info = _require_provider(provider_id)
    store = store_of(request)
    try:
        store.set_key(info.id, body.api_key)
    except KeyStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return provider_entry(store, info)


@router.delete("/keys/{provider_id}")
def delete_key(provider_id: str, request: Request) -> dict[str, Any]:
    _require_provider(provider_id)
    if not store_of(request).remove_key(provider_id):
        raise HTTPException(
            status_code=404, detail=f"no key is configured for provider '{provider_id}'"
        )
    return {"removed": True}


@router.post("/keys/{provider_id}/test")
def test_key(provider_id: str, request: Request) -> dict[str, Any]:
    _require_provider(provider_id)
    store = store_of(request)
    key = store.get_key(provider_id)
    if key is None:
        raise HTTPException(
            status_code=400,
            detail=f"No API key is saved for '{provider_id}' yet — add one first, then test it.",
        )
    result = probe_of(request)(provider_id, key)
    return {"ok": result.ok, "message": result.message, "retryable": result.retryable}


@router.post("/defaults/{engine}")
def put_default(engine: str, body: DefaultBody, request: Request) -> dict[str, Any]:
    _require_engine(engine)
    _require_provider(body.provider)
    try:
        store_of(request).set_default(engine, body.provider)
    except KeyStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"engine": engine, "default": body.provider}
