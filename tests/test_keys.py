"""Seam: the BYOK key store (BUILD_PLAN 2.1, spec §6.5).

``app.core.keys.KeyStore`` is the only place API keys touch disk: one
gitignored ``state/keys.json`` (app.config.app_state_dir), written atomically
and kept as local as the OS allows. The store is provider-agnostic — the set
of known providers and the engines each serves is injected data (the real
mapping comes from ``app.providers.catalog``) — so the storage rules can be
proven without any network code in the loop.

Pinned here:
- add/get/remove round-trip; persisted across KeyStore instances;
- validation: unknown providers, blank keys, keys with control characters;
- masking: UI/API get ``••••last4`` — the raw key never leaves ``get_key``;
- per-engine defaults: settable only for a known engine, a provider that
  serves that engine, and a provider whose key is configured (§6.5);
- removing a key cascades: defaults pointing at that provider are dropped;
- corrupt store files fail loudly (keys are too valuable to discard silently);
- atomic writes leave a valid JSON file at every point (crash can't lose keys).
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from app.core.keys import KEYS_FILENAME, KeyStore, KeyStoreError

PROVIDER_ENGINES = {
    "gemini": ("tts", "image", "video"),
    "elevenlabs": ("tts",),
    "replicate": ("image", "video"),
}
ENGINES = ("tts", "image", "video")


def make_store(tmp_path: Path) -> KeyStore:
    return KeyStore(
        tmp_path / KEYS_FILENAME,
        provider_engines=PROVIDER_ENGINES,
        engines=ENGINES,
    )


# --- add / get / remove -------------------------------------------------------


def test_set_and_get_round_trip(tmp_path: Path):
    store = make_store(tmp_path)
    assert store.get_key("gemini") is None
    assert store.has_key("gemini") is False
    store.set_key("gemini", "AIza" + "a" * 30)
    assert store.get_key("gemini") == "AIza" + "a" * 30
    assert store.has_key("gemini") is True
    assert store.configured_providers() == ("gemini",)


def test_keys_persist_across_instances(tmp_path: Path):
    make_store(tmp_path).set_key("gemini", "k" * 20)
    reloaded = make_store(tmp_path)
    assert reloaded.get_key("gemini") == "k" * 20


def test_store_file_is_the_gitignored_keys_json(tmp_path: Path):
    store = make_store(tmp_path)
    store.set_key("gemini", "k" * 20)
    path = tmp_path / KEYS_FILENAME
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["keys"]["gemini"]["api_key"] == "k" * 20
    assert "defaults" in data


def test_remove_key_returns_whether_it_existed(tmp_path: Path):
    store = make_store(tmp_path)
    store.set_key("gemini", "k" * 20)
    assert store.remove_key("gemini") is True
    assert store.get_key("gemini") is None
    assert store.remove_key("gemini") is False


# --- validation ----------------------------------------------------------------


def test_unknown_provider_is_rejected(tmp_path: Path):
    store = make_store(tmp_path)
    with pytest.raises(KeyStoreError, match="unknown provider 'openai'"):
        store.set_key("openai", "k" * 20)
    assert store.configured_providers() == ()


@pytest.mark.parametrize("bad", ["", "   ", "\n", "\t"])
def test_blank_keys_are_rejected(tmp_path: Path, bad: str):
    store = make_store(tmp_path)
    with pytest.raises(KeyStoreError, match="key is needed"):
        store.set_key("gemini", bad)
    assert store.has_key("gemini") is False


@pytest.mark.parametrize("bad", ["k" * 5 + "\x00" + "k" * 5, "k k", "kk\nkk"])
def test_key_with_control_characters_or_whitespace_is_rejected(tmp_path: Path, bad: str):
    # A real API key never contains whitespace — a pasted line break must not
    # silently truncate into a "valid-looking" broken key.
    store = make_store(tmp_path)
    with pytest.raises(KeyStoreError, match="control characters or whitespace"):
        store.set_key("gemini", bad)
    assert store.has_key("gemini") is False


def test_set_key_trims_surrounding_whitespace(tmp_path: Path):
    store = make_store(tmp_path)
    store.set_key("gemini", "  " + "k" * 30 + "\n")
    assert store.get_key("gemini") == "k" * 30


# --- masking (§6.5: keys never leave the store raw) ------------------------------


def test_mask_shows_only_the_last_four_characters(tmp_path: Path):
    store = make_store(tmp_path)
    store.set_key("gemini", "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    masked = store.mask_key("gemini")
    assert masked is not None
    assert masked.endswith("WXYZ")
    assert masked.startswith("•")
    assert "ABCDEFGHIJKLMNOP" not in masked


def test_mask_of_unconfigured_provider_is_none(tmp_path: Path):
    assert make_store(tmp_path).mask_key("gemini") is None


def test_store_repr_and_str_never_contain_the_key(tmp_path: Path):
    store = make_store(tmp_path)
    secret = "SUPER-SECRET-KEY-VALUE"
    store.set_key("gemini", secret)
    assert secret not in repr(store)
    assert secret not in str(store)


def test_short_keys_are_masked_without_leaking(tmp_path: Path):
    store = make_store(tmp_path)
    store.set_key("gemini", "short")
    masked = store.mask_key("gemini")
    assert masked is not None
    assert "short" not in masked


# --- per-engine defaults (§6.5: pick the default when multiple serve one engine) ---


def test_default_round_trip_and_persistence(tmp_path: Path):
    store = make_store(tmp_path)
    store.set_key("gemini", "k" * 20)
    store.set_default("tts", "gemini")
    assert store.default_for("tts") == "gemini"
    assert make_store(tmp_path).default_for("tts") == "gemini"
    assert store.defaults() == {"tts": "gemini"}


def test_default_for_unset_engine_is_none(tmp_path: Path):
    assert make_store(tmp_path).default_for("tts") is None


def test_default_requires_a_known_engine(tmp_path: Path):
    store = make_store(tmp_path)
    store.set_key("gemini", "k" * 20)
    with pytest.raises(KeyStoreError, match="unknown engine 'subtitles'"):
        store.set_default("subtitles", "gemini")


def test_default_requires_a_provider_that_serves_the_engine(tmp_path: Path):
    store = make_store(tmp_path)
    store.set_key("elevenlabs", "k" * 20)
    with pytest.raises(KeyStoreError, match="elevenlabs.*does not serve the image engine"):
        store.set_default("image", "elevenlabs")


def test_default_requires_a_configured_key(tmp_path: Path):
    store = make_store(tmp_path)
    with pytest.raises(KeyStoreError, match="no API key is configured for 'gemini'"):
        store.set_default("tts", "gemini")


def test_default_requires_a_known_provider(tmp_path: Path):
    store = make_store(tmp_path)
    with pytest.raises(KeyStoreError, match="unknown provider 'sora'"):
        store.set_default("tts", "sora")


def test_removing_a_key_drops_defaults_that_point_at_it(tmp_path: Path):
    store = make_store(tmp_path)
    store.set_key("gemini", "k" * 20)
    store.set_default("tts", "gemini")
    store.set_default("image", "gemini")
    store.remove_key("gemini")
    assert store.default_for("tts") is None
    assert store.default_for("image") is None
    assert store.defaults() == {}


# --- durability ------------------------------------------------------------------


def test_corrupt_store_file_fails_loudly(tmp_path: Path):
    path = tmp_path / KEYS_FILENAME
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(KeyStoreError, match="corrupt"):
        KeyStore(path, provider_engines=PROVIDER_ENGINES, engines=ENGINES)


def test_writes_are_atomic_valid_json_at_every_point(tmp_path: Path):
    # Hammer the store from many threads; a reader must always see valid JSON
    # with every committed key present (os.replace atomicity, §1.2 WAL-spirit).
    store = make_store(tmp_path)
    path = tmp_path / KEYS_FILENAME
    errors: list[Exception] = []

    def writer(n: int) -> None:
        try:
            for i in range(10):
                store.set_key("gemini", f"k{n}-{i}" * 3)
        except Exception as exc:  # noqa: BLE001 - collected below
            errors.append(exc)

    def read_json() -> dict:
        # In-process readers never touch disk (keys are served from memory),
        # so this raw external reader stands in for e.g. an editor or backup
        # tool. On Windows io.open can be transiently denied while os.replace
        # swaps the file — BUILD_PLAN risk #7's lock-retry applies.
        for attempt in range(10):
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.02)
        raise AssertionError("unreachable")

    def reader() -> None:
        try:
            for _ in range(50):
                if path.exists():
                    read_json()
        except Exception as exc:  # noqa: BLE001 - collected below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    threads += [threading.Thread(target=reader) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    final = json.loads(path.read_text(encoding="utf-8"))
    assert set(final["keys"]) == {"gemini"}


@pytest.mark.skipif(__import__("os").name != "posix", reason="chmod is advisory on Windows")
def test_file_is_chmodded_owner_only_where_the_os_allows(tmp_path: Path):
    store = make_store(tmp_path)
    store.set_key("gemini", "k" * 20)
    mode = (tmp_path / KEYS_FILENAME).stat().st_mode & 0o777
    assert mode == 0o600


def test_tmp_file_is_chmodded_owner_only_before_it_swaps_in(tmp_path: Path, monkeypatch):
    """Review m2-f4: the 0600 chmod must hit the .tmp file *before* os.replace.
    Chmodding only the final path leaves a window during every save in which
    keys.json (and the .tmp) carry default umask permissions on POSIX."""
    events: list[str] = []
    real_replace = os.replace

    def fake_chmod(path: object, mode: int) -> None:
        events.append(f"chmod:{Path(str(path)).name}:{mode:o}")

    def fake_replace(src: object, dst: object) -> None:
        events.append(f"replace:{Path(str(src)).name}->{Path(str(dst)).name}")
        real_replace(src, dst)

    monkeypatch.setattr("app.core.keys.os.chmod", fake_chmod)
    monkeypatch.setattr("app.core.keys.os.replace", fake_replace)

    make_store(tmp_path).set_key("gemini", "k" * 20)

    tmp_chmods = [i for i, e in enumerate(events) if e == f"chmod:{KEYS_FILENAME}.tmp:600"]
    swaps = [i for i, e in enumerate(events) if e.startswith("replace:")]
    assert tmp_chmods, f"the .tmp file is never chmodded 0600 — events: {events}"
    assert swaps, f"the store was never swapped in — events: {events}"
    assert tmp_chmods[0] < swaps[0], f"the chmod must precede the swap — events: {events}"
