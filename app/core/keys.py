"""BYOK key store (BUILD_PLAN 2.1, spec §6.5) — the one place keys touch disk.

Keys live in a single gitignored file, ``<state>/keys.json`` next to the
projects tree (app.config.app_state_dir), and never leave the machine except
towards the provider they belong to:

- **Local only** — the file sits under ``state/`` which .gitignore excludes;
  writes are atomic (temp file + ``os.replace``) so a crash mid-write can
  never lose the previous keys, and the file is chmodded owner-only where the
  OS honors it (best-effort on Windows).
- **Provider-scoped** — the store holds a key per *provider id* (the catalog
  in ``app.providers.catalog`` defines which providers exist and which engines
  each serves, injected here as plain data so storage stays import-light).
- **Per-engine defaults** (§6.5) — a default provider per engine, settable only
  for a known engine, a provider that serves it, and a configured key;
  removing a key drops the defaults that point at it (an invariant, so readers
  can trust ``default_for`` without re-checking).
- **Masked by default** — UI/API surfaces use ``mask_key`` (``••••last4``);
  the raw key is reachable only through ``get_key`` for the code path that
  calls the provider itself. ``repr``/``str`` of the store never contain keys.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

KEYS_FILENAME = "keys.json"
_SCHEMA_VERSION = 1
_MASK = "••••"


class KeyStoreError(ValueError):
    """A key/default request the store cannot honor; message is user-facing."""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _replace_with_retry(tmp: Path, target: Path, *, attempts: int = 6) -> None:
    """``os.replace`` with a short bounded retry (BUILD_PLAN risk #7).

    On Windows/OneDrive a concurrent reader (or the sync client) can hold the
    target open for a few milliseconds, making the atomic swap raise
    ``PermissionError``; retrying briefly keeps the store durable without
    busy-spinning. The last error propagates if the lock never clears.
    """
    for attempt in range(attempts):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.02 * (attempt + 1))


class KeyStore:
    """Thread-safe, atomically-persisted API keys + per-engine defaults."""

    def __init__(
        self,
        path: Path,
        *,
        provider_engines: Mapping[str, Sequence[str]] | None = None,
        engines: Sequence[str] = (),
    ) -> None:
        self._path = Path(path)
        self._provider_engines: dict[str, tuple[str, ...]] = {
            pid: tuple(engines_) for pid, engines_ in (provider_engines or {}).items()
        }
        self._engines = tuple(engines)
        self._lock = threading.RLock()
        self._keys: dict[str, dict[str, str]] = {}
        self._defaults: dict[str, str] = {}
        self._load()

    # --- keys -------------------------------------------------------------------

    def set_key(self, provider_id: str, api_key: str) -> None:
        """Store (or replace) the key for one provider, validated and trimmed."""
        self._require_provider(provider_id)
        key = (api_key or "").strip()
        if not key:
            raise KeyStoreError(f"A non-blank API key is needed for provider '{provider_id}'.")
        if any(ch.isspace() or ord(ch) < 32 for ch in key):
            raise KeyStoreError(
                f"The API key for '{provider_id}' contains control characters or"
                " whitespace — paste the key itself, without line breaks."
            )
        with self._lock:
            record = self._keys.get(provider_id)
            added_at = record["added_at"] if record else _now_iso()
            self._keys[provider_id] = {
                "api_key": key,
                "added_at": added_at,
                "updated_at": _now_iso(),
            }
            self._save()

    def get_key(self, provider_id: str) -> str | None:
        """The raw key — only for the code path that calls this provider."""
        with self._lock:
            record = self._keys.get(provider_id)
            return record["api_key"] if record else None

    def has_key(self, provider_id: str) -> bool:
        with self._lock:
            return provider_id in self._keys

    def remove_key(self, provider_id: str) -> bool:
        """Drop the key (and any default that points at it); was one there?"""
        with self._lock:
            existed = self._keys.pop(provider_id, None) is not None
            dropped = [e for e, pid in self._defaults.items() if pid == provider_id]
            for engine in dropped:
                del self._defaults[engine]
            if existed or dropped:
                self._save()
            return existed

    def configured_providers(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._keys))

    def mask_key(self, provider_id: str) -> str | None:
        """``••••last4`` view for UI/API — never the raw key."""
        with self._lock:
            record = self._keys.get(provider_id)
        if record is None:
            return None
        key = record["api_key"]
        return _MASK + key[-4:] if len(key) > 8 else _MASK

    # --- per-engine defaults (§6.5) ----------------------------------------------

    def set_default(self, engine: str, provider_id: str) -> None:
        """Pick the provider new jobs use for an engine when none is chosen."""
        if engine not in self._engines:
            known = ", ".join(self._engines) or "none"
            raise KeyStoreError(f"unknown engine '{engine}' (known engines: {known})")
        self._require_provider(provider_id)
        with self._lock:
            if provider_id not in self._keys:
                raise KeyStoreError(
                    f"cannot default the {engine} engine to '{provider_id}':"
                    f" no API key is configured for '{provider_id}' — add the key first."
                )
            served = self._provider_engines[provider_id]
            if engine not in served:
                raise KeyStoreError(
                    f"provider '{provider_id}' does not serve the {engine} engine"
                    f" (it serves: {', '.join(served) or 'nothing'})."
                )
            self._defaults[engine] = provider_id
            self._save()

    def default_for(self, engine: str) -> str | None:
        with self._lock:
            return self._defaults.get(engine)

    def defaults(self) -> dict[str, str]:
        with self._lock:
            return dict(self._defaults)

    # --- persistence ---------------------------------------------------------------

    def _require_provider(self, provider_id: str) -> None:
        if provider_id not in self._provider_engines:
            raise KeyStoreError(
                f"unknown provider '{provider_id}'"
                f" (known providers: {', '.join(sorted(self._provider_engines)) or 'none'})"
            )

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            keys = data["keys"]
            defaults = data.get("defaults", {})
            if not isinstance(keys, dict) or not isinstance(defaults, dict):
                raise ValueError("'keys'/'defaults' must be mappings")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise KeyStoreError(
                f"The key store at {self._path} is corrupt ({exc})."
                " Fix or remove the file by hand — keys are not discarded automatically."
            ) from exc
        self._keys = {str(pid): dict(rec) for pid, rec in keys.items()}
        self._defaults = {str(engine): str(pid) for engine, pid in defaults.items()}

    def _save(self) -> None:
        payload = {
            "version": _SCHEMA_VERSION,
            "keys": self._keys,
            "defaults": self._defaults,
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            try:
                # before the swap (review m2-f4): chmodding only the final
                # path leaves a window where keys.json briefly carries the
                # default umask permissions on POSIX.
                os.chmod(tmp, 0o600)
            except OSError:
                pass  # advisory where the OS does not honor it (Windows)
            _replace_with_retry(tmp, self._path)
        except OSError as exc:
            raise KeyStoreError(f"could not write the key store at {self._path}: {exc}") from exc
        try:
            os.chmod(self._path, 0o600)  # advisory on Windows; enforced on POSIX
        except OSError:
            pass

    def __repr__(self) -> str:  # keys must never leak through repr/logging
        return f"<KeyStore path={str(self._path)!r} providers={len(self._keys)}>"
