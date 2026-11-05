"""Provider catalog — the known BYOK providers as editable metadata (BUILD_PLAN 2.1).

One :class:`ProviderInfo` per provider: which engines it serves, where its key
comes from, and how to validate a key. The catalog is the source the settings
surface (§6.5: engine availability from configured keys, per-engine defaults)
and the key store wiring in ``app.main`` are derived from; provider *adapters*
additionally register engine instances in the registry (M2 tasks 2.2/2.3+).

Adding a provider = one ``ProviderInfo`` here + a probe (so Settings can test
its keys) + adapters per engine, per BUILD_PLAN §1.2 "Provider abstraction".
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

PROBE_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class KeyTestResult:
    """Outcome of a key-validation probe; ``message`` is user-facing.

    It must say exactly what's wrong or missing (§6.5) and must never contain
    the key itself. ``retryable`` mirrors the provider error taxonomy
    (quota/server/network vs. permanent rejection).
    """

    ok: bool
    message: str
    retryable: bool = False


@dataclass(frozen=True)
class ProviderInfo:
    """One BYOK provider, as the Settings UI presents it."""

    id: str
    label: str
    engines: tuple[str, ...]  # engines this provider can serve with its key
    key_label: str  # e.g. "Gemini API key"
    key_hint: str  # one sentence: where the key comes from + known gotchas
    key_url: str  # where the user creates/manager the key
    docs_url: str


GEMINI = ProviderInfo(
    id="gemini",
    label="Google Gemini",
    engines=("tts", "image", "video"),  # one AI Studio key covers all three (§0)
    key_label="Gemini API key",
    key_hint=(
        "Create a key at Google AI Studio. New keys are 'authorization keys' —"
        " keep (or set) a Gemini API restriction on them: unrestricted standard"
        " keys are rejected by the API."
    ),
    key_url="https://aistudio.google.com/apikey",
    docs_url="https://ai.google.dev/gemini-api/docs/api-key",
)

PROVIDERS: dict[str, ProviderInfo] = {GEMINI.id: GEMINI}


def provider_info(provider_id: str) -> ProviderInfo | None:
    return PROVIDERS.get(provider_id)


# Probe dispatch: provider id → zero/min-spend key validation. Imported lazily
# so importing the catalog never pulls an HTTP client into non-network code.
_PROBES: dict[str, str] = {"gemini": "app.providers.gemini.keytest:test_key"}


def test_provider_key(
    provider_id: str,
    api_key: str,
    *,
    timeout_s: float = PROBE_TIMEOUT_S,
) -> KeyTestResult:
    """Validate one provider key against its own API (§6.5: test keys)."""
    target = _PROBES.get(provider_id)
    if target is None:
        known = ", ".join(sorted(_PROBES)) or "none"
        raise KeyError(f"no key probe for provider '{provider_id}' (probes: {known})")
    module_name, _, attr = target.partition(":")
    import importlib

    probe: Callable[..., KeyTestResult] = getattr(importlib.import_module(module_name), attr)
    return probe(api_key, timeout_s=timeout_s)
