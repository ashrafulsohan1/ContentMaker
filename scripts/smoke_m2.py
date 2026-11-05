#!/usr/bin/env python
"""Milestone 2 smoke gate - the BUILD_PLAN section 3 / spec 13.2 definition of done.

Drives the milestone-2 gate scope over the real HTTP app (FastAPI TestClient -
no server, no real network), with the real provider code in the loop:

    1. Settings flow offline - add a key in the Settings screen and validate it
       against a MockTransport-backed fake of Google's zero-spend key probe;
    2. engine availability + per-engine default reflect the keys (and die with
       them); the flow's key gate refuses key-less BYOK choices;
    3. dry-run job produces per-beat audio + a valid timing manifest at $0;
    4. estimate-before-spend: a priced job lands awaiting_confirmation with
       zero provider traffic; starting a job that was never estimated is
       refused (HTTP 409) before any paid call;
    5. hard per-job cap: paid calls stop BEFORE the capped call, completed
       beats are kept (silent defaults in the manifest), and a retry cannot
       sneak past a still-binding cap.

The paid path runs the REAL :class:`~app.providers.gemini.tts.GeminiTTSProvider`
(task 2.3) on the REAL :class:`~app.providers.gemini.client.GeminiClient`
(2.2) whose HTTP moves over an ``httpx.MockTransport`` fake serving real WAV
bytes (stdlib ``wave``) - so the provider's ffprobe duration probe, the
manifest write and the assembly run for real. The key probe (2.1) runs the
real ``keytest.test_key`` on its own MockTransport. Keys are read from the
KeyStore at call time, exactly as in production.

Offline guarantees (gate requirement: ZERO real network, asserted not assumed):
- a process-wide socket guard records - and fails the run on - ANY outbound
  attempt (connect, connect_ex, create_connection, getaddrinfo,
  gethostbyname). The only traffic allowed through, always disclosed in the
  report, is the interpreter's own asyncio ``socket.socketpair`` self-pipe
  (loopback wakeup plumbing every TestClient portal needs on Windows) and
  address-literal resolvers that never touch DNS;
- both fakes record every request: the run asserts the ONLY host ever dialed
  is the Gemini API host, and only through the offline transports;
- an AST scan keeps ``httpx`` confined to ``app/providers/`` - the sole
  sanctioned outbound-HTTP layer (spec 6.5: a key travels only to its own
  provider).

Usage:  .venv/Scripts/python.exe scripts/smoke_m2.py
Exit:   0 - every check passed (gate green); 1 - at least one check failed.

The gate runs against a throwaway projects root + key store (a temp dir), so
it is idempotent and leaves nothing behind.
"""

from __future__ import annotations

import ast
import base64
import io
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import warnings
import wave
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # the script must run from any cwd

GOOD_KEY = "AIza" + "0123456789abcdef" * 3  # realistic shape, never a real key
BAD_KEY = "AIza" + "ffffffffffffffff" * 3  # the probe fake rejects this one
GEMINI_HOST = "generativelanguage.googleapis.com"
FORMAT_VIDEO = {"platform_id": "youtube", "format_id": "youtube_shorts"}  # 1080x1920 MP4
N_BEATS = 3
# Distinct durations so the manifest must track each beat's probed file.
PAID_WAV_S = (0.30, 0.35, 0.40)
DRY_BEATS = (
    "Dry-run beat one checks per-beat audio on the mock engines.",
    "Dry-run beat two confirms zero spend end to end offline.",
    "Dry-run beat three lands the timing manifest for the gate.",
)
PAID_BEATS = (
    "Paid path beat one speaks to the offline Gemini fake.",
    "Paid path beat two proves the estimate gate held.",
    "Paid path beat three lands probed durations in the manifest.",
)
DRY_PROJECT, PAID_PROJECT, CAP_PROJECT = "Smoke M2 Dry", "Smoke M2 Paid", "Smoke M2 Cap"
JOB_TIMEOUT_S = 240.0  # real ffmpeg encodes on slow machines; generous by design
POLL_S = 0.25
GUARD_TARGETS = (
    "socket.socket.connect",
    "socket.socket.connect_ex",
    "socket.create_connection",
    "socket.getaddrinfo",
    "socket.gethostbyname",
)


def script_of(beats: tuple[str, ...]) -> str:
    return "[scene: hook]\n" + "\n\n".join(beats) + "\n"


# --- offline fakes (httpx.MockTransport handlers; every request recorded) --------


def _wav_bytes(seconds: float) -> bytes:
    """A real, playable mono 16-bit 24 kHz PCM WAV - ffprobe-probable for real."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"\x00\x00" * round(seconds * 24000))
    return buf.getvalue()


def _interaction(model: str, wav: bytes) -> dict[str, Any]:
    """The spike-shaped Interactions response carrying the WAV inline (base64)."""
    return {
        "id": "smoke_interaction",
        "object": "interaction",
        "status": "completed",
        "model": model,
        "steps": [
            {
                "type": "model_output",
                "content": [
                    {
                        "type": "audio",
                        "mime_type": "audio/wav",
                        "data": base64.b64encode(wav).decode("ascii"),
                    }
                ],
            }
        ],
    }


class FakeVoices:
    """Google's zero-spend key-probe endpoint (GET /v1beta/voices), offline.

    The good key gets a 200; any other key gets Google's 401 envelope, so the
    real ``keytest.test_key`` mapping is exercised on both paths.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("x-goog-api-key") == GOOD_KEY:
            return httpx.Response(200, json={"voices": [{"name": "Kore"}]})
        return httpx.Response(
            401,
            json={
                "error": {
                    "code": "authentication",
                    "message": "API key not valid. Please pass a valid API key.",
                }
            },
        )


class FakeGeminiTTS:
    """The Interactions TTS endpoint (POST /v1beta/interactions), offline.

    Serves real WAV bytes (rotating through :data:`PAID_WAV_S`) so the
    provider's ffprobe probe and the timing manifest are exercised for real;
    records every request for the zero-network / per-beat / cap assertions.
    """

    def __init__(self, model: str) -> None:
        self.requests: list[httpx.Request] = []
        self.model = model
        self._calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("x-goog-api-key") != GOOD_KEY:
            return httpx.Response(
                401,
                json={"error": {"code": "authentication", "message": "API key not valid."}},
            )
        self._calls += 1
        seconds = PAID_WAV_S[(self._calls - 1) % len(PAID_WAV_S)]
        return httpx.Response(200, json=_interaction(self.model, _wav_bytes(seconds)))


# --- zero-network guard -----------------------------------------------------------


class NetworkAttempt(AssertionError):
    """Any code tried to open a real network connection during the smoke run."""


def _target_host(args: tuple[Any, ...]) -> str | None:
    if not args:
        return None
    address = args[0]
    if isinstance(address, (tuple, list)) and address:
        first = address[0]
        return first if isinstance(first, str) else None
    if isinstance(address, str):
        return address
    return None


def _is_loopback(host: str | None) -> bool:
    return host is not None and (host in ("localhost", "::1") or host.startswith("127."))


def _is_address_literal(host: str | None) -> bool:
    """A numeric IP never hits DNS - resolving it is a purely local operation."""
    if host is None or _is_loopback(host):
        return True if host is not None else False
    parts = host.split(".")
    return len(parts) == 4 and all(part.isdigit() and 0 <= int(part) <= 255 for part in parts)


class SocketGuard:
    """Fails the run on any outbound network attempt, recording the caller.

    Allowed through (and always disclosed in the report): the interpreter's own
    asyncio ``socket.socketpair`` self-pipe - on Windows that helper binds and
    CONNECTS two sockets over 127.0.0.1 for the event loop's wakeup plumbing,
    which every TestClient portal needs - plus address-literal resolvers that
    never touch DNS. An app-level dial to ANY host, loopback included, raises.
    """

    def __init__(self) -> None:
        self.violations: list[str] = []
        self.loopback: list[str] = []
        self._originals: dict[str, Callable[..., Any]] = {}

    def install(self) -> None:
        guard = self
        originals: dict[str, Callable[..., Any]] = {
            "connect": socket.socket.connect,
            "connect_ex": socket.socket.connect_ex,
            "create_connection": socket.create_connection,
            "getaddrinfo": socket.getaddrinfo,
            "gethostbyname": socket.gethostbyname,
        }
        self._originals = originals

        def denied(target: str, method_patch: bool) -> Callable[..., Any]:
            def blocked(*args: Any, **kwargs: Any) -> Any:
                stack = "".join(traceback.format_stack(limit=14))
                rest = args[1:] if method_patch else args
                host = _target_host(rest)
                self_pipe = "socketpair" in stack and _is_loopback(host)
                resolver = target.startswith("socket.get") and _is_address_literal(host)
                if self_pipe or resolver:
                    guard.loopback.append(f"{target}({host})")
                    return originals[target.rsplit(".", 1)[-1]](*args, **kwargs)
                guard.violations.append(f"{target}({host})\n{stack}")
                raise NetworkAttempt(
                    f"outbound network attempted via {target} during the smoke run"
                )

            return blocked

        socket.socket.connect = denied("socket.socket.connect", True)  # type: ignore[method-assign]
        socket.socket.connect_ex = denied("socket.socket.connect_ex", True)  # type: ignore[method-assign]
        socket.create_connection = denied("socket.create_connection", False)
        socket.getaddrinfo = denied("socket.getaddrinfo", False)  # type: ignore[assignment]
        socket.gethostbyname = denied("socket.gethostbyname", False)

    def restore(self) -> None:
        if not self._originals:
            return
        socket.socket.connect = self._originals["connect"]  # type: ignore[method-assign]
        socket.socket.connect_ex = self._originals["connect_ex"]  # type: ignore[method-assign]
        socket.create_connection = self._originals["create_connection"]
        socket.getaddrinfo = self._originals["getaddrinfo"]  # type: ignore[assignment]
        socket.gethostbyname = self._originals["gethostbyname"]
        self._originals = {}

    @property
    def installed(self) -> bool:
        return bool(self._originals)

    def summary(self) -> str:
        note = (
            f"; {len(self.loopback)} interpreter self-pipe/local-resolver call(s)"
            if self.loopback
            else ""
        )
        return f"blocked outbound: {', '.join(GUARD_TARGETS)}{note}"


# --- check harness ----------------------------------------------------------------


@dataclass
class Result:
    name: str
    status: str  # PASS | FAIL | SKIP
    detail: str
    seconds: float = 0.0
    tail: str = ""


@dataclass
class CheckSpec:
    name: str
    title: str
    fn: Callable[[dict[str, Any]], str]
    requires: tuple[str, ...] = ()


def _fail(message: str) -> None:
    raise AssertionError(message)


def _tail(exc: BaseException, lines: int = 5) -> str:
    chunks = traceback.format_exception(type(exc), exc, exc.__traceback__)
    rows = [row for chunk in chunks for row in chunk.splitlines() if row.strip()]
    return "\n".join("        " + row for row in rows[-lines:])


# --- checks -----------------------------------------------------------------------


def check_environment(ctx: dict[str, Any]) -> str:
    """Python >= 3.11, ffmpeg + ffprobe present, the app imports, prices ready."""
    py = ".".join(map(str, sys.version_info[:3]))
    # Deliberate runtime guard (gate may run under any interpreter): noqa - the
    # toolchain targets 3.11+, but this check must survive when it doesn't.
    if sys.version_info < (3, 11):  # noqa: UP036
        _fail(f"python {py} is below the app's minimum 3.11")
    versions: dict[str, str] = {}
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            _fail(f"{tool} not on PATH - assembly cannot run (see scripts/check_env.py)")
        proc = subprocess.run([tool, "-version"], capture_output=True, text=True, timeout=30)
        rows = (proc.stdout or proc.stderr).splitlines() or [""]
        versions[tool] = rows[0].strip()
    try:
        import importlib

        app_main = importlib.import_module("app.main")
        ctx["create_app"] = app_main.create_app
        registry_mod = importlib.import_module("app.engines.registry")
        ctx["ENGINES"] = registry_mod.ENGINES
        ctx["Registry"] = registry_mod.Registry
        mocks = importlib.import_module("app.engines.mocks")
        ctx["MockTTSProvider"] = mocks.MockTTSProvider
        ctx["MockImageProvider"] = mocks.MockImageProvider
        ctx["MockVideoProvider"] = mocks.MockVideoProvider
        jobs_mod = importlib.import_module("app.core.jobs")
        ctx["TERMINAL"] = jobs_mod.TERMINAL_JOB_STATUSES
        ctx["AssetSpec"] = jobs_mod.AssetSpec
        manifest_mod = importlib.import_module("app.scripting.manifest")
        ctx["read_manifest"] = manifest_mod.read_manifest
        ctx["DEFAULT_FPS"] = manifest_mod.DEFAULT_FPS
        ctx["FFmpeg"] = importlib.import_module("app.assembly.ffmpeg").FFmpeg
        ctx["MANIFEST_SUFFIX"] = importlib.import_module("app.assembly.pipeline").MANIFEST_SUFFIX
        ctx["slugify"] = importlib.import_module("app.core.projects").slugify
        ctx["SUBDIRS"] = importlib.import_module("app.config").PROJECT_SUBDIRS
        keys_mod = importlib.import_module("app.core.keys")
        ctx["KeyStore"] = keys_mod.KeyStore
        ctx["KEYS_FILENAME"] = keys_mod.KEYS_FILENAME
        catalog = importlib.import_module("app.providers.catalog")
        ctx["PROVIDERS"] = catalog.PROVIDERS
        ctx["keytest"] = importlib.import_module("app.providers.gemini.keytest")
        ctx["GeminiClient"] = importlib.import_module("app.providers.gemini.client").GeminiClient
        ctx["GeminiTTSProvider"] = importlib.import_module(
            "app.providers.gemini.tts"
        ).GeminiTTSProvider
        flow_mod = importlib.import_module("app.web.flow")
        ctx["FlowOptions"] = flow_mod.FlowOptions
        ctx["resolve_providers"] = flow_mod.resolve_providers
        ctx["MissingKeyError"] = flow_mod.MissingKeyError
        price = importlib.import_module("app.core.cost").load_prices().price_for("gemini", "tts")
    except Exception as exc:
        _fail(f"the app package does not import: {exc}\n{_tail(exc)}")
    if price.price <= 0 or not price.model:
        _fail(
            "app/data/prices.json must carry the spike-verified gemini tts price"
            f" and model id (got price={price.price!r}, model={price.model!r})"
        )
    ctx["PRICE"], ctx["MODEL"] = price.price, price.model
    return f"python {py}; gemini tts ${price.price}/1k chars on {price.model}"
    # (ffmpeg/ffprobe versions verified above; detail names the price data the
    # paid path re-derives every amount from)


def check_no_httpx(ctx: dict[str, Any]) -> str:
    """AST scan: no app/ module outside app/providers/ may import httpx.

    The provider adapters (app/providers/) legitimately dial their own cloud
    API - the ONLY sanctioned outbound HTTP in the app, each key travelling
    solely to the provider it belongs to (spec 6.5). Everything else - UI
    flow, jobs, cost, assembly, settings/storage - must stay network-free;
    the dry-run flow below never dials, and the paid flow dials only the
    offline MockTransport.
    """
    app_dir = ROOT / "app"
    providers_dir = app_dir / "providers"
    scanned = 0
    exempted = 0
    offenders: list[str] = []
    for path in sorted(app_dir.rglob("*.py")):
        if providers_dir in path.parents:
            exempted += 1
            continue
        scanned += 1
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name == "httpx" or name.startswith("httpx.") for name in names):
                offenders.append(str(path.relative_to(app_dir)))
    if offenders:
        _fail(f"httpx imported outside app/providers/: {offenders}")
    return (
        f"{scanned} app/ modules scanned, 0 httpx imports"
        f" ({exempted} provider modules exempt - tasks 2.2/2.3 scope)"
    )


def check_boot(ctx: dict[str, Any]) -> str:
    """App factory with the smoke registry: mocks + the REAL gemini TTS
    adapter on an offline transport, a temp KeyStore, the fake key probe."""
    store = ctx["KeyStore"](
        ctx["state_dir"] / ctx["KEYS_FILENAME"],
        provider_engines={pid: info.engines for pid, info in ctx["PROVIDERS"].items()},
        engines=ctx["ENGINES"],
    )
    if store.configured_providers():
        _fail("a fresh key store must start empty")
    probe_transport = FakeVoices()
    tts_transport = FakeGeminiTTS(ctx["MODEL"])

    def fake_probe(provider_id: str, api_key: str):
        if provider_id != "gemini":
            _fail(f"the key probe must dispatch per provider, got {provider_id!r}")
        return ctx["keytest"].test_key(
            api_key, transport=httpx.MockTransport(probe_transport.handler)
        )

    registry = ctx["Registry"]()
    registry.register("tts", ctx["MockTTSProvider"]())
    registry.register("image", ctx["MockImageProvider"]())
    registry.register("video", ctx["MockVideoProvider"]())
    # The REAL 2.3 provider on the REAL 2.2 client; only the HTTP moves onto
    # the recording fake. The key is read from the store at call time, and
    # durations are probed with the real ffprobe (the fake serves real WAVs).
    registry.register(
        "tts",
        ctx["GeminiTTSProvider"](
            ctx["GeminiClient"](store, transport=httpx.MockTransport(tts_transport.handler))
        ),
    )
    app = ctx["create_app"](
        registry=registry,
        key_store=store,
        key_probe=fake_probe,
        projects_root=ctx["projects_root"],
    )
    if app.state.key_probe is not fake_probe:
        _fail("app.state.key_probe is not the smoke's fake probe")
    from fastapi.testclient import TestClient

    client = TestClient(app)
    client.__enter__()  # lifespan: maintenance task + job worker, stopped at cleanup
    available = {
        engine: [p.provider_id for p in registry.providers_for(engine)] for engine in ctx["ENGINES"]
    }
    if available != {"tts": ["mock", "gemini"], "image": ["mock"], "video": ["mock"]}:
        _fail(f"registry shape wrong: {available}")
    ctx.update(
        client=client,
        close_client=client.__exit__,
        registry=registry,
        key_store=store,
        probe_transport=probe_transport,
        tts_transport=tts_transport,
        app_jobs=app.state.jobs,
    )
    return (
        "TestClient up (lifespan on); tts -> [mock, gemini on offline transport],"
        " image/video -> [mock]; key store empty"
    )


def check_guard(ctx: dict[str, Any]) -> str:
    """The socket guard is live and has recorded nothing but interpreter plumbing."""
    guard: SocketGuard = ctx["guard"]
    if not guard.installed:
        _fail("socket guard is not installed")
    if guard.violations:
        _fail(f"outbound network attempted before the flow even started:\n{guard.violations[0]}")
    return guard.summary() + " - guard up for every remaining check"


def check_settings_add(ctx: dict[str, Any]) -> str:
    """Gate 1a - Settings: add a key; masked display; the raw key never returns."""
    client = ctx["client"]
    # Before-state: the provider is listed, no key, no default.
    before = client.get("/api/settings").json()
    gemini = next(p for p in before["providers"] if p["id"] == "gemini")
    if gemini["key_configured"]:
        _fail("gemini reports a key before one was added")
    if before["engines"]["tts"]["default"] is not None:
        _fail("a default exists before any key was added")
    page = client.get("/settings")
    if page.status_code != 200 or "Google Gemini" not in page.text:
        _fail(f"GET /settings -> {page.status_code} (provider catalog missing?)")
    # Add the key through the Settings form (the user path).
    resp = client.post("/settings/keys/gemini", data={"api_key": GOOD_KEY}, follow_redirects=False)
    if resp.status_code != 303 or not resp.headers["location"].startswith("/settings?notice="):
        _fail(f"POST /settings/keys/gemini -> {resp.status_code} {resp.headers.get('location')!r}")
    rendered = client.get(resp.headers["location"])
    if "key saved" not in rendered.text:
        _fail("the settings page does not confirm the saved key")
    if GOOD_KEY in rendered.text:
        _fail("the raw key leaked onto the settings page")
    # API surface: masked only, never the raw key.
    payload = client.get("/api/settings").json()
    entry = next(p for p in payload["providers"] if p["id"] == "gemini")
    if not entry["key_configured"]:
        _fail("the added key is not reflected as configured")
    if entry["key_masked"] != "••••" + GOOD_KEY[-4:]:
        _fail(f"masked key wrong: {entry['key_masked']!r}")
    if GOOD_KEY in json.dumps(payload):
        _fail("the raw key leaked into the settings API payload")
    # Persisted to the local, gitignored state dir of the throwaway workspace.
    if not (ctx["state_dir"] / ctx["KEYS_FILENAME"]).is_file():
        _fail("the key store file was not written to the local state dir")
    # Blank keys are rejected at the boundary.
    resp = client.put("/api/settings/keys/gemini", json={"api_key": "   "})
    if resp.status_code != 400:
        _fail(f"a blank key must be refused (got {resp.status_code})")
    return "key added via Settings form; masked ••••last4 shown; raw key never returned"


def check_settings_test_key(ctx: dict[str, Any]) -> str:
    """Gate 1b - Settings: validate the key against the MockTransport fake."""
    client = ctx["client"]
    probe_requests = ctx["probe_transport"].requests
    n0 = len(probe_requests)
    resp = client.post("/settings/keys/gemini/test", follow_redirects=False)
    if resp.status_code != 303 or "notice=" not in resp.headers["location"]:
        _fail(f"testing the good key -> {resp.status_code} {resp.headers.get('location')!r}")
    page = client.get(resp.headers["location"])
    if "Key test:" not in page.text or "works" not in page.text:
        _fail("the good key's test result is not shown as passing")
    if len(probe_requests) != n0 + 1:
        _fail(f"expected exactly 1 probe request, got {len(probe_requests) - n0}")
    req = probe_requests[-1]
    if req.method != "GET" or req.url.host != GEMINI_HOST or req.url.path != "/v1beta/voices":
        _fail(f"probe hit {req.method} {req.url.host}{req.url.path}, the documented probe")
    if req.headers.get("x-goog-api-key") != GOOD_KEY:
        _fail("the probe did not send the key in the x-goog-api-key header")
    if "key" in req.url.params:
        _fail("the key must travel in the header only, never the query string")
    # An invalid key maps to a message saying exactly what's wrong - key-free.
    client.post("/settings/keys/gemini", data={"api_key": BAD_KEY}, follow_redirects=False)
    resp = client.post("/settings/keys/gemini/test", follow_redirects=False)
    if resp.status_code != 303:
        _fail(f"testing the bad key -> {resp.status_code} {resp.headers.get('location')!r}")
    # The plain-form redirect carries the message back on the page (the UI's
    # notice param) - the page content is the user-visible outcome asserted below.
    page = client.get(resp.headers["location"])
    if "Key test:" not in page.text or "401" not in page.text:
        _fail("the invalid-key result must say what's wrong (401)")
    if BAD_KEY in page.text:
        _fail("the key-test error message must never echo the key")
    if len(probe_requests) != n0 + 2:
        _fail("the bad-key test must be exactly one more offline probe")
    # Restore the good key (the paid checks read it from the store at call time).
    client.post("/settings/keys/gemini", data={"api_key": GOOD_KEY}, follow_redirects=False)
    resp = client.post("/settings/keys/gemini/test", follow_redirects=False)
    if "notice=" not in resp.headers["location"]:
        _fail("the good key must validate again after re-adding")
    return (
        "good key validated (GET /v1beta/voices, header-only auth); invalid key"
        " reports 401 without echoing the key - all offline via MockTransport"
    )


def check_availability_defaults(ctx: dict[str, Any]) -> str:
    """Gate 2 - engine availability + per-engine default live and die with keys."""
    client = ctx["client"]
    payload = client.get("/api/settings").json()
    if not next(p for p in payload["engines"]["tts"]["providers"] if p["id"] == "gemini")[
        "key_configured"
    ]:
        _fail("the tts engine row does not reflect the configured gemini key")
    resp = client.post(
        "/settings/defaults/tts", data={"provider": "gemini"}, follow_redirects=False
    )
    if resp.status_code != 303:
        _fail(f"POST /settings/defaults/tts -> {resp.status_code}")
    payload = client.get("/api/settings").json()
    if payload["engines"]["tts"]["default"] != "gemini":
        _fail("the per-engine default was not recorded")
    # The options page needs a wizard flow (GET /options redirects to the start
    # without one) - make it the way a user arrives: through the input step.
    resp = client.post(
        "/input",
        data={"project": "Smoke M2 Settings", "script": "[scene: hook]\nKey availability beat.\n"},
        follow_redirects=False,
    )
    if resp.status_code != 303:
        _fail(f"POST /input (settings flow) -> {resp.status_code}")
    options = client.get("/options")
    if options.status_code != 200:
        _fail(f"GET /options -> {options.status_code}")
    if "· default" not in options.text:
        _fail("the options availability row does not mark the gemini default")
    if "not available, add the key in Settings" in options.text:
        _fail("a keyed provider still shows as not available on the options page")
    # The flow's provider resolution: dry-run forces mocks; the key gate
    # refuses a BYOK choice without a key and resolves with one.
    resolve = ctx["resolve_providers"]
    flow_opts = ctx["FlowOptions"](
        platform_id="youtube",
        format_id="youtube_shorts",
        audio=True,
        visual="video",
        dry_run=False,
    )
    dry = replace(flow_opts, dry_run=True)
    if resolve(dry, ctx["registry"], ctx["key_store"]) != {"tts": "mock", "video": "mock"}:
        _fail("dry-run must force the mock providers")
    if resolve(flow_opts, ctx["registry"], ctx["key_store"]) != {
        "tts": "gemini",
        "video": "mock",
    }:
        _fail("with a key + default, tts must resolve to gemini (video stays mock)")
    empty = ctx["KeyStore"](
        ctx["workspace"] / "empty-keys.json",
        provider_engines={pid: info.engines for pid, info in ctx["PROVIDERS"].items()},
        engines=ctx["ENGINES"],
    )
    # No key and no default: resolution falls back to the first registered
    # provider (the mock); an EXPLICIT BYOK choice is refused with a message
    # that says exactly what to do (§6.5 key gate).
    if resolve(flow_opts, ctx["registry"], empty) != {"tts": "mock", "video": "mock"}:
        _fail("without key or default, resolution must fall back to the mocks")
    explicit = replace(flow_opts, providers={"tts": "gemini"})
    try:
        resolve(explicit, ctx["registry"], empty)
    except ctx["MissingKeyError"] as exc:
        message = str(exc)
        if "gemini" not in message.lower() or "Settings" not in message:
            _fail(f"the key-less refusal must say what to do: {message}")
    else:
        _fail("a BYOK choice without a key must be refused (MissingKeyError)")
    # Removing the key drops the default (an invariant readers can trust).
    client.post("/settings/keys/gemini/remove", follow_redirects=False)
    payload = client.get("/api/settings").json()
    if payload["engines"]["tts"]["default"] is not None:
        _fail("removing the key must drop the default that pointed at it")
    if next(p for p in payload["providers"] if p["id"] == "gemini")["key_configured"]:
        _fail("the removed key still shows as configured")
    # Restore key + default for the paid checks below.
    client.post("/settings/keys/gemini", data={"api_key": GOOD_KEY}, follow_redirects=False)
    client.post("/settings/defaults/tts", data={"provider": "gemini"}, follow_redirects=False)
    payload = client.get("/api/settings").json()
    if payload["engines"]["tts"]["default"] != "gemini":
        _fail("the restored default is not back")
    return "availability + default follow the key; key-less BYOK refused; removal drops default"


def check_dry_run_job(ctx: dict[str, Any]) -> str:
    """Gate 3 - dry-run job: per-beat audio + a valid timing manifest, $0."""
    client = ctx["client"]
    resp = client.post(
        "/input",
        data={"project": DRY_PROJECT, "script": script_of(DRY_BEATS)},
        follow_redirects=False,
    )
    if resp.status_code != 303:
        _fail(f"POST /input (dry project) -> {resp.status_code}")
    slug = ctx["slugify"](DRY_PROJECT)
    project_dir = ctx["projects_root"] / slug
    missing = [d for d in ctx["SUBDIRS"] if not (project_dir / d).is_dir()]
    if missing:
        _fail(f"projects/{slug}/ is missing subfolder(s) {missing}")
    resp = client.post(
        "/options",
        data={**FORMAT_VIDEO, "audio": "on", "visual": "video", "dry_run": "on"},
        follow_redirects=False,
    )
    if resp.status_code != 303 or not resp.headers["location"].startswith("/confirm/"):
        _fail(f"POST /options (dry) -> {resp.status_code} {resp.headers.get('location')!r}")
    job_id = resp.headers["location"].rsplit("/", 1)[1]
    confirm = client.get(f"/confirm/{job_id}")
    if confirm.status_code != 200 or "$0.0000" not in confirm.text:
        _fail("the dry-run confirmation page does not show the $0.0000 estimate")
    payload = client.get(f"/api/jobs/{job_id}").json()
    if payload["status"] != "awaiting_confirmation":
        _fail(f"dry job status is {payload['status']!r} before confirmation")
    if payload["estimate_usd"] != 0.0 or payload["spent_usd"] != 0.0:
        _fail("dry-run money is not zero")
    if payload["providers"] != {"tts": "mock", "video": "mock"}:
        _fail(f"dry-run providers are not the mocks: {payload['providers']}")
    if client.post(f"/jobs/{job_id}/confirm", follow_redirects=False).status_code != 303:
        _fail("dry job confirm failed")
    payload = _wait_terminal(ctx, job_id)
    if payload["status"] != "completed":
        errors = [a["error"] for a in payload["assets"] if a["error"]]
        _fail(f"dry job ended {payload['status']!r}; asset errors: {errors}")
    if payload["spent_usd"] != 0.0:
        _fail(f"dry-run job spent {payload['spent_usd']}")
    tts = [a for a in payload["assets"] if a["kind"] == "tts"]
    if len(tts) != N_BEATS or {a["status"] for a in tts} != {"done"}:
        _fail("the dry job did not produce one done tts asset per beat")
    for n in range(1, N_BEATS + 1):
        audio = project_dir / "audio" / f"beat_{n:02d}.wav"
        if not audio.is_file() or audio.stat().st_size == 0:
            _fail(f"per-beat audio missing: {audio}")
    manifest = _manifest_of(ctx, project_dir, job_id)
    if len(manifest.beats) != N_BEATS:
        _fail(f"manifest has {len(manifest.beats)} beats, expected {N_BEATS}")
    if manifest.fps_target != ctx["DEFAULT_FPS"]:
        _fail(f"manifest fps {manifest.fps_target} != DEFAULT_FPS")
    ffmpeg = ctx["FFmpeg"]()
    for beat in manifest.beats:
        if not beat.audio_file:
            _fail(f"beat {beat.index} has no audio_file")
        audio = project_dir / beat.audio_file
        if not audio.is_file():
            _fail(f"beat {beat.index}: audio file missing: {audio}")
        if beat.duration_s <= 0:
            _fail(f"beat {beat.index}: non-positive duration")
        probed = round(ffmpeg.probe_duration(audio), 3)
        if abs(probed - beat.duration_s) > 0.005:
            _fail(f"beat {beat.index}: manifest {beat.duration_s}s vs probed {probed}s")
    if abs(sum(b.duration_s for b in manifest.beats) - manifest.total_duration_s) > 0.001:
        _fail("manifest total duration does not equal the beat sum")
    entries = _spend_entries(ctx, slug, job_id)
    if len(entries) != 2 * N_BEATS or any(
        e["amount_usd"] != 0.0 or e["provider_id"] != "mock" for e in entries
    ):
        _fail("the dry job's spend log is not 6 zero-cost mock entries")
    ctx["dry_slug"], ctx["dry_job"] = slug, job_id
    return (
        f"completed; {N_BEATS} per-beat audio files; manifest valid (probed"
        f" durations match ffprobe); $0.0 spent"
    )


def check_estimate_gate(ctx: dict[str, Any]) -> str:
    """Gate 4a - a priced job lands awaiting_confirmation with ZERO paid traffic;
    starting a job that was never estimated is refused before any spend."""
    client = ctx["client"]
    tts_requests = ctx["tts_transport"].requests
    n0 = len(tts_requests)
    price = ctx["PRICE"]
    resp = client.post(
        "/input",
        data={"project": PAID_PROJECT, "script": script_of(PAID_BEATS)},
        follow_redirects=False,
    )
    if resp.status_code != 303:
        _fail(f"POST /input (paid project) -> {resp.status_code}")
    slug = ctx["slugify"](PAID_PROJECT)
    resp = client.post(
        "/options",
        data={**FORMAT_VIDEO, "audio": "on", "visual": "video"},
        follow_redirects=False,
    )
    if resp.status_code != 303 or not resp.headers["location"].startswith("/confirm/"):
        _fail(f"POST /options (paid) -> {resp.status_code} {resp.headers.get('location')!r}")
    job_id = resp.headers["location"].rsplit("/", 1)[1]
    payload = client.get(f"/api/jobs/{job_id}").json()
    if payload["status"] != "awaiting_confirmation":
        _fail(f"priced job status is {payload['status']!r}, expected awaiting_confirmation")
    if abs(payload["estimate_usd"] - N_BEATS * price) > 1e-9:
        _fail(f"estimate {payload['estimate_usd']} != {N_BEATS} x price table ({N_BEATS * price})")
    if payload["spent_usd"] != 0.0:
        _fail("a job that has not run must show spent 0.0")
    for asset in payload["assets"]:
        expected = price if asset["kind"] == "tts" else 0.0
        if abs(asset["estimated_cost_usd"] - expected) > 1e-9:
            _fail(f"{asset['kind']} asset estimate {asset['estimated_cost_usd']} != {expected}")
    if len(tts_requests) != n0:
        _fail("estimation must be local math against prices.json - zero HTTP")
    confirm = client.get(f"/confirm/{job_id}")
    if confirm.status_code != 200 or f"${N_BEATS * price:.4f}" not in confirm.text:
        _fail("the confirmation page does not show the priced estimate")
    if f"/jobs/{job_id}/confirm" not in confirm.text:
        _fail("the confirmation page does not offer the explicit confirm action")
    # The §6.6 gate, over real HTTP: a job that was never estimated cannot start.
    draft = ctx["app_jobs"].create_job(
        slug,
        [
            ctx["AssetSpec"](
                kind="tts",
                beat_index=1,
                out_path=ctx["projects_root"] / slug / "audio" / "beat_99.wav",
                params={"text": PAID_BEATS[0], "voice": "default"},
            )
        ],
        providers={"tts": "gemini"},
    )
    resp = client.post(f"/jobs/{draft.id}/confirm", follow_redirects=False)
    if resp.status_code != 409:
        _fail(f"starting an un-estimated job must be refused (got {resp.status_code})")
    if "awaiting_confirmation" not in resp.text or "draft" not in resp.text:
        _fail("the refusal must say the job must be awaiting_confirmation first")
    if len(tts_requests) != n0:
        _fail("the refused start must not touch the provider")
    ctx.update(paid_slug=slug, paid_job=job_id, gate_requests=n0)
    return (
        f"job {job_id}: awaiting_confirmation, estimate ${N_BEATS * price:.4f} from"
        f" prices.json, 0 provider calls; un-estimated start -> HTTP 409"
    )


def check_paid_job(ctx: dict[str, Any]) -> str:
    """Gate 4b - confirmation admits exactly one paid call per beat; the manifest
    carries real probed durations; the spend log stays at parity."""
    client = ctx["client"]
    job_id = ctx["paid_job"]
    slug = ctx["paid_slug"]
    project_dir = ctx["projects_root"] / slug
    price = ctx["PRICE"]
    resp = client.post(f"/jobs/{job_id}/confirm", follow_redirects=False)
    if resp.status_code != 303 or resp.headers["location"] != f"/jobs/{job_id}":
        _fail(f"POST confirm (paid) -> {resp.status_code} {resp.headers.get('location')!r}")
    payload = _wait_terminal(ctx, job_id)
    if payload["status"] != "completed":
        errors = [a["error"] for a in payload["assets"] if a["error"]]
        _fail(f"paid job ended {payload['status']!r}; asset errors: {errors}")
    paid = ctx["tts_transport"].requests[ctx["gate_requests"] :]
    if len(paid) != N_BEATS:
        _fail(f"expected exactly {N_BEATS} paid calls, got {len(paid)}")
    for req in paid:
        if req.method != "POST" or not req.url.path.endswith("/v1beta/interactions"):
            _fail(f"paid call is {req.method} {req.url.path}, expected POST interactions")
        if req.url.host != GEMINI_HOST:
            _fail(f"paid call hit {req.url.host}")
        if req.headers.get("x-goog-api-key") != GOOD_KEY:
            _fail("the paid call must carry the stored key in the auth header")
        body = json.loads(req.content.decode("utf-8"))
        if body.get("model") != ctx["MODEL"]:
            _fail(f"paid call model {body.get('model')!r} != prices.json {ctx['MODEL']!r}")
        if "key" in req.url.params:
            _fail("the key must never travel in the query string")
    texts = [
        json.loads(req.content.decode("utf-8"))["input"][0]["content"][0]["text"] for req in paid
    ]
    if texts != list(PAID_BEATS):
        _fail("beat text must travel verbatim, one call per beat")
    if abs(payload["spent_usd"] - N_BEATS * price) > 1e-9:
        _fail(f"spent {payload['spent_usd']} != {N_BEATS * price}")
    tts = sorted((a for a in payload["assets"] if a["kind"] == "tts"), key=lambda a: a["id"])
    if any(abs(a["spent_usd"] - price) > 1e-9 for a in tts):
        _fail("each tts asset must carry its own spend")
    manifest = _manifest_of(ctx, project_dir, job_id)
    if len(manifest.beats) != N_BEATS:
        _fail(f"paid manifest has {len(manifest.beats)} beats")
    ffmpeg = ctx["FFmpeg"]()
    for position, beat in enumerate(manifest.beats):
        if not beat.audio_file:
            _fail(f"manifest beat {position + 1} has no audio_file")
        audio = project_dir / beat.audio_file
        if not audio.is_file():
            _fail(f"manifest beat {position + 1}: audio missing: {audio}")
        probed = round(ffmpeg.probe_duration(audio), 3)
        if abs(probed - beat.duration_s) > 0.005:
            _fail(f"beat {position + 1}: manifest {beat.duration_s}s vs probed {probed}s")
        if abs(beat.duration_s - PAID_WAV_S[position]) > 0.02:
            _fail(
                f"beat {position + 1}: manifest {beat.duration_s}s is not the served"
                f" {PAID_WAV_S[position]}s - durations must be probed, not guessed"
            )
        if beat.text != PAID_BEATS[position]:
            _fail(f"beat {position + 1} text drifted from the script")
    entries = _spend_entries(ctx, slug, job_id)
    gemini = [e for e in entries if e["provider_id"] == "gemini"]
    mock = [e for e in entries if e["provider_id"] == "mock"]
    if len(gemini) != N_BEATS or any(abs(e["amount_usd"] - price) > 1e-9 for e in gemini):
        _fail("the spend log must carry one gemini entry per beat at the table price")
    if len(mock) != N_BEATS or any(e["amount_usd"] != 0.0 for e in mock):
        _fail("the mock video beats must be logged at zero cost")
    ctx["paid_requests"] = len(ctx["tts_transport"].requests)
    return (
        f"completed; {N_BEATS} paid calls (model {ctx['MODEL']}, text verbatim);"
        f" manifest durations probed; spend log at parity (${N_BEATS * price:.2f})"
    )


def check_cap(ctx: dict[str, Any]) -> str:
    """Gate 5 - hard per-job cap: paid calls stop before the capped call,
    completed beats are kept, and a retry cannot sneak past a binding cap."""
    client = ctx["client"]
    price = ctx["PRICE"]
    cap = round(price * 1.5, 6)  # admits beat 1, blocks beats 2 and 3
    n0 = len(ctx["tts_transport"].requests)
    resp = client.post(
        "/input",
        data={"project": CAP_PROJECT, "script": script_of(PAID_BEATS)},
        follow_redirects=False,
    )
    if resp.status_code != 303:
        _fail(f"POST /input (cap project) -> {resp.status_code}")
    slug = ctx["slugify"](CAP_PROJECT)
    project_dir = ctx["projects_root"] / slug
    resp = client.post(
        "/options",
        data={**FORMAT_VIDEO, "audio": "on", "visual": "video", "spend_cap_usd": f"{cap}"},
        follow_redirects=False,
    )
    if resp.status_code != 303 or not resp.headers["location"].startswith("/confirm/"):
        _fail(f"POST /options (cap) -> {resp.status_code} {resp.headers.get('location')!r}")
    job_id = resp.headers["location"].rsplit("/", 1)[1]
    payload = client.get(f"/api/jobs/{job_id}").json()
    if abs((payload["spend_cap_usd"] or 0.0) - cap) > 1e-9:
        _fail(f"the job does not carry its cap: {payload['spend_cap_usd']}")
    if client.post(f"/jobs/{job_id}/confirm", follow_redirects=False).status_code != 303:
        _fail("cap job confirm failed")
    payload = _wait_terminal(ctx, job_id)
    if payload["status"] != "partial":
        _fail(f"a capped job must end partial, got {payload['status']!r}")
    tts = sorted(
        (a for a in payload["assets"] if a["kind"] == "tts"), key=lambda a: a["beat_index"]
    )
    if [a["status"] for a in tts] != ["done", "skipped", "skipped"]:
        _fail(f"cap must keep beat 1 and skip the rest, got {[a['status'] for a in tts]}")
    for asset in tts[1:]:
        if not asset["error"] or "spend cap" not in asset["error"]:
            _fail(f"skipped asset must name the cap: {asset['error']!r}")
        if asset["spent_usd"] != 0.0:
            _fail("a skipped asset must not carry spend")
    if abs(payload["spent_usd"] - price) > 1e-9:
        _fail(f"spent {payload['spent_usd']} != one beat ({price})")
    if len(ctx["tts_transport"].requests) - n0 != 1:
        _fail("the capped beats must never be dialed")
    if not (project_dir / "audio" / "beat_01.wav").is_file():
        _fail("the completed beat's audio was not kept")
    manifest = _manifest_of(ctx, project_dir, job_id)
    if len(manifest.beats) != N_BEATS:
        _fail(
            f"the partial job's manifest should cover its 3 visual beats, got {len(manifest.beats)}"
        )
    if not manifest.beats[0].audio_file:
        _fail("manifest beat 1 lost its audio")
    for beat in manifest.beats[1:]:
        if beat.audio_file:
            _fail(f"beat {beat.index} was cap-skipped - it must be a silent default beat")
        if beat.duration_s <= 0:
            _fail(f"silent beat {beat.index} needs a positive default duration")
    # The cap cannot be sneak-past via per-asset retry while it still binds.
    skipped_id = tts[1]["id"]
    resp = client.post(f"/jobs/{job_id}/assets/{skipped_id}/retry", follow_redirects=False)
    if resp.status_code != 303:
        _fail(f"POST retry -> {resp.status_code} {resp.headers.get('location')!r}")
    rerun = _wait_terminal(ctx, job_id)
    if rerun["status"] != "partial":
        _fail(f"the re-run must stay partial, got {rerun['status']!r}")
    rerun_tts = sorted(
        (a for a in rerun["assets"] if a["kind"] == "tts"), key=lambda a: a["beat_index"]
    )
    if [a["status"] for a in rerun_tts] != ["done", "skipped", "skipped"]:
        _fail("the retried asset must stay skipped while the cap binds")
    if not rerun_tts[1]["error"] or "spend cap" not in rerun_tts[1]["error"]:
        _fail("the re-skipped asset must name the cap")
    if abs(rerun["spent_usd"] - price) > 1e-9:
        _fail("the retry must not add spend")
    if len(ctx["tts_transport"].requests) - n0 != 1:
        _fail("the retry must not hide a paid call")
    ctx["cap_slug"], ctx["cap_job"] = slug, job_id
    return (
        f"partial at cap ${cap:.2f}: beat 1 done (${price}), beats 2-3 skipped before"
        f" their paid call; retry stays blocked; manifest keeps completed audio"
    )


def check_audit(ctx: dict[str, Any]) -> str:
    """Persisted state + the offline guarantees: guard silent, only the fake
    host ever dialed (and only offline), keys local, spend logs at parity."""
    guard: SocketGuard = ctx["guard"]
    if guard.violations:
        _fail(
            f"the flow made {len(guard.violations)} outbound network attempt(s):"
            f" {guard.violations[0].splitlines()[0]}"
        )
    for name in ("probe_transport", "tts_transport"):
        transport = ctx[name]
        for req in transport.requests:
            if req.url.host != GEMINI_HOST or req.url.scheme != "https":
                _fail(f"{name} dialed {req.url.scheme}://{req.url.host} - out of scope")
    price = ctx["PRICE"]
    # Dry project: one zero-cost mock entry per asset (3 tts + 3 video).
    dry = _spend_entries(ctx, ctx["dry_slug"], ctx["dry_job"])
    if len(dry) != 2 * N_BEATS or any(
        e["provider_id"] != "mock" or e["amount_usd"] != 0.0 for e in dry
    ):
        _fail(f"dry project: expected {2 * N_BEATS} zero-cost mock entries, got {len(dry)}")
    # Paid project: 3 gemini beats at the table price + 3 zero-cost mock videos.
    paid = _spend_entries(ctx, ctx["paid_slug"], ctx["paid_job"])
    gemini = [e for e in paid if e["provider_id"] == "gemini"]
    mock = [e for e in paid if e["provider_id"] == "mock"]
    if len(gemini) != N_BEATS or any(abs(e["amount_usd"] - price) > 1e-9 for e in gemini):
        _fail(f"paid project: expected {N_BEATS} gemini entries at ${price}")
    if len(mock) != N_BEATS or any(e["amount_usd"] != 0.0 for e in mock):
        _fail(f"paid project: expected {N_BEATS} zero-cost mock entries")
    if abs(sum(e["amount_usd"] for e in paid) - N_BEATS * price) > 1e-9:
        _fail(f"paid project: log total {sum(e['amount_usd'] for e in paid)} != {N_BEATS * price}")
    # Cap project: exactly 1 admitted gemini beat + the 3 zero-cost mock videos.
    cap = _spend_entries(ctx, ctx["cap_slug"], ctx["cap_job"])
    gemini_cap = [e for e in cap if e["provider_id"] == "gemini"]
    mock_cap = [e for e in cap if e["provider_id"] == "mock"]
    if len(gemini_cap) != 1 or abs(gemini_cap[0]["amount_usd"] - price) > 1e-9:
        _fail(f"cap project: expected exactly 1 gemini entry at ${price}")
    if len(mock_cap) != N_BEATS or any(e["amount_usd"] != 0.0 for e in mock_cap):
        _fail(f"cap project: expected {N_BEATS} zero-cost mock entries")
    if abs(sum(e["amount_usd"] for e in cap) - price) > 1e-9:
        _fail(f"cap project: log total {sum(e['amount_usd'] for e in cap)} != {price}")
    if not (ctx["state_dir"] / ctx["KEYS_FILENAME"]).is_file():
        _fail("the key store vanished from the local state dir")
    return (
        f"guard silent ({len(guard.loopback)} harness IPC calls); only"
        f" https://{GEMINI_HOST} via the offline fakes; keys stay in the local"
        f" state dir; spend logs at parity"
    )


SPECS: tuple[CheckSpec, ...] = (
    CheckSpec("environment", "python/ffmpeg/ffprobe + app + prices.json", check_environment),
    CheckSpec(
        "no-httpx-outside-providers",
        "httpx confined to app/providers/",
        check_no_httpx,
        ("environment",),
    ),
    CheckSpec(
        "boot-offline-app",
        "app + temp key store + gemini tts on a fake",
        check_boot,
        ("environment",),
    ),
    CheckSpec("socket-guard-live", "socket guard up", check_guard, ("boot-offline-app",)),
    CheckSpec(
        "settings-add-key",
        "Settings: add key, masked, raw never returned",
        check_settings_add,
        ("socket-guard-live",),
    ),
    CheckSpec(
        "settings-test-key-offline",
        "Settings: validate key vs MockTransport fake",
        check_settings_test_key,
        ("settings-add-key",),
    ),
    CheckSpec(
        "availability-defaults",
        "availability + default live and die with keys",
        check_availability_defaults,
        ("settings-test-key-offline",),
    ),
    CheckSpec(
        "dry-run-job-audio-manifest",
        "dry-run job: per-beat audio + valid manifest",
        check_dry_run_job,
        ("socket-guard-live",),
    ),
    CheckSpec(
        "estimate-before-spend-gate",
        "estimate gates start; zero unconfirmed traffic",
        check_estimate_gate,
        ("availability-defaults",),
    ),
    CheckSpec(
        "paid-job-completes-offline",
        "confirm admits 1 paid call/beat; manifest probed",
        check_paid_job,
        ("estimate-before-spend-gate",),
    ),
    CheckSpec(
        "cap-enforcement",
        "hard cap: stop paid calls, keep beats, retry-proof",
        check_cap,
        ("paid-job-completes-offline",),
    ),
    CheckSpec(
        "offline-audit",
        "guard silent; fake host only; keys local; logs at parity",
        check_audit,
        ("dry-run-job-audio-manifest", "paid-job-completes-offline", "cap-enforcement"),
    ),
)


# --- runner -----------------------------------------------------------------------


def _wait_terminal(ctx: dict[str, Any], job_id: str) -> dict[str, Any]:
    client = ctx["client"]
    terminal = ctx["TERMINAL"]
    deadline = time.monotonic() + JOB_TIMEOUT_S
    payload: dict[str, Any] = {}
    while time.monotonic() < deadline:
        payload = client.get(f"/api/jobs/{job_id}").json()
        if payload["status"] in terminal:
            return payload
        time.sleep(POLL_S)
    raise AssertionError(
        f"job {job_id} never reached a terminal status within {JOB_TIMEOUT_S:.0f}s"
        f" (now {payload.get('status')!r})"
    )


def _manifest_of(ctx: dict[str, Any], project_dir: Path, job_id: str):
    path = project_dir / "plan" / f"{job_id}{ctx['MANIFEST_SUFFIX']}"
    if not path.is_file():
        _fail(f"missing timing manifest: {path}")
    return ctx["read_manifest"](path)  # raises on any §6.2 violation


def _spend_entries(ctx: dict[str, Any], slug: str, job_id: str) -> list[dict[str, Any]]:
    log = ctx["projects_root"] / slug / "state" / "spend_log.jsonl"
    if not log.is_file():
        _fail(f"{slug}: state/spend_log.jsonl missing")
    rows = []
    for line in log.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return [row for row in rows if row.get("job_id") == job_id]


def _print_report(results: list[Result], total_s: float) -> None:
    width = max(len(spec.name) for spec in SPECS)
    print()
    for i, result in enumerate(results, 1):
        line = (
            f"[{i:2d}/{len(results)}] {result.status:<4} {result.name:<{width}} - {result.detail}"
        )
        print(f"{line} ({result.seconds:.1f}s)" if result.status == "PASS" else line)
        if result.tail:
            print(result.tail)
    passed = sum(1 for r in results if r.status == "PASS")
    failed = sum(1 for r in results if r.status == "FAIL")
    skipped = sum(1 for r in results if r.status == "SKIP")
    print("-" * 96)
    print(
        f"{len(results)} checks: {passed} passed, {failed} failed, {skipped} skipped"
        f" - {total_s:.1f}s total"
    )
    print(f"MILESTONE 2 SMOKE GATE: {'PASS' if failed == 0 else 'FAIL'}")


def main() -> int:
    warnings.filterwarnings("ignore")  # a gate reports its own checks, not deprecations
    print("=== ContentMaker - milestone 2 smoke gate (scripts/smoke_m2.py) ===")
    print(
        "Gate scope: BUILD_PLAN section 3 - BYOK settings (offline fake), key-gated"
        " availability, dry-run audio + manifest, estimate-before-spend, per-job cap."
    )
    started = time.monotonic()
    guard = SocketGuard()
    guard.install()
    ctx: dict[str, Any] = {"guard": guard}
    results: list[Result] = []
    workspace = Path(tempfile.mkdtemp(prefix="smoke_m2_"))
    ctx["workspace"] = workspace
    ctx["projects_root"] = workspace / "projects"
    ctx["state_dir"] = workspace / "state"
    failed = False
    try:
        blocked: set[str] = set()
        for spec in SPECS:
            unmet = [dep for dep in spec.requires if dep in blocked]
            if unmet:
                blocked.add(spec.name)  # a skipped check also blocks its dependents
                results.append(Result(spec.name, "SKIP", f"prerequisite '{unmet[0]}' did not pass"))
                continue
            started_at = time.monotonic()
            try:
                detail = spec.fn(ctx)
            except Exception as exc:  # a check failure IS the report
                blocked.add(spec.name)
                failed = True
                first_line = str(exc).splitlines()[0][:180]
                results.append(
                    Result(spec.name, "FAIL", first_line, time.monotonic() - started_at, _tail(exc))
                )
            else:
                results.append(Result(spec.name, "PASS", detail, time.monotonic() - started_at))
    finally:
        closer = ctx.get("close_client")
        if closer is not None:
            try:
                closer(None, None, None)  # TestClient exit: lifespan shutdown
            except Exception:  # cleanup must not mask the report
                pass
        guard.restore()
        shutil.rmtree(workspace, ignore_errors=True)
    _print_report(results, time.monotonic() - started)
    note = "temp workspace removed" if not workspace.exists() else f"temp workspace {workspace}"
    print(f"({note}; run with .venv/Scripts/python.exe scripts/smoke_m2.py)")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
