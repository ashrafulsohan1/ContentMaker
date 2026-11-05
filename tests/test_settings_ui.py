"""Seams: the Settings screen + key-gated provider resolution (BUILD_PLAN 2.1).

The Settings screen (§6.5/§8) drives the ``KeyStore`` over plain forms
(htmx-enhanced): add/remove/test keys per provider, see engine availability
derived from configured keys, pick per-engine defaults. Two guarantees pinned
beyond the JSON API:

- a key pasted into the form is **never rendered back** — only the mask;
- ``resolve_providers`` refuses to build a real (non-dry-run) job on a BYOK
  provider without a configured key, with a message naming the engine, the
  provider and exactly what to do (§6.5: "explains exactly what's missing").
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.keys import KeyStore
from app.engines.base import TTSRequest, TTSResult
from app.engines.mocks import MockTTSProvider, MockVideoProvider
from app.engines.registry import ENGINES, Registry
from app.main import create_app
from app.providers import catalog
from app.web.flow import (
    FlowError,
    FlowOptions,
    MissingEngineError,
    MissingKeyError,
    engine_availability,
    resolve_providers,
)

KEY = "AIza" + "0123456789abcdef" * 3
PROVIDER_ENGINES = {pid: info.engines for pid, info in catalog.PROVIDERS.items()}


def make_store(tmp_path: Path) -> KeyStore:
    return KeyStore(tmp_path / "keys.json", provider_engines=PROVIDER_ENGINES, engines=ENGINES)


def make_client(
    tmp_path: Path,
    *,
    registry: Registry | None = None,
    store: KeyStore | None = None,
    probe=None,
) -> TestClient:
    app = create_app(
        registry=registry,
        key_store=store if store is not None else make_store(tmp_path),
        key_probe=probe,
        projects_root=tmp_path / "projects",
    )
    return TestClient(app)


def start_flow(client: TestClient, project: str = "demo") -> None:
    resp = client.post(
        "/input",
        data={"project": project, "script": "First beat.\n\nSecond beat.\n"},
        follow_redirects=False,
    )
    assert resp.status_code == 303


# --- settings page -------------------------------------------------------------


class TestSettingsPage:
    def test_page_lists_the_catalog_provider_with_add_form(self, tmp_path: Path):
        html = make_client(tmp_path).get("/settings").text
        assert "Google Gemini" in html
        assert "No key configured" in html
        assert 'action="/settings/keys/gemini"' in html
        assert 'type="password"' in html and 'name="api_key"' in html
        assert 'action="/settings/keys/gemini/test"' in html

    def test_navigation_reaches_settings_from_the_home_page(self, tmp_path: Path):
        html = make_client(tmp_path).get("/").text
        assert 'href="/settings"' in html

    def test_add_key_shows_only_the_mask_never_the_key(self, tmp_path: Path):
        client = make_client(tmp_path)
        resp = client.post("/settings/keys/gemini", data={"api_key": KEY}, follow_redirects=True)
        assert resp.status_code == 200
        assert KEY not in resp.text
        assert "••••" + KEY[-4:] in resp.text
        assert "Remove key" in resp.text  # a saved key can be removed again

    def test_blank_key_is_rejected_with_a_message_and_not_saved(self, tmp_path: Path):
        client = make_client(tmp_path)
        resp = client.post("/settings/keys/gemini", data={"api_key": "  "}, follow_redirects=True)
        assert "non-blank" in resp.text
        assert "No key configured" in resp.text

    def test_remove_key_returns_to_unconfigured(self, tmp_path: Path):
        client = make_client(tmp_path)
        client.post("/settings/keys/gemini", data={"api_key": KEY})
        resp = client.post("/settings/keys/gemini/remove", follow_redirects=True)
        assert "No key configured" in resp.text
        assert KEY[-4:] not in resp.text

    def test_test_key_probes_with_the_stored_key_and_shows_the_verdict(self, tmp_path: Path):
        seen: list[tuple[str, str]] = []

        def probe(provider_id: str, api_key: str):
            seen.append((provider_id, api_key))
            return catalog.KeyTestResult(ok=True, message="Key works — Google accepted it.")

        client = make_client(tmp_path, probe=probe)
        client.post("/settings/keys/gemini", data={"api_key": KEY})
        resp = client.post("/settings/keys/gemini/test", follow_redirects=True)
        assert seen == [("gemini", KEY)]
        assert "Key works — Google accepted it." in resp.text

    def test_failed_key_test_shows_exactly_what_is_wrong(self, tmp_path: Path):
        client = make_client(
            tmp_path,
            probe=lambda pid, key: catalog.KeyTestResult(
                ok=False, message="Google rejected this key (401) — it is invalid."
            ),
        )
        client.post("/settings/keys/gemini", data={"api_key": KEY})
        resp = client.post("/settings/keys/gemini/test", follow_redirects=True)
        assert "Google rejected this key (401) — it is invalid." in resp.text

    def test_plain_form_failure_renders_the_red_error_banner(self, tmp_path: Path):
        """No-JS fallback (review m2-f2): a failed action must redirect with
        ?error= so the page renders the red .error styling — an error shown
        in the green .notice banner reads as a success confirmation."""
        client = make_client(
            tmp_path,
            probe=lambda pid, key: catalog.KeyTestResult(
                ok=False, message="Google rejected this key (401) — it is invalid."
            ),
        )
        client.post("/settings/keys/gemini", data={"api_key": KEY})
        resp = client.post("/settings/keys/gemini/test", follow_redirects=True)
        assert (
            '<div class="error">Key test: Google rejected this key (401) — it is invalid.</div>'
            in resp.text
        )
        assert '<div class="notice">Key test: Google rejected this key' not in resp.text

    def test_plain_form_success_still_renders_the_notice_banner(self, tmp_path: Path):
        client = make_client(tmp_path)
        resp = client.post("/settings/keys/gemini", data={"api_key": KEY}, follow_redirects=True)
        assert '<div class="notice">' in resp.text
        assert '<div class="error">' not in resp.text

    def test_test_without_a_saved_key_asks_for_one_first(self, tmp_path: Path):
        resp = make_client(tmp_path).post("/settings/keys/gemini/test", follow_redirects=True)
        assert "add one first" in resp.text

    def test_htmx_test_gets_the_settings_fragment(self, tmp_path: Path):
        client = make_client(
            tmp_path,
            probe=lambda pid, key: catalog.KeyTestResult(ok=True, message="Key works."),
        )
        client.post("/settings/keys/gemini", data={"api_key": KEY})
        resp = client.post("/settings/keys/gemini/test", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert "<html" not in resp.text[:200]  # a fragment, not the page
        assert "Key works." in resp.text

    def test_default_selection_round_trips(self, tmp_path: Path):
        client = make_client(tmp_path)
        client.post("/settings/keys/gemini", data={"api_key": KEY})
        resp = client.post(
            "/settings/defaults/tts", data={"provider": "gemini"}, follow_redirects=True
        )
        assert 'value="gemini" selected' in resp.text or "selected>Google Gemini" in resp.text
        assert "no key configured" not in resp.text.lower()

    def test_default_without_a_key_is_rejected(self, tmp_path: Path):
        resp = make_client(tmp_path).post(
            "/settings/defaults/tts", data={"provider": "gemini"}, follow_redirects=True
        )
        assert "no api key is configured" in resp.text.lower()

    def test_removing_a_key_clears_the_default_too(self, tmp_path: Path):
        client = make_client(tmp_path)
        client.post("/settings/keys/gemini", data={"api_key": KEY})
        client.post("/settings/defaults/tts", data={"provider": "gemini"})
        resp = client.post("/settings/keys/gemini/remove", follow_redirects=True)
        assert "No key configured" in resp.text


# --- key-gated provider resolution (§6.5: say exactly what's missing) -------------


class StubGeminiTTS:
    """A stand-in adapter (the real one is task 2.3) registered for tests."""

    provider_id = "gemini"

    def generate(self, request: TTSRequest) -> TTSResult:
        return TTSResult(file=request.out_path, duration_s=1.0)

    def estimate_cost_usd(self, request: TTSRequest) -> float:
        return 0.02  # $0.02 per 1k chars, per prices.json


def options(**overrides) -> FlowOptions:
    base = {
        "platform_id": "youtube",
        "format_id": "youtube_shorts",
        "audio": True,
        "visual": "none",
        "dry_run": False,
    }
    base.update(overrides)
    return FlowOptions(**base)


class TestKeyGatedResolution:
    def test_dry_run_still_forces_mock_even_with_keys_configured(self, tmp_path: Path):
        registry = Registry()
        registry.register("tts", MockTTSProvider())
        store = make_store(tmp_path)
        store.set_key("gemini", KEY)
        resolved = resolve_providers(options(dry_run=True), registry, store)
        assert resolved == {"tts": "mock"}

    def test_chosen_byok_provider_without_key_raises_missing_key(self, tmp_path: Path):
        registry = Registry()
        registry.register("tts", StubGeminiTTS())
        with pytest.raises(MissingKeyError) as excinfo:
            resolve_providers(options(providers={"tts": "gemini"}), registry, None)
        message = str(excinfo.value)
        assert "tts" in message and "Google Gemini" in message
        assert "Settings" in message

    def test_chosen_byok_provider_with_key_but_no_adapter_says_not_available(self, tmp_path: Path):
        store = make_store(tmp_path)
        store.set_key("gemini", KEY)
        with pytest.raises(FlowError, match="does not include its tts provider"):
            resolve_providers(options(providers={"tts": "gemini"}), Registry(), store)

    def test_chosen_byok_provider_with_key_and_adapter_resolves(self, tmp_path: Path):
        registry = Registry()
        registry.register("tts", StubGeminiTTS())
        store = make_store(tmp_path)
        store.set_key("gemini", KEY)
        resolved = resolve_providers(options(providers={"tts": "gemini"}), registry, store)
        assert resolved == {"tts": "gemini"}

    def test_default_provider_is_used_when_nothing_is_chosen(self, tmp_path: Path):
        registry = Registry()
        registry.register("tts", StubGeminiTTS())
        registry.register("tts", MockTTSProvider())
        store = make_store(tmp_path)
        store.set_key("gemini", KEY)
        store.set_default("tts", "gemini")
        resolved = resolve_providers(options(), registry, store)
        assert resolved == {"tts": "gemini"}

    def test_without_keys_or_choice_the_first_registered_provider_is_used(self, tmp_path: Path):
        registry = Registry()
        registry.register("tts", MockTTSProvider())
        resolved = resolve_providers(options(), registry, None)
        assert resolved == {"tts": "mock"}

    def test_needed_engine_with_no_provider_at_all_stays_missing_engine(self, tmp_path: Path):
        with pytest.raises(MissingEngineError):
            resolve_providers(options(), Registry(), None)

    def test_availability_lists_keyed_providers_mock_and_default_flags(self, tmp_path: Path):
        registry = Registry()
        registry.register("tts", MockTTSProvider())
        registry.register("image", MockTTSProvider())  # any provider id works here
        store = make_store(tmp_path)
        store.set_key("gemini", KEY)
        store.set_default("tts", "gemini")
        rows = engine_availability(registry, store)
        tts_ids = {entry["id"] for entry in rows["tts"]}
        assert {"gemini", "mock"} <= tts_ids
        gemini = next(e for e in rows["tts"] if e["id"] == "gemini")
        assert gemini["configured"] is True and gemini["is_default"] is True
        mock = next(e for e in rows["tts"] if e["id"] == "mock")
        assert mock["configured"] is True  # mocks are always runnable (dry-run)
        image_gemini = next(e for e in rows["image"] if e["id"] == "gemini")
        assert image_gemini["configured"] is True and image_gemini["is_default"] is False

    def test_availability_works_without_a_key_store(self, tmp_path: Path):
        rows = engine_availability(Registry(), None)
        assert set(rows) == set(ENGINES)


# --- the gate through the wizard (§6.5: UI explains exactly what's missing) -------


class TestWizardKeyGating:
    def test_real_run_without_a_key_is_blocked_with_the_exact_message(self, tmp_path: Path):
        client = make_client(tmp_path)  # mock-only registry, empty key store
        start_flow(client)
        resp = client.post(
            "/options",
            data={
                "platform_id": "youtube",
                "format_id": "youtube_shorts",
                "audio": "on",
                "visual": "none",
                "provider_tts": "gemini",
                "dry_run": "",
            },
        )
        assert resp.status_code == 200  # re-rendered with the explanation
        assert "add your key in Settings" in resp.text
        assert "Google Gemini" in resp.text

    def test_real_run_with_key_and_adapter_creates_the_job_on_gemini(self, tmp_path: Path):
        registry = Registry()
        registry.register("tts", StubGeminiTTS())
        registry.register("video", MockVideoProvider())  # visuals resolve to mock
        store = make_store(tmp_path)
        store.set_key("gemini", KEY)
        client = make_client(tmp_path, registry=registry, store=store)
        start_flow(client)
        resp = client.post(
            "/options",
            data={
                "platform_id": "youtube",
                "format_id": "youtube_shorts",
                "audio": "on",
                "visual": "video",
                "provider_tts": "gemini",
                "dry_run": "",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text
        job_id = resp.headers["location"].rsplit("/", 1)[1]
        payload = client.get(f"/api/jobs/{job_id}").json()
        assert payload["providers"]["tts"] == "gemini"
        assert payload["status"] == "awaiting_confirmation"
