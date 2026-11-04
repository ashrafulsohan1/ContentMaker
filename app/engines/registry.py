"""Provider registry (BUILD_PLAN 1.4) — BYOK keys plug in here (M2+).

One provider list per engine; lookups fail with messages that name the engine
and provider involved, so the UI can say exactly what's missing (§6.5).
Registering a provider whose id already exists for that engine replaces it
(config-driven defaults may be overridden by user-selected BYOK providers).
"""
from __future__ import annotations

from app.engines.base import ImageProvider, TTSProvider, VideoProvider

ENGINES: tuple[str, ...] = ("tts", "image", "video")

Provider = TTSProvider | ImageProvider | VideoProvider


class Registry:
    """Engine name → provider instances; ``get`` returns the same object."""

    def __init__(self) -> None:
        self._providers: dict[str, list[Provider]] = {engine: [] for engine in ENGINES}

    def register(self, engine: str, provider: Provider) -> None:
        providers = self._bucket(engine)
        provider_id = _provider_id(provider)
        providers[:] = [p for p in providers if _provider_id(p) != provider_id]
        providers.append(provider)

    def providers_for(self, engine: str) -> list[Provider]:
        return list(self._bucket(engine))

    def get(self, engine: str, provider_id: str) -> Provider:
        providers = self._bucket(engine)
        for provider in providers:
            if _provider_id(provider) == provider_id:
                return provider
        registered = ", ".join(_provider_id(p) for p in providers) or "none registered"
        raise ValueError(
            f"no '{provider_id}' provider for engine '{engine}' (registered: {registered})"
        )

    def _bucket(self, engine: str) -> list[Provider]:
        providers = self._providers.get(engine)
        if providers is None:
            raise ValueError(f"unknown engine '{engine}' (known engines: {', '.join(ENGINES)})")
        return providers


def _provider_id(provider: Provider) -> str:
    provider_id = getattr(provider, "provider_id", None)
    if not provider_id:
        raise ValueError(
            f"provider {type(provider).__name__} must expose a truthy 'provider_id' attribute"
        )
    return str(provider_id)
