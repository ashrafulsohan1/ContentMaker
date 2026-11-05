#!/usr/bin/env python
"""Milestone 1 smoke gate - the BUILD_PLAN section 3 / spec 13.1 definition of done.

Drives the whole product flow over the real HTTP app (FastAPI TestClient - no
server, no network) on the zero-network mock engines only:

    create project -> paste script -> segment -> review beats -> pick
    platform/format -> dry-run generate -> job completes with per-asset
    statuses -> timing manifest valid -> deliverables exist in output/ at
    the chosen platform dimensions -> projects/<name>/ structure correct.

Two full runs cover both assembly paths (BUILD_PLAN 1.12):
- a video job (youtube_shorts: TTS + mock clips -> stitched 1080x1920 MP4),
- an image job (instagram_feed_square: TTS + mock PNGs -> 1080x1080 JPEGs).

Offline guarantees (gate requirement: ZERO network calls, asserted, not assumed):
- every ``app/`` module is AST-scanned: no ``import httpx`` / ``from httpx``;
- a process-wide socket guard records - and fails the run on - ANY outbound
  attempt (connect, connect_ex, create_connection, getaddrinfo, gethostbyname)
  for the whole run. The only traffic allowed through, and always disclosed in
  the report, is the interpreter's own asyncio ``socket.socketpair`` self-pipe
  (loopback-only wakeup plumbing every TestClient portal needs on Windows) and
  address-literal resolvers that never touch DNS. The app itself dials nothing:
  the run finishing green under the guard is the proof.

Usage:  .venv/Scripts/python.exe scripts/smoke_m1.py
Exit:   0 - every check passed (gate green); 1 - at least one check failed.

The gate runs against a throwaway projects root (a temp dir), never the repo's
own ``projects/`` folder, so it is idempotent and leaves nothing behind.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # the script must run from any cwd

PROJECT_VIDEO = "Smoke Video"  # slug comes from app code - never assumed
PROJECT_IMAGE = "Smoke Image"
FORMAT_VIDEO = {"platform_id": "youtube", "format_id": "youtube_shorts"}  # 1080x1920 MP4
FORMAT_IMAGE = {"platform_id": "instagram", "format_id": "instagram_feed_square"}  # 1080x1080
SCRIPT = (
    "[scene: hook]\n"
    "Smoke gate opens with a one-line hook beat.\n"
    "\n"
    "Second beat explains the milestone gate in a couple of plain spoken sentences.\n"
    "\n"
    "<!-- img: a tidy desk with a laptop and a clapperboard -->\n"
    "Third beat carries the image hint for its visual.\n"
)
EDITED_HOOK = "Edited hook line for the smoke gate."
N_BEATS = 3
JOB_TIMEOUT_S = 240.0  # real ffmpeg encodes on slow machines; generous by design
POLL_S = 0.25
GUARD_TARGETS = (
    "socket.socket.connect",
    "socket.socket.connect_ex",
    "socket.create_connection",
    "socket.getaddrinfo",
    "socket.gethostbyname",
)


# --- zero-network guard ---------------------------------------------------------


class NetworkAttempt(AssertionError):
    """Any code tried to open a real network connection during the smoke run."""


def _target_host(args: tuple[Any, ...]) -> str | None:
    """The host a connect/create_connection/getaddrinfo call targets."""
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
    if host is None:
        return False
    return host in ("localhost", "::1") or host.startswith("127.")


def _is_address_literal(host: str | None) -> bool:
    """A numeric IP never hits DNS - resolving it is a purely local operation."""
    if host is None:
        return False
    if _is_loopback(host):
        return True
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
        self.violations: list[str] = []  # target + caller stack of every denial
        self.loopback: list[str] = []  # allowed interpreter self-pipes / local resolvers
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
                rest = args[1:] if method_patch else args  # patched methods receive self
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
        if self.loopback:
            note = f"; {len(self.loopback)} interpreter self-pipe/local-resolver call(s)"
        else:
            note = ""
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
    """Python >= 3.11, ffmpeg + ffprobe present, and the app package imports."""
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
        ctx["default_registry"] = app_main.default_registry
        ctx["ENGINES"] = importlib.import_module("app.engines.registry").ENGINES
        ctx["TERMINAL"] = importlib.import_module("app.core.jobs").TERMINAL_JOB_STATUSES
        ctx["slugify"] = importlib.import_module("app.core.projects").slugify
        ctx["SUBDIRS"] = importlib.import_module("app.config").PROJECT_SUBDIRS
        ctx["FFmpeg"] = importlib.import_module("app.assembly.ffmpeg").FFmpeg
        ctx["SUFFIX"] = importlib.import_module("app.assembly.pipeline").MANIFEST_SUFFIX
        manifest_mod = importlib.import_module("app.scripting.manifest")
        ctx["read_manifest"] = manifest_mod.read_manifest
        ctx["DEFAULT_FPS"] = manifest_mod.DEFAULT_FPS
    except Exception as exc:
        _fail(f"the app package does not import: {exc}\n{_tail(exc)}")
    return f"python {py}; {versions['ffmpeg'][:58]}; ffprobe present"


def check_boot(ctx: dict[str, Any]) -> str:
    """create_app() with the default registry: mocks only, TestClient up."""
    registry = ctx["default_registry"]()
    available = {
        engine: [provider.provider_id for provider in registry.providers_for(engine)]
        for engine in ctx["ENGINES"]
    }
    if available != {"tts": ["mock"], "image": ["mock"], "video": ["mock"]}:
        _fail(f"registry must hold only the zero-network mocks, got {available}")
    app = ctx["create_app"](registry=registry, projects_root=ctx["projects_root"])
    if app.state.projects_root != ctx["projects_root"]:
        _fail("app.state.projects_root is not the smoke projects root")
    from fastapi.testclient import TestClient

    client = TestClient(app)
    client.__enter__()  # lifespan: maintenance task + job worker, stopped at cleanup
    ctx["client"] = client
    ctx["close_client"] = client.__exit__
    return "TestClient up (lifespan on); engines tts/image/video -> mock only"


def check_no_httpx(ctx: dict[str, Any]) -> str:
    """AST scan: no app/ module outside app/providers/ may import httpx.

    M1 gate was app-wide (zero network anywhere). From M2 the provider
    adapters (app/providers/) legitimately dial their own cloud API — that is
    the ONLY sanctioned outbound HTTP in the app, and each key travels solely
    to the provider it belongs to (spec 6.5). The rest of the app — UI flow,
    jobs, cost, assembly, settings/storage — must stay network-free, which is
    what this check continues to prove; the dry-run flow below never dials.
    """
    app_dir = ROOT / "app"
    providers_dir = app_dir / "providers"
    scanned = 0
    exempted = 0
    offenders: list[str] = []
    for path in sorted(app_dir.rglob("*.py")):
        if providers_dir in path.parents:
            exempted += 1  # provider adapters own the outbound HTTP (task 2.1+)
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
        f" ({exempted} provider modules exempt — task 2.1 scope)"
    )


def check_guard(ctx: dict[str, Any]) -> str:
    """The socket guard is live and has recorded nothing but interpreter plumbing."""
    guard: SocketGuard = ctx["guard"]
    if not guard.installed:
        _fail("socket guard is not installed")
    if guard.violations:
        _fail(f"outbound network attempted before the flow even started:\n{guard.violations[0]}")
    return guard.summary() + " - guard up for every remaining check"


def check_input(ctx: dict[str, Any]) -> str:
    """POST /input creates the project with the full section-7 tree + verbatim script."""
    client = ctx["client"]
    index = client.get("/")
    if index.status_code != 200:
        _fail(f"GET / -> {index.status_code}")
    for field in ('name="project"', 'name="script"', 'name="script_file"'):
        if field not in index.text:
            _fail(f"input form is missing {field}")
    resp = client.post(
        "/input", data={"project": PROJECT_VIDEO, "script": SCRIPT}, follow_redirects=False
    )
    if resp.status_code != 303 or resp.headers["location"] != "/review":
        _fail(f"POST /input -> {resp.status_code} {resp.headers.get('location')!r}")
    slug = ctx["slugify"](PROJECT_VIDEO)
    project_dir = ctx["projects_root"] / slug
    missing = [d for d in ctx["SUBDIRS"] if not (project_dir / d).is_dir()]
    if missing:
        _fail(f"projects/{slug}/ is missing subfolder(s) {missing}")
    saved = project_dir / "script" / "script.txt"
    if not saved.is_file() or saved.read_text(encoding="utf-8") != SCRIPT:
        _fail("script/script.txt is missing or does not match the pasted script verbatim")
    ctx["video_slug"] = slug
    ctx["video_dir"] = project_dir
    return f"projects/{slug}/ with {len(ctx['SUBDIRS'])} subfolders; script saved verbatim"


def check_review(ctx: dict[str, Any]) -> str:
    """Segmentation produced the expected beats; one review edit round-trips."""
    client = ctx["client"]
    review = client.get("/review")
    if review.status_code != 200:
        _fail(f"GET /review -> {review.status_code}")
    beat_ids = re.findall(r'action="/review/beat/([A-Za-z0-9]+)/edit"', review.text)
    texts = [
        text.strip()
        for text in re.findall(r"<textarea[^>]*>(.*?)</textarea>", review.text, re.DOTALL)
    ]
    if len(beat_ids) != N_BEATS or len(texts) != N_BEATS:
        _fail(f"expected {N_BEATS} beats, found {len(beat_ids)} ids / {len(texts)} texts")
    if "tidy desk with a laptop" not in review.text:
        _fail("the <!-- img: --> hint did not reach the review page")
    if texts[0] != "Smoke gate opens with a one-line hook beat.":
        _fail(f"beat 1 text drifted: {texts[0]!r}")
    resp = client.post(
        f"/review/beat/{beat_ids[0]}/edit",
        data={"text": EDITED_HOOK, "label": "hook"},
        follow_redirects=False,
    )
    if resp.status_code != 303:
        _fail(f"beat edit -> {resp.status_code}")
    if EDITED_HOOK not in client.get("/review").text:
        _fail("edited beat text did not survive a reload")
    return f"{N_BEATS} beats parsed ([scene:] label + img hint carried); edit round-trips"


def check_options_video(ctx: dict[str, Any]) -> str:
    """Options: platform/format from the editable catalog; dry-run estimate is $0.0000."""
    client = ctx["client"]
    options = client.get("/options")
    if options.status_code != 200:
        _fail(f"GET /options -> {options.status_code}")
    if 'value="youtube"' not in options.text or 'value="youtube_shorts"' not in options.text:
        _fail("platforms.json catalog not offered on the options page")
    if "checked" not in options.text.split('name="dry_run"', 1)[1][:120]:
        _fail("dry-run is not the default on the options page")
    resp = client.post(
        "/options",
        data={**FORMAT_VIDEO, "audio": "on", "visual": "video", "dry_run": "on"},
        follow_redirects=False,
    )
    if resp.status_code != 303 or not resp.headers["location"].startswith("/confirm/"):
        _fail(f"POST /options -> {resp.status_code} {resp.headers.get('location')!r}")
    job_id = resp.headers["location"].rsplit("/", 1)[1]
    confirm = client.get(f"/confirm/{job_id}")
    if confirm.status_code != 200:
        _fail(f"GET /confirm/{job_id} -> {confirm.status_code}")
    if "$0.0000" not in confirm.text:
        _fail("dry-run estimate is not exactly zero on the confirmation page")
    if f"/jobs/{job_id}/confirm" not in confirm.text:
        _fail("confirmation page does not offer the explicit confirm action")
    ctx["video_job"] = job_id
    return f"created job {job_id} (youtube/shorts); confirm page shows $0.0000"


def check_state_video(ctx: dict[str, Any]) -> str:
    """Pre-run API state: awaiting_confirmation, 6 assets, all mock, zero estimate."""
    client = ctx["client"]
    job_id = ctx["video_job"]
    payload = client.get(f"/api/jobs/{job_id}").json()
    if payload["status"] != "awaiting_confirmation":
        _fail(f"job status is {payload['status']!r} before confirmation")
    if payload["estimate_usd"] != 0.0 or payload["spent_usd"] != 0.0:
        _fail(
            f"dry-run money is not zero: estimate={payload['estimate_usd']}"
            f" spent={payload['spent_usd']}"
        )
    if payload["providers"] != {"tts": "mock", "video": "mock"}:
        _fail(f"providers are not the mocks: {payload['providers']}")
    assets = payload["assets"]
    kinds = sorted(asset["kind"] for asset in assets)
    if kinds != ["tts"] * N_BEATS + ["video"] * N_BEATS:
        _fail(f"asset kinds wrong: {kinds}")
    statuses = {asset["status"] for asset in assets}
    if statuses != {"pending"}:
        _fail(f"assets not all pending before start: {statuses}")
    project_dir = ctx["video_dir"]
    for asset in assets:
        out = Path(asset["out_path"])
        if not out.is_absolute() or project_dir not in out.parents:
            _fail(f"asset out_path {out} is outside projects/{ctx['video_slug']}/")
        expected_dir = "audio" if asset["kind"] == "tts" else "visuals"
        if out.parent.name != expected_dir:
            _fail(f"{asset['kind']} asset lives in {out.parent.name}/ not {expected_dir}/")
    return f"awaiting_confirmation; {len(assets)} assets (3 tts + 3 video) all pending, $0.0"


def check_run_video(ctx: dict[str, Any]) -> str:
    """Confirm -> worker runs the job -> completed with every asset done."""
    client = ctx["client"]
    job_id = ctx["video_job"]
    resp = client.post(f"/jobs/{job_id}/confirm", follow_redirects=False)
    if resp.status_code != 303 or resp.headers["location"] != f"/jobs/{job_id}":
        _fail(f"POST confirm -> {resp.status_code} {resp.headers.get('location')!r}")
    payload = _wait_terminal(ctx, job_id)
    if payload["status"] != "completed":
        errors = [asset["error"] for asset in payload["assets"] if asset["error"]]
        _fail(f"job ended {payload['status']!r} (expected completed); asset errors: {errors}")
    statuses = {asset["status"] for asset in payload["assets"]}
    if statuses != {"done"}:
        _fail(f"per-asset statuses not all done: {sorted(statuses)}")
    progress = payload["progress"]
    if progress["percent"] != 100 or progress["done"] != progress["total"]:
        _fail(f"progress wrong: {progress}")
    if payload["spent_usd"] != 0.0:
        _fail(f"dry-run job spent {payload['spent_usd']}")
    return f"completed; {progress['done']}/{progress['total']} assets done; spent $0.0"


def check_manifest_video(ctx: dict[str, Any]) -> str:
    """The timing manifest exists and passes the strict section-6.2 reader."""
    job_id = ctx["video_job"]
    project_dir = ctx["video_dir"]
    path = project_dir / "plan" / f"{job_id}{ctx['SUFFIX']}"
    if not path.is_file():
        _fail(f"missing timing manifest: {path}")
    manifest = ctx["read_manifest"](path)  # raises on any section-6.2 violation
    if manifest.job_id != job_id:
        _fail(f"manifest job_id {manifest.job_id!r} != {job_id!r}")
    if len(manifest.beats) != N_BEATS:
        _fail(f"manifest has {len(manifest.beats)} beats, expected {N_BEATS}")
    if manifest.fps_target != ctx["DEFAULT_FPS"]:
        _fail(f"fps_target {manifest.fps_target} != DEFAULT_FPS {ctx['DEFAULT_FPS']}")
    ffmpeg = ctx["FFmpeg"]()
    for beat in manifest.beats:
        if not beat.audio_file:
            _fail(f"beat {beat.index} has no audio_file")
        audio = project_dir / beat.audio_file
        if not audio.is_file():
            _fail(f"beat {beat.index}: audio file missing on disk: {audio}")
        if beat.duration_s <= 0:
            _fail(f"beat {beat.index}: non-positive duration {beat.duration_s}")
        probed = round(ffmpeg.probe_duration(audio), 3)
        if abs(probed - beat.duration_s) > 0.005:
            _fail(f"beat {beat.index}: manifest {beat.duration_s}s vs probed {probed}s")
    if manifest.beats[0].text != EDITED_HOOK:
        _fail(f"manifest beat 1 text is not the edited beat: {manifest.beats[0].text!r}")
    total = sum(beat.duration_s for beat in manifest.beats)
    if abs(total - manifest.total_duration_s) > 0.001:
        _fail(f"total {manifest.total_duration_s} != beat sum {total}")
    ctx["video_manifest"] = manifest
    return (
        f"{len(manifest.beats)} beats, contiguous 0..{manifest.total_duration_s:.3f}s"
        f" @ {manifest.fps_target}fps, durations match ffprobe, text tracks the edited beat"
    )


def check_deliverable_video(ctx: dict[str, Any]) -> str:
    """output/youtube_shorts.mp4 is the platform-correct 1080x1920 h264+aac MP4."""
    client = ctx["client"]
    job_id = ctx["video_job"]
    project_dir = ctx["video_dir"]
    out = project_dir / "output" / "youtube_shorts.mp4"
    if not out.is_file() or out.stat().st_size == 0:
        _fail(f"deliverable missing or empty: {out}")
    probe = ctx["FFmpeg"]().probe(out)
    streams = probe.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        _fail("no video stream in the deliverable")
    if video.get("codec_name") != "h264":
        _fail(f"video codec is {video.get('codec_name')!r}, expected h264")
    if (video.get("width"), video.get("height")) != (1080, 1920):
        _fail(f"resolution {video.get('width')}x{video.get('height')}, expected 1080x1920")
    if audio is None or audio.get("codec_name") != "aac":
        _fail(f"audio stream is {audio and audio.get('codec_name')!r}, expected aac")
    seconds = float(probe["format"]["duration"])
    expected = ctx["video_manifest"].total_duration_s
    if abs(seconds - expected) > 0.5:
        _fail(f"deliverable duration {seconds:.3f}s drifts from manifest {expected:.3f}s")
    listing = client.get(f"/api/jobs/{job_id}/deliverables").json()
    names = [item["name"] for item in listing["deliverables"]]
    if names != ["youtube_shorts.mp4"]:
        _fail(f"deliverables API lists {names}")
    served = client.get(f"/projects/{ctx['video_slug']}/output/youtube_shorts.mp4")
    if served.status_code != 200 or served.content != out.read_bytes():
        _fail(f"output file not served byte-identical (status {served.status_code})")
    return (
        f"h264 1080x1920 + aac, {seconds:.2f}s ~= manifest {expected:.2f}s,"
        f" {out.stat().st_size} bytes, served over HTTP"
    )


def check_run_image(ctx: dict[str, Any]) -> str:
    """Second project: image-format flow (instagram feed square) completes too."""
    client = ctx["client"]
    resp = client.post(
        "/input", data={"project": PROJECT_IMAGE, "script": SCRIPT}, follow_redirects=False
    )
    if resp.status_code != 303:
        _fail(f"POST /input (image project) -> {resp.status_code}")
    slug = ctx["slugify"](PROJECT_IMAGE)
    project_dir = ctx["projects_root"] / slug
    missing = [d for d in ctx["SUBDIRS"] if not (project_dir / d).is_dir()]
    if missing:
        _fail(f"projects/{slug}/ is missing subfolder(s) {missing}")
    ctx["image_slug"], ctx["image_dir"] = slug, project_dir
    created = client.post(
        "/options",
        data={**FORMAT_IMAGE, "audio": "on", "visual": "images", "dry_run": "on"},
        follow_redirects=False,
    )
    if created.status_code != 303 or not created.headers["location"].startswith("/confirm/"):
        _fail(
            f"POST /options (image) -> {created.status_code} {created.headers['location']!r}"
        )
    job_id = created.headers["location"].rsplit("/", 1)[1]
    if client.post(f"/jobs/{job_id}/confirm", follow_redirects=False).status_code != 303:
        _fail("image job confirm failed")
    payload = _wait_terminal(ctx, job_id)
    if payload["status"] != "completed":
        errors = [asset["error"] for asset in payload["assets"] if asset["error"]]
        _fail(f"image job ended {payload['status']!r}; asset errors: {errors}")
    if {asset["status"] for asset in payload["assets"]} != {"done"}:
        _fail("image job assets not all done")
    ctx["image_job"] = job_id
    return f"job {job_id} completed (instagram feed-square images); all assets done"


def check_deliverables_image(ctx: dict[str, Any]) -> str:
    """One JPEG per beat at the format's 1080x1080, plus a valid manifest."""
    client = ctx["client"]
    job_id = ctx["image_job"]
    project_dir = ctx["image_dir"]
    path = project_dir / "plan" / f"{job_id}{ctx['SUFFIX']}"
    manifest = ctx["read_manifest"](path)  # exists + valid section-6.2
    if len(manifest.beats) != N_BEATS or any(not beat.audio_file for beat in manifest.beats):
        _fail("image-job manifest wrong shape (3 audio-bearing beats expected)")
    expected_names = [f"instagram_feed_square_beat_{n:02d}.jpg" for n in range(1, N_BEATS + 1)]
    for name in expected_names:
        out = project_dir / "output" / name
        if not out.is_file() or out.stat().st_size == 0:
            _fail(f"missing image deliverable: {out}")
        probe = ctx["FFmpeg"]().probe(out)
        video = next(
            (s for s in probe.get("streams", []) if s.get("codec_type") == "video"), None
        )
        if video is None:
            _fail(f"{name}: no image stream")
        if video.get("codec_name") != "mjpeg":
            _fail(f"{name}: codec {video.get('codec_name')!r}, expected mjpeg (JPEG)")
        if (video.get("width"), video.get("height")) != (1080, 1080):
            _fail(f"{name}: {video.get('width')}x{video.get('height')}, expected 1080x1080")
    listing = client.get(f"/api/jobs/{job_id}/deliverables").json()
    names = [item["name"] for item in listing["deliverables"]]
    if names != expected_names:
        _fail(f"deliverables API lists {names}, expected {expected_names}")
    return f"3 JPEGs at 1080x1080 (mjpeg) + manifest valid ({len(manifest.beats)} beats)"


def check_audit(ctx: dict[str, Any]) -> str:
    """Persisted state: job.db, zero-cost spend log, clean scratch, silent guard."""
    guard: SocketGuard = ctx["guard"]
    if guard.violations:
        _fail(
            f"the flow made {len(guard.violations)} outbound network attempt(s):"
            f" {guard.violations[0].splitlines()[0]}"
        )
    lines_total = 0
    projects = (
        (ctx["video_slug"], ctx["video_dir"], ctx["video_job"]),
        (ctx["image_slug"], ctx["image_dir"], ctx["image_job"]),
    )
    for slug, project_dir, job_id in projects:
        if not (project_dir / "state" / "job.db").is_file():
            _fail(f"{slug}: state/job.db missing")
        log = project_dir / "state" / "spend_log.jsonl"
        if not log.is_file():
            _fail(f"{slug}: state/spend_log.jsonl missing")
        rows = [row for row in log.read_text(encoding="utf-8").splitlines() if row.strip()]
        if len(rows) != 2 * N_BEATS:
            _fail(f"{slug}: spend log has {len(rows)} entries, expected {2 * N_BEATS}")
        for row in rows:
            entry = json.loads(row)
            if entry["amount_usd"] != 0.0 or entry["provider_id"] != "mock":
                _fail(f"{slug}: non-mock spend entry: {entry}")
            if entry["job_id"] != job_id:
                _fail(f"{slug}: spend entry for foreign job {entry['job_id']!r}")
        scratch = project_dir / "state" / "scratch"
        if scratch.is_dir() and any(scratch.iterdir()):
            _fail(f"{slug}: stitch scratch was not cleaned: {list(scratch.iterdir())}")
        lines_total += len(rows)
        plumbing = len(guard.loopback)
    return (
        f"2 x job.db present; {lines_total} zero-cost spend entries;"
        f" scratch clean; {plumbing} harness IPC calls"
    )


SPECS: tuple[CheckSpec, ...] = (
    CheckSpec("environment", "python/ffmpeg/ffprobe + app import", check_environment),
    CheckSpec("boot-mock-only", "app boots; mock-only registry", check_boot, ("environment",)),
    CheckSpec(
        "no-httpx-in-app-code", "no httpx anywhere in app/", check_no_httpx, ("environment",)
    ),
    CheckSpec("zero-network-socket-guard", "socket guard live", check_guard, ("environment",)),
    CheckSpec(
        "input-creates-project",
        "project + section-7 tree + verbatim script",
        check_input,
        ("boot-mock-only",),
    ),
    CheckSpec(
        "review-beats", "segmentation + beat review edit", check_review,
        ("input-creates-project",),
    ),
    CheckSpec(
        "video-job-created", "options -> youtube/shorts dry-run job", check_options_video,
        ("review-beats",),
    ),
    CheckSpec(
        "dry-run-job-state", "pre-run state: 6 assets, mocks, $0", check_state_video,
        ("video-job-created",),
    ),
    CheckSpec(
        "video-job-completes", "confirm -> completed, all assets done", check_run_video,
        ("dry-run-job-state",),
    ),
    CheckSpec(
        "video-manifest-valid", "timing manifest passes the 6.2 reader", check_manifest_video,
        ("video-job-completes",),
    ),
    CheckSpec(
        "video-deliverable-dimensions", "1080x1920 h264+aac MP4 in output/",
        check_deliverable_video, ("video-manifest-valid",),
    ),
    CheckSpec(
        "image-job-completes", "image-format flow completes", check_run_image, ("review-beats",)
    ),
    CheckSpec(
        "image-deliverables", "3 JPEGs at 1080x1080 + valid manifest", check_deliverables_image,
        ("image-job-completes",),
    ),
    CheckSpec(
        "project-state-audit", "job.db, spend log, scratch, guard", check_audit,
        ("video-job-completes", "image-job-completes"),
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


def _print_report(results: list[Result], total_s: float) -> None:
    width = max(len(spec.name) for spec in SPECS)
    print()
    for i, result in enumerate(results, 1):
        line = (
            f"[{i:2d}/{len(results)}] {result.status:<4} {result.name:<{width}}"
            f" - {result.detail}"
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
    print(f"MILESTONE 1 SMOKE GATE: {'PASS' if failed == 0 else 'FAIL'}")


def main() -> int:
    warnings.filterwarnings("ignore")  # a gate reports its own checks, not deprecations
    print("=== ContentMaker - milestone 1 smoke gate (scripts/smoke_m1.py) ===")
    print("Gate scope: BUILD_PLAN section 3 - full HTTP flow on mock engines, offline.")
    started = time.monotonic()
    guard = SocketGuard()
    guard.install()
    ctx: dict[str, Any] = {"guard": guard}
    results: list[Result] = []
    workspace = Path(tempfile.mkdtemp(prefix="smoke_m1_"))
    ctx["projects_root"] = workspace / "projects"
    failed = False
    try:
        blocked: set[str] = set()
        for spec in SPECS:
            unmet = [dep for dep in spec.requires if dep in blocked]
            if unmet:
                blocked.add(spec.name)  # a skipped check also blocks its dependents
                results.append(
                    Result(spec.name, "SKIP", f"prerequisite '{unmet[0]}' did not pass")
                )
                continue
            started_at = time.monotonic()
            try:
                detail = spec.fn(ctx)
            except Exception as exc:  # noqa: BLE001 - a check failure IS the report
                blocked.add(spec.name)
                failed = True
                first_line = str(exc).splitlines()[0][:180]
                results.append(
                    Result(spec.name, "FAIL", first_line,
                           time.monotonic() - started_at, _tail(exc))
                )
            else:
                results.append(
                    Result(spec.name, "PASS", detail, time.monotonic() - started_at)
                )
    finally:
        closer = ctx.get("close_client")
        if closer is not None:
            try:
                closer(None, None, None)  # TestClient exit: lifespan shutdown
            except Exception:  # noqa: BLE001 - cleanup must not mask the report
                pass
        guard.restore()
        shutil.rmtree(workspace, ignore_errors=True)
    _print_report(results, time.monotonic() - started)
    note = "temp workspace removed" if not workspace.exists() else f"temp workspace {workspace}"
    print(f"({note}; run with .venv/Scripts/python.exe scripts/smoke_m1.py)")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
