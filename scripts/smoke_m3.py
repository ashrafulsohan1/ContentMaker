#!/usr/bin/env python
"""Milestone 3 smoke gate - the BUILD_PLAN section 3 / spec 13.3 definition of done.

Gate scope (offline, end to end, mock assets - per the M3 tasking):

    1. platform-correct image deliverables at EXACT platform resolutions:
       two single-image formats (instagram_feed_portrait 1080x1350,
       x_single_image 1200x675) export one JPEG per beat, probed back at the
       catalog's exact width/height/codec - dimensions follow platforms.json,
       never a hardcoded size;
    2. a carousel yields N images: instagram_carousel (contract 2-10) on a
       3-beat script exports exactly 3 slides at 1080x1350, the job plan
       carries the min/max contract, out-of-range beat counts are refused at
       the options step (below minimum and above maximum), and
       GET /api/jobs/{id}/export bundles exactly the N slides into one ZIP;
    3. the MP4 is assembled via REAL ffmpeg from mock PNGs + WAVs with the
       timing manifest as single source of truth: youtube_shorts (1080x1920)
       stitches 3 mock-PNG beats + 3 mock-TTS WAVs into one H.264/AAC MP4
       whose probed duration matches the manifest total within tolerance and
       whose per-beat manifest durations match both ffprobe of each WAV and
       the mock TTS length formula;
    4. the consistency fixture (tests/test_consistency_fixture.py, task 3.5)
       passes on the same app: the 3-beat sample script runs to a completed
       carousel with ONE identical style block, ONE platform dims pair and
       pairwise-distinct prompts across all beats.

Everything runs over the real HTTP app (FastAPI TestClient - no server) on
the mock engines (BUILD_PLAN 1.4): mock TTS writes real WAV files, the mock
image provider writes real PNGs at platform dims, and all assembly/probing is
real ffmpeg/ffprobe (tasks 3.2/3.3 in the loop). Generation is dry-run at $0 -
no keys, no paid calls, nothing to fake at the HTTP layer.

Offline guarantees (gate requirement: ZERO real network, asserted not assumed):
- a process-wide socket guard records - and fails the run on - ANY outbound
  attempt (connect, connect_ex, create_connection, getaddrinfo,
  gethostbyname). The only traffic allowed through, always disclosed in the
  report, is the interpreter's own asyncio ``socket.socketpair`` self-pipe
  (loopback wakeup plumbing every TestClient portal needs on Windows) and
  address-literal resolvers that never touch DNS;
- the app boots on a mocks-only registry, so there is no provider client to
  dial with even if a key existed;
- an AST scan keeps ``httpx`` confined to ``app/providers/`` - the sole
  sanctioned outbound-HTTP layer (spec 6.5).

Usage:  .venv/Scripts/python.exe scripts/smoke_m3.py
Exit:   0 - every check passed (gate green); 1 - at least one check failed.

The gate runs against a throwaway projects root + key store (a temp dir), so
it is idempotent and leaves nothing behind.
"""

from __future__ import annotations

import ast
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
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # the script must run from any cwd

# The formats this gate pins, straight from app/data/platforms.json.
PORTRAIT = {"platform_id": "instagram", "format_id": "instagram_feed_portrait"}  # 1080x1350 jpg
X_IMAGE = {"platform_id": "x", "format_id": "x_single_image"}  # 1200x675 jpg
CAROUSEL = {"platform_id": "instagram", "format_id": "instagram_carousel"}  # 1080x1350 jpg 2-10
SHORTS = {"platform_id": "youtube", "format_id": "youtube_shorts"}  # 1080x1920 mp4
N_BEATS = 3
PORTAIT_BEATS = (
    "Portrait slide one introduces the topic.",
    "Portrait slide two carries the argument.",
)
X_BEATS = (
    "Wide image beat one, sixteen by nine.",
    "Wide image beat two closes the thread.",
)
CAROUSEL_BEATS = (
    "Carousel slide one opens the set with the hook.",
    "Carousel slide two explains the mechanism in plain words.",
    "Carousel slide three lands the call to action.",
)
# Distinct lengths so the mock TTS (len/15 s, min 1 s) yields three distinct
# durations the manifest must track exactly.
VIDEO_BEATS = (
    "Hook: three beats, one vertical video.",
    "The middle beat explains how the manifest drives every single cut.",
    "The closing beat is the longest, so the total duration is unmistakable.",
)
PROJECTS = {
    "portrait": "Smoke M3 Portrait",
    "ximage": "Smoke M3 X Image",
    "carousel": "Smoke M3 Carousel",
    "video": "Smoke M3 Video",
}
JOB_TIMEOUT_S = 300.0  # real ffmpeg encodes at platform dims on slow machines
POLL_S = 0.25
# Per-beat: manifest durations are ms-rounded, mock WAVs frame-rounded.
MS_TOLERANCE_S = 0.021
# ffprobe vs manifest audio duration (the seam the pipeline itself probes on).
PROBE_TOLERANCE_S = 0.005
GUARD_TARGETS = (
    "socket.socket.connect",
    "socket.socket.connect_ex",
    "socket.create_connection",
    "socket.getaddrinfo",
    "socket.gethostbyname",
)


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


def script_of(beats: tuple[str, ...]) -> str:
    return "\n\n".join(beats) + "\n"


# --- checks -----------------------------------------------------------------------


def check_environment(ctx: dict[str, Any]) -> str:
    """Python >= 3.11, ffmpeg + ffprobe present, the app imports, and
    platforms.json carries exactly the specs this gate pins."""
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
        manifest_mod = importlib.import_module("app.scripting.manifest")
        ctx["read_manifest"] = manifest_mod.read_manifest
        ctx["DEFAULT_FPS"] = manifest_mod.DEFAULT_FPS
        ctx["FFmpeg"] = importlib.import_module("app.assembly.ffmpeg").FFmpeg
        ctx["slugify"] = importlib.import_module("app.core.projects").slugify
        ctx["SUBDIRS"] = importlib.import_module("app.config").PROJECT_SUBDIRS
        keys_mod = importlib.import_module("app.core.keys")
        ctx["KeyStore"] = keys_mod.KeyStore
        ctx["KEYS_FILENAME"] = keys_mod.KEYS_FILENAME
        catalog = importlib.import_module("app.providers.catalog")
        ctx["PROVIDERS"] = catalog.PROVIDERS
        specs_mod = importlib.import_module("app.data.specs")
        ctx["load_platforms"] = specs_mod.load_platforms
        ctx["read_job_plan"] = importlib.import_module("app.assembly.pipeline").read_job_plan
    except Exception as exc:
        _fail(f"the app package does not import: {exc}\n{_tail(exc)}")
    # The consistency fixture of record (task 3.5) is imported, not copied: the
    # gate runs the sample script + options exactly as the fixture defines them.
    fixture_path = ROOT / "tests" / "test_consistency_fixture.py"
    if not fixture_path.is_file():
        _fail(f"the consistency fixture of record is missing: {fixture_path}")
    try:
        spec = importlib.util.spec_from_file_location("consistency_fixture", fixture_path)
        assert spec is not None and spec.loader is not None
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
    except Exception as exc:
        _fail(f"the consistency fixture does not import: {exc}\n{_tail(exc)}")
    ctx["fixture"] = fixture
    # Pin the catalog rows the gate asserts against (editable config - if the
    # data drifts, this names the row instead of failing obscurely downstream).
    expected = {
        ("instagram", "instagram_feed_portrait"): ("image", 1080, 1350, "jpg", 1, 1),
        ("x", "x_single_image"): ("image", 1200, 675, "jpg", 1, 1),
        ("instagram", "instagram_carousel"): ("carousel", 1080, 1350, "jpg", 2, 10),
        ("youtube", "youtube_shorts"): ("video", 1080, 1920, "mp4", 1, 1),
    }
    platforms = {(p.id, f.id): f for p in ctx["load_platforms"]() for f in p.formats}
    for key, want in expected.items():
        got = platforms.get(key)
        if got is None:
            _fail(f"platforms.json lost the format {key} this gate pins")
        have = (got.kind, *got.resolution, got.container, got.min_images, got.max_images)
        if have != want:
            _fail(f"platforms.json {key}: {have} != the pinned {want}")
    return f"python {py}; {len(expected)} pinned catalog rows verified"


def check_no_httpx(ctx: dict[str, Any]) -> str:
    """AST scan: no app/ module outside app/providers/ may import httpx.

    The provider adapters (app/providers/) legitimately dial their own cloud
    API - the ONLY sanctioned outbound HTTP in the app (spec 6.5). Everything
    else must stay network-free; this gate's flow dials nothing at all (mocks
    only), and the guard below fails the run on any real attempt.
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
        f" ({exempted} provider modules exempt - tasks 2.2/2.3/3.1 scope)"
    )


def check_boot(ctx: dict[str, Any]) -> str:
    """App factory on a MOCKS-ONLY registry (there is no provider client to
    dial), a throwaway key store + projects root, TestClient with lifespan."""
    store = ctx["KeyStore"](
        ctx["state_dir"] / ctx["KEYS_FILENAME"],
        provider_engines={pid: info.engines for pid, info in ctx["PROVIDERS"].items()},
        engines=ctx["ENGINES"],
    )
    if store.configured_providers():
        _fail("a fresh key store must start empty - this gate uses no keys at all")
    registry = ctx["Registry"]()
    registry.register("tts", ctx["MockTTSProvider"]())
    registry.register("image", ctx["MockImageProvider"]())
    registry.register("video", ctx["MockVideoProvider"]())
    app = ctx["create_app"](registry=registry, key_store=store, projects_root=ctx["projects_root"])
    from fastapi.testclient import TestClient

    client = TestClient(app)
    client.__enter__()  # lifespan: maintenance task + job worker, stopped at cleanup
    available = {
        engine: [p.provider_id for p in registry.providers_for(engine)] for engine in ctx["ENGINES"]
    }
    if available != {"tts": ["mock"], "image": ["mock"], "video": ["mock"]}:
        _fail(f"registry shape wrong: {available}")
    payload = client.get("/api/settings").json()
    if any(p["key_configured"] for p in payload["providers"]):
        _fail("no provider may report a configured key in this gate")
    ctx.update(client=client, close_client=client.__exit__, registry=registry, key_store=store)
    return "TestClient up (lifespan on); tts/image/video -> [mock] only; key store empty"


def check_guard(ctx: dict[str, Any]) -> str:
    """The socket guard is live and has recorded nothing but interpreter plumbing."""
    guard: SocketGuard = ctx["guard"]
    if not guard.installed:
        _fail("socket guard is not installed")
    if guard.violations:
        _fail(f"outbound network attempted before the flow even started:\n{guard.violations[0]}")
    return guard.summary() + " - guard up for every remaining check"


def _run_job(ctx: dict[str, Any], project: str, script: str, options: dict[str, str]) -> str:
    """The user path: input -> options -> awaiting_confirmation -> confirm;
    returns the job id once the job is confirmed (not yet terminal)."""
    client = ctx["client"]
    resp = client.post(
        "/input", data={"project": project, "script": script}, follow_redirects=False
    )
    if resp.status_code != 303:
        _fail(f"POST /input ({project}) -> {resp.status_code}")
    resp = client.post("/options", data=options, follow_redirects=False)
    if resp.status_code != 303 or not resp.headers["location"].startswith("/confirm/"):
        _fail(f"POST /options ({project}) -> {resp.status_code} {resp.headers.get('location')!r}")
    job_id = resp.headers["location"].rsplit("/", 1)[1]
    if client.post(f"/jobs/{job_id}/confirm", follow_redirects=False).status_code != 303:
        _fail(f"confirming job {job_id} failed")
    return job_id


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


def _completed_project(ctx: dict[str, Any], key: str, job_id: str) -> Path:
    """The common end-state assertions for every $0 mock job: completed, zero
    money, one done asset per kind; returns the project dir (and records the
    job for the audit check)."""
    payload = _wait_terminal(ctx, job_id)
    if payload["status"] != "completed":
        errors = [a["error"] for a in payload["assets"] if a["error"]]
        _fail(f"job {job_id} ended {payload['status']!r}; asset errors: {errors}")
    if payload["estimate_usd"] != 0.0 or payload["spent_usd"] != 0.0:
        _fail(f"mock job {job_id} is not $0: estimate {payload['estimate_usd']}")
    for asset in payload["assets"]:
        if asset["status"] != "done" or asset["spent_usd"] != 0.0:
            _fail(f"asset {asset['id']} is {asset['status']!r} at {asset['spent_usd']}")
    slug = ctx["slugify"](PROJECTS[key])
    project_dir = ctx["projects_root"] / slug
    missing = [d for d in ctx["SUBDIRS"] if not (project_dir / d).is_dir()]
    if missing:
        _fail(f"projects/{slug}/ is missing subfolder(s) {missing}")
    ctx.setdefault("jobs_done", []).append((slug, job_id))
    return project_dir


def _probed_stream(ctx: dict[str, Any], path: Path) -> dict[str, Any]:
    """The first media stream of a probed file (ffprobe through the real seam)."""
    ffmpeg: Any = ctx["FFmpeg"]()
    streams = ffmpeg.probe(path).get("streams", [])
    stream = next((s for s in streams if s.get("width") or s.get("codec_type") == "video"), None)
    if stream is None:
        _fail(f"no video stream in {path.name}: {streams}")
    return stream


def check_single_image_formats(ctx: dict[str, Any]) -> str:
    """Gate clause: image deliverables at EXACT platform resolutions.

    Two different single-image formats prove the export size comes from the
    platform catalog: instagram_feed_portrait (1080x1350) and x_single_image
    (1200x675) each export one JPEG per beat at exactly those dimensions,
    with the mock PNG sources themselves generated at platform dims.
    """
    cases = (
        ("portrait", PORTRAIT, PORTAIT_BEATS),
        ("ximage", X_IMAGE, X_BEATS),
    )
    parts: list[str] = []
    for key, options, beats in cases:
        job_id = _run_job(
            ctx,
            PROJECTS[key],
            script_of(beats),
            {**options, "audio": "on", "visual": "images", "dry_run": "on"},
        )
        project_dir = _completed_project(ctx, key, job_id)
        fmt = options["format_id"]
        expected = [project_dir / "output" / f"{fmt}_beat_{n:02d}.jpg" for n in (1, 2)]
        delivered = sorted(p.name for p in (project_dir / "output").glob(f"{fmt}*"))
        if delivered != sorted(p.name for p in expected):
            _fail(f"{fmt}: output has {delivered}, expected exactly the per-beat set")
        plan = ctx["read_job_plan"](project_dir, job_id)
        if plan is None or (plan.kind, plan.container) != ("image", "jpg"):
            _fail(f"{fmt}: job plan does not mirror the image format: {plan}")
        dims_want = (plan.width, plan.height)
        for n, slide in enumerate(expected, 1):
            if not slide.is_file() or slide.stat().st_size == 0:
                _fail(f"{fmt}: missing/empty deliverable {slide.name}")
            stream = _probed_stream(ctx, slide)
            have = (stream.get("codec_name"), stream.get("width"), stream.get("height"))
            if have != ("mjpeg", *dims_want):
                _fail(f"{slide.name}: probed {have}, expected mjpeg at exactly {dims_want}")
            source = _probed_stream(ctx, project_dir / "visuals" / f"beat_{n:02d}.png")
            if (source.get("codec_name"), source.get("width"), source.get("height")) != (
                "png",
                *dims_want,
            ):
                _fail(
                    f"beat {n}: mock PNG source is {source.get('width')}x"
                    f"{source.get('height')}, expected the platform dims {dims_want}"
                )
        parts.append(f"{fmt} {'x'.join(map(str, dims_want))} x{len(expected)}")
    return "exact platform resolutions: " + "; ".join(parts)


def check_carousel_n_images(ctx: dict[str, Any]) -> str:
    """Gate clause: a carousel yields N images (D14 count contract + ZIP).

    instagram_carousel (2-10) on 3 beats exports exactly 3 slides at
    1080x1350; the plan carries (min 2, max 10); GET /api/jobs/{id}/export
    bundles exactly those 3 files byte-identical; and out-of-range beat
    counts (1 and 11) are refused at the options step before any job exists.
    """
    client = ctx["client"]
    job_id = _run_job(
        ctx,
        PROJECTS["carousel"],
        script_of(CAROUSEL_BEATS),
        {**CAROUSEL, "audio": "on", "visual": "images", "dry_run": "on"},
    )
    project_dir = _completed_project(ctx, "carousel", job_id)
    expected = [project_dir / "output" / f"instagram_carousel_beat_{n:02d}.jpg" for n in (1, 2, 3)]
    delivered = sorted(p.name for p in (project_dir / "output").glob("instagram_carousel*"))
    if delivered != sorted(p.name for p in expected):
        _fail(f"carousel output is {delivered}, expected exactly {len(expected)} slides")
    for slide in expected:
        stream = _probed_stream(ctx, slide)
        have = (stream.get("codec_name"), stream.get("width"), stream.get("height"))
        if have != ("mjpeg", 1080, 1350):
            _fail(f"{slide.name}: probed {have}, expected mjpeg at exactly 1080x1350")
    plan = ctx["read_job_plan"](project_dir, job_id)
    if plan is None or (plan.kind, plan.min_images, plan.max_images) != ("carousel", 2, 10):
        _fail(f"carousel plan does not carry the platform count contract: {plan}")
    # The carousel upload unit: one ZIP attachment carrying exactly the set.
    resp = client.get(f"/api/jobs/{job_id}/export")
    if resp.status_code != 200 or resp.headers["content-type"] != "application/zip":
        _fail(f"GET export -> {resp.status_code} {resp.headers.get('content-type')}")
    if "attachment" not in resp.headers.get("content-disposition", ""):
        _fail("the ZIP must download as an attachment")
    with zipfile.ZipFile(io.BytesIO(resp.content)) as bundle:
        names = sorted(bundle.namelist())
        if names != sorted(p.name for p in expected):
            _fail(f"ZIP members {names} != the {len(expected)} slides on disk")
        for slide in expected:
            if bundle.read(slide.name) != slide.read_bytes():
                _fail(f"ZIP member {slide.name} differs from the file on disk")
    # The count contract at the boundary: below minimum and above maximum are
    # refused at the options step - no job, no assets, loud message.
    for count, fragment in ((1, "at least 2 images"), (11, "at most 10 images")):
        script = script_of(
            tuple(f"Beat {i} of an out-of-range carousel." for i in range(1, count + 1))
        )
        resp = client.post(
            "/input",
            data={"project": f"Smoke M3 Carousel {count}", "script": script},
            follow_redirects=False,
        )
        if resp.status_code != 303:
            _fail(f"POST /input ({count} beats) -> {resp.status_code}")
        resp = client.post(
            "/options",
            data={**CAROUSEL, "audio": "on", "visual": "images", "dry_run": "on"},
            follow_redirects=False,
        )
        page = resp.text
        if resp.status_code == 303 or fragment not in page:
            _fail(f"a {count}-beat carousel must be refused naming the contract ({fragment})")
    return "3 slides at 1080x1350; plan 2-10; ZIP = exactly the set; 1/11-beat sets refused"


def check_video_manifest(ctx: dict[str, Any]) -> str:
    """Gate clause: MP4 via REAL ffmpeg from mock PNGs + WAVs, manifest timings.

    youtube_shorts (1080x1920) stitches 3 image beats + 3 audio beats: the
    manifest carries each beat's probed WAV duration (and nothing else sets
    the timeline); the delivered MP4 probes back H.264/AAC at exactly
    1080x1920 @ the manifest fps with a total duration inside tolerance of
    the manifest total.
    """
    job_id = _run_job(
        ctx,
        PROJECTS["video"],
        script_of(VIDEO_BEATS),
        {**SHORTS, "audio": "on", "visual": "images", "dry_run": "on"},
    )
    project_dir = _completed_project(ctx, "video", job_id)
    out = project_dir / "output" / "youtube_shorts.mp4"
    if not out.is_file() or out.stat().st_size == 0:
        _fail("the video deliverable youtube_shorts.mp4 is missing/empty")
    delivered = sorted(p.name for p in (project_dir / "output").glob("youtube_shorts*"))
    if delivered != ["youtube_shorts.mp4"]:
        _fail(f"video output has {delivered}, expected exactly one MP4")
    manifest = ctx["read_manifest"](
        project_dir / "plan" / f"{job_id}_manifest.json"
    )  # raises on any §6.2 violation
    if len(manifest.beats) != N_BEATS or [b.index for b in manifest.beats] != [1, 2, 3]:
        _fail(f"manifest must cover beats 1..{N_BEATS} contiguously")
    if manifest.fps_target != ctx["DEFAULT_FPS"]:
        _fail(f"manifest fps {manifest.fps_target} != DEFAULT_FPS")
    ffmpeg: Any = ctx["FFmpeg"]()
    for beat in manifest.beats:
        if not beat.audio_file:
            _fail(f"beat {beat.index}: manifest beat has no audio_file")
        audio = project_dir / beat.audio_file
        if not audio.is_file():
            _fail(f"beat {beat.index}: audio file missing: {beat.audio_file}")
        probed = round(ffmpeg.probe_duration(audio), 3)
        if abs(probed - beat.duration_s) > PROBE_TOLERANCE_S:
            _fail(f"beat {beat.index}: manifest {beat.duration_s}s vs probed {probed}s")
        expected = max(1.0, len(beat.text) / 15.0)  # the mock TTS length formula
        if abs(beat.duration_s - expected) > MS_TOLERANCE_S:
            _fail(
                f"beat {beat.index}: manifest {beat.duration_s}s is not the mock's"
                f" {expected:.3f}s for its text - durations must be probed, not guessed"
            )
        source = _probed_stream(ctx, project_dir / "visuals" / f"beat_{beat.index:02d}.png")
        if (source.get("width"), source.get("height")) != (1080, 1920):
            _fail(f"beat {beat.index}: mock PNG is not at platform dims 1080x1920")
    durations = [b.duration_s for b in manifest.beats]
    if len(set(durations)) != N_BEATS:
        _fail(f"the three beats must have distinct durations, got {durations}")
    if abs(sum(durations) - manifest.total_duration_s) > 0.001:
        _fail("manifest total duration does not equal the beat sum")
    # The deliverable itself: H.264/AAC at exact platform dims and fps, and a
    # total that stays inside the assembler's own alignment tolerance.
    streams = ffmpeg.probe(out)["streams"]
    video = next(s for s in streams if s.get("codec_type") == "video")
    audio = next(s for s in streams if s.get("codec_type") == "audio")
    if (video.get("codec_name"), video.get("width"), video.get("height")) != (
        "h264",
        1080,
        1920,
    ):
        _fail(
            f"MP4 video stream is {video.get('codec_name')} {video.get('width')}x"
            f"{video.get('height')}, expected h264 1080x1920"
        )
    if video.get("pix_fmt") != "yuv420p" or video.get("sample_aspect_ratio") != "1:1":
        _fail(
            f"MP4 is not normalized: pix_fmt={video.get('pix_fmt')}"
            f" sar={video.get('sample_aspect_ratio')}"
        )
    # fps normalization survives the concat: the codec rate is exactly the
    # manifest target (avg_frame_rate is the container's played average and
    # drifts a fraction of a fps with per-segment audio quantization).
    if eval_frac(video.get("r_frame_rate", "")) != float(manifest.fps_target):
        _fail(f"MP4 fps {video.get('r_frame_rate')} != manifest {manifest.fps_target}")
    if (audio.get("codec_name"), audio.get("sample_rate"), audio.get("channels")) != (
        "aac",
        "44100",
        2,
    ):
        _fail(
            f"MP4 audio stream is {audio.get('codec_name')}@{audio.get('sample_rate')}x"
            f"{audio.get('channels')}, expected aac 44100 stereo"
        )
    total = ffmpeg.probe_duration(out)
    tolerance = N_BEATS / float(manifest.fps_target) + 0.2
    if abs(total - manifest.total_duration_s) > tolerance:
        _fail(
            f"assembled MP4 lasts {total:.3f}s but the manifest total is"
            f" {manifest.total_duration_s:.3f}s (tolerance {tolerance:.3f}s)"
        )
    ctx["video_total_s"], ctx["video_manifest_s"] = round(total, 3), manifest.total_duration_s
    return (
        f"h264/aac 1080x1920 @{manifest.fps_target:.0f}fps; {total:.3f}s probed vs"
        f" {manifest.total_duration_s:.3f}s manifest (3 distinct beat durations)"
    )


def eval_frac(text: str) -> float:
    """ffmpeg's '30/1' frame-rate notation as a float (0.0 when unparseable)."""
    try:
        num, _, den = text.partition("/")
        return float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0


def check_consistency_fixture(ctx: dict[str, Any]) -> str:
    """Gate clause: the consistency fixture (task 3.5) passes.

    The fixture of record's own 3-beat sample script + options (imported from
    tests/test_consistency_fixture.py, not copied) run on this app to a
    completed instagram_carousel, and the §6.3 coordination holds at the
    generation-request seam: ONE identical style block, ONE platform dims
    pair, pairwise-distinct prompts - with all three slides on disk.
    """
    fixture = ctx["fixture"]
    job_id = _run_job(
        ctx, "Smoke M3 Consistency", fixture.SAMPLE_SCRIPT, dict(fixture.CAROUSEL_OPTIONS)
    )
    payload = _wait_terminal(ctx, job_id)
    if payload["status"] != "completed":
        errors = [a["error"] for a in payload["assets"] if a["error"]]
        _fail(f"the fixture run ended {payload['status']!r}; asset errors: {errors}")
    slides = sorted(
        (a for a in payload["assets"] if a["kind"] == "image"), key=lambda a: a["beat_index"]
    )
    if [a["beat_index"] for a in slides] != [1, 2, 3]:
        _fail("the fixture run must produce one image asset per beat 1..3")
    style_hints = {a["params"]["style_hint"] for a in slides}
    if style_hints != {fixture.STYLE}:
        _fail(f"the fixed style block must reach every beat, got {style_hints!r}")
    dims = {(a["params"]["width"], a["params"]["height"]) for a in slides}
    if dims != {(1080, 1350)}:
        _fail(f"platform dims must be identical on every beat, got {dims!r}")
    prompts = [a["params"]["prompt"] for a in slides]
    if len(set(prompts)) != 3:
        _fail("beat prompts must stay distinct - anchoring coordinates style, not subject")
    project_dir = ctx["projects_root"] / ctx["slugify"]("Smoke M3 Consistency")
    for n in (1, 2, 3):
        slide = project_dir / "output" / f"instagram_carousel_beat_{n:02d}.jpg"
        if not slide.is_file() or slide.stat().st_size == 0:
            _fail(f"fixture slide missing: {slide}")
        stream = _probed_stream(ctx, slide)
        if (stream.get("codec_name"), stream.get("width"), stream.get("height")) != (
            "mjpeg",
            1080,
            1350,
        ):
            _fail(f"fixture slide {n} is not an mjpeg 1080x1350 slide")
    ctx.setdefault("jobs_done", []).append((project_dir.name, job_id))
    return (
        "3-beat sample run completed: 1 style block, 1 dims pair (1080x1350),"
        " 3 distinct prompts, 3 slides on disk"
    )


def check_audit(ctx: dict[str, Any]) -> str:
    """The offline guarantees: guard silent, no keys touched, every mock job
    logged at exactly $0 by the mock provider."""
    guard: SocketGuard = ctx["guard"]
    if guard.violations:
        _fail(
            f"the flow made {len(guard.violations)} outbound network attempt(s):"
            f" {guard.violations[0].splitlines()[0]}"
        )
    if ctx["key_store"].configured_providers():
        _fail("this gate must never touch a provider key")
    done: list[tuple[str, str]] = ctx.get("jobs_done", [])
    if len(done) != 5:
        _fail(f"expected 5 completed mock jobs in the audit, got {len(done)}")
    total_entries = 0
    for slug, job_id in done:
        log = ctx["projects_root"] / slug / "state" / "spend_log.jsonl"
        if not log.is_file():
            _fail(f"{slug}: state/spend_log.jsonl missing")
        rows = [
            json.loads(line)
            for line in log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        mine = [row for row in rows if row.get("job_id") == job_id]
        if not mine:
            _fail(f"{slug}: no spend entries recorded for job {job_id}")
        for row in mine:
            if row.get("provider_id") != "mock" or row.get("amount_usd") != 0.0:
                _fail(f"{slug}: spend log entry is not a $0 mock entry: {row}")
        total_entries += len(mine)
    return (
        f"guard silent ({len(guard.loopback)} harness IPC calls); no keys touched;"
        f" {total_entries} spend entries across {len(done)} jobs, all $0 mock"
    )


SPECS: tuple[CheckSpec, ...] = (
    CheckSpec(
        "environment",
        "python/ffmpeg/ffprobe + app + pinned catalog rows",
        check_environment,
    ),
    CheckSpec(
        "no-httpx-outside-providers",
        "httpx confined to app/providers/",
        check_no_httpx,
        ("environment",),
    ),
    CheckSpec(
        "boot-offline-app",
        "mocks-only registry + temp key store/projects root",
        check_boot,
        ("environment",),
    ),
    CheckSpec("socket-guard-live", "socket guard up", check_guard, ("boot-offline-app",)),
    CheckSpec(
        "single-image-platform-resolution",
        "image exports at exact platform resolutions",
        check_single_image_formats,
        ("socket-guard-live",),
    ),
    CheckSpec(
        "carousel-n-images",
        "carousel yields N images; count contract; ZIP export",
        check_carousel_n_images,
        ("socket-guard-live",),
    ),
    CheckSpec(
        "video-manifest-assembly",
        "MP4 via real ffmpeg, manifest timings within tolerance",
        check_video_manifest,
        ("socket-guard-live",),
    ),
    CheckSpec(
        "consistency-fixture",
        "3-beat consistency fixture of record passes",
        check_consistency_fixture,
        ("socket-guard-live",),
    ),
    CheckSpec(
        "offline-audit",
        "guard silent; no keys; all jobs $0 mock",
        check_audit,
        (
            "single-image-platform-resolution",
            "carousel-n-images",
            "video-manifest-assembly",
            "consistency-fixture",
        ),
    ),
)


# --- runner -----------------------------------------------------------------------


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
    print(f"MILESTONE 3 SMOKE GATE: {'PASS' if failed == 0 else 'FAIL'}")


def main() -> int:
    warnings.filterwarnings("ignore")  # a gate reports its own checks, not deprecations
    print("=== ContentMaker - milestone 3 smoke gate (scripts/smoke_m3.py) ===")
    print(
        "Gate scope: BUILD_PLAN section 3 - platform-correct exports at exact"
        " resolutions, carousel N images + ZIP, MP4 via real ffmpeg from mock"
        " PNGs + WAVs on manifest timings, consistency fixture. Mock assets,"
        " dry-run, zero real network."
    )
    started = time.monotonic()
    guard = SocketGuard()
    guard.install()
    ctx: dict[str, Any] = {"guard": guard}
    results: list[Result] = []
    workspace = Path(tempfile.mkdtemp(prefix="smoke_m3_"))
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
    print(f"({note}; run with .venv/Scripts/python.exe scripts/smoke_m3.py)")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
