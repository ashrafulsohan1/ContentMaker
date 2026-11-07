#!/usr/bin/env python
"""Smoke gate for the packaged ContentMaker.exe (v1.0.1, BUILD_PLAN §12).

Runs the REAL built exe the way a user would, and reports a per-check result:

    1. EXE PRESENT       dist/ContentMaker.exe exists (scripts/build_exe.py output);
    2. LAUNCH            the exe starts with --no-browser --port 8799 under a
                         scratch CONTENTMAKER_HOME (temp dir) so nothing is ever
                         written next to the exe — CONTENTMAKER_FFMPEG_DIR is
                         cleared from the child env so the resolver's bundled
                         tier is the one that must answer;
    3. HEALTHZ READY     GET /healthz answers 200 within the boot timeout
                         (one-file extraction + uvicorn bind);
    4. FROZEN            /healthz reports frozen == true;
    5/6. BUNDLED BINARIES /healthz resolves ffmpeg/ffprobe to the BUNDLED
                         ffmpeg_bin paths — tier 2 of the resolver
                         (app/core/binaries.py): ``<_MEI…>/ffmpeg_bin/<tool>.exe``,
                         i.e. inside the one-file extraction dir, NOT PATH and
                         NOT a CONTENTMAKER_FFMPEG_DIR override;
    7. UI ROOT           GET / answers 200;
    8. CLEAN EXIT        the process stops on a graceful console interrupt
                         (CTRL_BREAK → uvicorn's clean shutdown) within the
                         timeout — no terminate()/kill() escalation, uvicorn's
                         shutdown sequence is in the server log and the port is
                         freed afterwards. The one-file bootloader PARENT is
                         terminated by the CRT's default Ctrl+Break disposition
                         (exit code 3) while the server child exits cleanly —
                         so the parent's raw exit code is reported, not asserted;
    9. SCRATCH HOME      the exe wrote nothing next to itself (no projects/ or
                         state/ appear in dist/) — CONTENTMAKER_HOME was honored;
    10. COLLISION        v1.0.2 port-collision fallback, end-to-end: with the
                         launcher's DEFAULT port 8765 squatted by a plain socket
                         BEFORE launch, the exe is started WITHOUT --port and
                         must shift to the first free port upward (8766) instead
                         of dying on bind — /healthz answers 200 with frozen
                         JSON on 8766 within the boot timeout, the console
                         notice "Port 8765 in use — using 8766" is printed, and
                         the fallback server stops cleanly afterwards.

Usage:   .venv/Scripts/python.exe scripts/smoke_exe.py
Exit:    0 — every check passed; 1 — at least one check failed.

The scratch CONTENTMAKER_HOME dir is removed on success and kept (its path is
reported, the server log inside it) on failure for diagnosis.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "dist" / "ContentMaker.exe"
BASE_URL = "http://127.0.0.1:8799"
PORT = 8799
# v1.0.2 port-collision check: the launcher's DEFAULT port (app.config
# DEFAULT_PORT = 8765), squatted by a plain socket before the exe starts, and
# the first fallback port upward the launcher must shift to (pick_port scans
# requested..requested+30, app/launcher.py).
COLLISION_PORT = 8765
FALLBACK_PORT = 8766
BOOT_TIMEOUT_S = 120.0  # one-file extraction + uvicorn bind (cold AV scans can be slow)
POLL_S = 0.5
SHUTDOWN_TIMEOUT_S = 15.0
LOG_NAME = "server.log"


@dataclass
class Result:
    name: str
    status: str  # PASS | FAIL | SKIP
    detail: str = ""
    tail: str = ""


def _fail(message: str) -> None:
    raise AssertionError(message)


def _healthz(timeout_s: float = 5.0, port: int = PORT) -> tuple[int, dict | None]:
    """GET /healthz -> (status, payload-or-None); network errors are poll noise."""
    request = urllib.request.Request(f"http://127.0.0.1:{port}/healthz", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return 0, None


def _bundled_check(tool: str, resolved: object) -> None:
    """Tier 2 of the 3-tier resolver (app/core/binaries.py): the path must be
    the staged binary inside the one-file extraction dir — parent dir
    ``ffmpeg_bin`` under a ``_MEI…`` dir, and the file must exist on disk."""
    if not isinstance(resolved, str) or not resolved:
        _fail(f"/healthz must report the resolved {tool} path, got {resolved!r}")
    path = Path(resolved)
    if path.name != f"{tool}.exe":
        _fail(f"{tool} must resolve to the Windows exe, got {resolved!r}")
    if path.parent.name != "ffmpeg_bin":
        _fail(
            f"{tool} must resolve into the bundled ffmpeg_bin/ dir (tier 2), got {resolved!r}"
        )
    if not path.parent.parent.name.startswith("_MEI"):
        _fail(
            f"{tool} must resolve inside the one-file extraction dir (_MEI…), got {resolved!r}"
        )
    if not path.is_file():
        _fail(f"resolved {tool} does not exist on disk: {resolved!r}")


def check_exe_present(ctx: dict) -> str:
    if not EXE.is_file():
        _fail(
            f"{EXE} not found — build it first: "
            f".venv/Scripts/python.exe scripts/build_exe.py --bundle-ffmpeg"
        )
    return f"{EXE.name} present ({EXE.stat().st_size / 1e6:.1f} MB)"


def check_launch(ctx: dict) -> str:
    if sys.platform == "win32" and not _port_free(PORT):
        _fail(f"port {PORT} is already in use — the smoke needs a free 127.0.0.1:{PORT}")
    scratch = Path(tempfile.mkdtemp(prefix="smoke_exe_"))
    env = dict(os.environ)
    env["CONTENTMAKER_HOME"] = str(scratch)  # scratch home: nothing lands next to the exe
    env.pop("CONTENTMAKER_FFMPEG_DIR", None)  # clear the override tier: bundled tier must win
    ctx["scratch"] = scratch
    log_path = scratch / LOG_NAME
    log = log_path.open("w", encoding="utf-8", buffering=1)
    try:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        ctx["proc"] = subprocess.Popen(
            [str(EXE), "--no-browser", "--port", str(PORT)],
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=flags,
        )
    except OSError as exc:
        log.close()
        shutil.rmtree(scratch, ignore_errors=True)
        _fail(f"failed to start {EXE}: {exc}")
    ctx["log_path"] = log_path
    ctx["log_handle"] = log
    return (
        f"pid {ctx['proc'].pid}; CONTENTMAKER_HOME={scratch}"
        " (scratch dir, CONTENTMAKER_FFMPEG_DIR cleared)"
    )


def check_healthz_ready(ctx: dict) -> str:
    deadline = time.monotonic() + BOOT_TIMEOUT_S
    status, payload = 0, None
    while time.monotonic() < deadline:
        status, payload = _healthz()
        if status == 200:
            ctx["healthz"] = payload
            waited = BOOT_TIMEOUT_S - (deadline - time.monotonic())
            return f"GET /healthz -> 200 after {waited:.1f}s"
        if ctx["proc"].poll() is not None:
            _fail(
                f"the exe exited early (code {ctx['proc'].returncode}) "
                f"before answering /healthz"
            )
        time.sleep(POLL_S)
    _fail(f"GET /healthz never answered 200 within {BOOT_TIMEOUT_S:.0f}s (last status {status})")


def check_frozen(ctx: dict) -> str:
    payload = ctx["healthz"]
    if payload.get("frozen") is not True:
        _fail(f"/healthz must report frozen=true for the packaged exe, got {payload!r}")
    return "frozen == true"


def check_bundled_ffmpeg(ctx: dict) -> str:
    payload = ctx["healthz"]
    _bundled_check("ffmpeg", payload.get("ffmpeg"))
    return f"ffmpeg -> {payload['ffmpeg']}"


def check_bundled_ffprobe(ctx: dict) -> str:
    payload = ctx["healthz"]
    _bundled_check("ffprobe", payload.get("ffprobe"))
    return f"ffprobe -> {payload['ffprobe']}"


def check_ui_root(ctx: dict) -> str:
    request = urllib.request.Request(f"{BASE_URL}/", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=10.0) as resp:
            status = resp.status
            body = resp.read()
    except urllib.error.URLError as exc:
        _fail(f"GET / failed: {exc}")
    if status != 200:
        _fail(f"GET / -> {status}, expected 200")
    if not body:
        _fail("GET / returned an empty body")
    return f"GET / -> 200 ({len(body)} bytes)"


def check_clean_exit(ctx: dict) -> str:
    """Clean stop = the graceful console interrupt is accepted (no terminate/kill
    escalation), uvicorn's own shutdown sequence lands in the server log, and the
    port is freed. The PyInstaller one-file PARENT bootloader is terminated by
    the CRT's default Ctrl+Break disposition (exit code 3) while the uvicorn
    child handles SIGBREAK gracefully (uvicorn/server.py HANDLED_SIGNALS) — so
    the parent's raw exit code is reported, not asserted."""
    proc: subprocess.Popen = ctx["proc"]
    escalated: str | None = None
    if sys.platform == "win32":
        try:
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)  # graceful console interrupt
        except (OSError, ValueError) as exc:
            escalated = f"CTRL_BREAK_EVENT failed ({exc}); falling back to terminate()"
    else:
        escalated = "non-Windows host: terminate() (no console interrupt)"
    try:
        proc.wait(timeout=SHUTDOWN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        if escalated is None:
            escalated = f"no exit within {SHUTDOWN_TIMEOUT_S:.0f}s of CTRL_BREAK"
        proc.terminate()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5.0)
            escalated += "; terminate() ignored, kill() applied"
        else:
            escalated += "; terminate() applied"
        _fail(f"termination was NOT clean: {escalated}")
    finally:
        ctx["log_handle"].close()
    log_text = ctx["log_path"].read_text(encoding="utf-8", errors="replace")
    for marker in ("Application shutdown complete", "Finished server process"):
        if marker not in log_text:
            _fail(f"the server log lacks uvicorn's clean-shutdown line {marker!r}")
    deadline = time.monotonic() + 10.0
    while not _port_free(PORT):
        if time.monotonic() > deadline:
            _fail(f"port {PORT} is still bound after the process exited (orphaned server?)")
        time.sleep(0.5)
    note = f"; {escalated}" if escalated else ""
    return (
        f"stopped cleanly: uvicorn shutdown logged, port {PORT} freed, no kill escalation"
        f" (bootloader parent exit {proc.returncode} = CRT default for Ctrl+Break){note}"
    )


def check_scratch_home(ctx: dict) -> str:
    """CONTENTMAKER_HOME must be the writable home: nothing next to the exe.
    (Writable content is created lazily — the assertion is on dist/ staying clean.)"""
    for name in ("projects", "state"):
        stray = EXE.parent / name
        if stray.exists():
            _fail(f"the exe wrote next to itself despite scratch CONTENTMAKER_HOME: {stray}")
    return f"nothing written next to the exe; scratch CONTENTMAKER_HOME was {ctx['scratch']}"


def check_collision_fallback(ctx: dict) -> str:
    """v1.0.2 port-collision fallback, end-to-end on the real exe: BEFORE launch
    a plain socket squats the launcher's DEFAULT port 8765, the exe is started
    WITHOUT --port, so it must shift to the first free port upward (8766)
    instead of dying on bind — /healthz on 8766 answers 200 with a JSON payload
    carrying frozen=true within the boot timeout, and the console notice
    "Port 8765 in use — using 8766" is printed. Self-contained: own process,
    own scratch CONTENTMAKER_HOME, own cleanup (squatter socket closed, process
    stopped, fallback port freed, scratch removed) whatever the outcome — a
    failure message embeds the collision server log tail, since the report's
    _log_tail covers only the primary instance's log."""
    if not _port_free(FALLBACK_PORT):
        _fail(
            f"port {FALLBACK_PORT} is already in use — the collision check needs "
            f"a free {FALLBACK_PORT} to prove the {COLLISION_PORT}->{FALLBACK_PORT} fallback"
        )
    squatter = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    scratch = Path(tempfile.mkdtemp(prefix="smoke_collision_"))
    log_path = scratch / LOG_NAME
    log = log_path.open("w", encoding="utf-8", buffering=1)
    proc: subprocess.Popen | None = None
    try:
        squatter.bind(("127.0.0.1", COLLISION_PORT))
        squatter.listen(1)
        env = dict(os.environ)
        env["CONTENTMAKER_HOME"] = str(scratch)  # scratch home here too
        env.pop("CONTENTMAKER_FFMPEG_DIR", None)
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        proc = subprocess.Popen(
            [str(EXE), "--no-browser"],  # NO --port: the DEFAULT 8765 must collide
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=flags,
        )
        status, payload = 0, None
        deadline = time.monotonic() + BOOT_TIMEOUT_S
        while time.monotonic() < deadline:
            status, payload = _healthz(port=FALLBACK_PORT)
            if status == 200 and isinstance(payload, dict):
                break
            if proc.poll() is not None:
                _fail(
                    f"the exe exited early (code {proc.returncode}) while "
                    f"{COLLISION_PORT} was squatted — it must fall back to "
                    f"{FALLBACK_PORT}, not die on bind" + _log_tail(log_path)
                )
            time.sleep(POLL_S)
        if status != 200 or not isinstance(payload, dict):
            _fail(
                f"GET /healthz never answered 200 JSON on the fallback port "
                f"{FALLBACK_PORT} within {BOOT_TIMEOUT_S:.0f}s while "
                f"{COLLISION_PORT} was squatted (last status {status})"
                + _log_tail(log_path)
            )
        if payload.get("frozen") is not True:
            _fail(
                f"/healthz on the fallback port {FALLBACK_PORT} must report "
                f"frozen=true, got {payload!r}"
            )
        # Graceful stop before reading the console notice: the exe's stdout is
        # block-buffered when redirected to a file, so its early prints (the
        # fallback notice) only flush when the process exits cleanly.
        _stop_proc(proc)
        deadline = time.monotonic() + 10.0
        # ASCII-only match: the exe's redirected stdout uses the ANSI code page
        # (launcher.py's em dash lands as cp1252 0x97, i.e. U+FFFD after the
        # errors="replace" read), so an exact UTF-8 match would false-fail.
        # "Port 8765 in use ... using 8766" appears on no other line.
        notice = f"Port {COLLISION_PORT} in use — using {FALLBACK_PORT}"
        pattern = re.compile(
            rf"Port {COLLISION_PORT} in use.{{0,3}}using {FALLBACK_PORT}"
        )
        while True:
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
            if pattern.search(log_text):
                break
            if time.monotonic() > deadline:
                _fail(
                    f"the launcher did not print the fallback notice {notice!r}"
                    + _log_tail(log_path)
                )
            time.sleep(0.5)
        deadline = time.monotonic() + 10.0
        while not _port_free(FALLBACK_PORT):
            if time.monotonic() > deadline:
                _fail(
                    f"fallback port {FALLBACK_PORT} is still bound after the "
                    f"process exited (orphaned server?)"
                )
            time.sleep(0.5)
        return (
            f"{COLLISION_PORT} squatted before launch, exe started without --port "
            f"-> fell back to {FALLBACK_PORT}: /healthz 200 frozen=true, console "
            f"notice {notice!r}, fallback server stopped cleanly"
        )
    finally:
        if proc is not None:
            _stop_proc(proc)
        log.close()
        squatter.close()
        shutil.rmtree(scratch, ignore_errors=True)


CHECKS: tuple[tuple[str, Callable[[dict], str]], ...] = (
    ("exe-present", check_exe_present),
    ("launch-scratch-home", check_launch),
    ("healthz-ready", check_healthz_ready),
    ("frozen-flag", check_frozen),
    ("bundled-ffmpeg", check_bundled_ffmpeg),
    ("bundled-ffprobe", check_bundled_ffprobe),
    ("ui-root", check_ui_root),
    ("clean-exit", check_clean_exit),
    ("scratch-home-honored", check_scratch_home),
    ("collision-fallback", check_collision_fallback),
)


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _stop_proc(proc: subprocess.Popen) -> None:
    """Stop a smoke-launched exe for good: graceful console interrupt first
    (CREATE_NEW_PROCESS_GROUP put it in its own group, so CTRL_BREAK reaches
    the one-file parent AND the uvicorn child), then terminate()/kill()
    escalation. Never leaves a server behind on any code path."""
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
        except (OSError, ValueError):
            pass  # fall through to terminate() below
    try:
        proc.wait(timeout=SHUTDOWN_TIMEOUT_S)
        return
    except subprocess.TimeoutExpired:
        pass
    proc.terminate()
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5.0)


def _log_tail(log_path: Path, lines: int = 15) -> str:
    try:
        rows = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    tail = "\n".join(f"    | {row}" for row in rows[-lines:] if row.strip())
    return f"\n  server log tail ({log_path}):\n{tail}" if tail else ""


def _print_report(results: list[Result], total_s: float) -> None:
    width = max(len(name) for name, _ in CHECKS)
    print()
    for i, result in enumerate(results, 1):
        line = f"[{i}/{len(results)}] {result.status:<4} {result.name:<{width}} - {result.detail}"
        print(f"{line} ({total_s:.1f}s total)" if result.status == "PASS" else line)
        if result.tail:
            print(result.tail)
    passed = sum(1 for r in results if r.status == "PASS")
    failed = sum(1 for r in results if r.status == "FAIL")
    skipped = sum(1 for r in results if r.status == "SKIP")
    print("-" * 96)
    print(f"{len(results)} checks: {passed} passed, {failed} failed, {skipped} skipped")
    print(f"EXE SMOKE GATE: {'PASS' if failed == 0 else 'FAIL'}")


def main() -> int:
    print("=== ContentMaker - packaged exe smoke gate (scripts/smoke_exe.py) ===")
    print(f"Target: {EXE} on {BASE_URL} (BUILD_PLAN §12: one-file build, bundled ffmpeg)")
    started = time.monotonic()
    results: list[Result] = []
    ctx: dict = {}
    blocked = False
    scratch: Path | None = None
    try:
        for name, check in CHECKS:
            if blocked:
                results.append(Result(name, "SKIP", "an earlier check did not pass"))
                continue
            try:
                detail = check(ctx)
            except Exception as exc:  # noqa: BLE001 - a check failure IS the report
                blocked = True
                tail = _log_tail(ctx["log_path"]) if ctx.get("log_path") else ""
                results.append(Result(name, "FAIL", str(exc).splitlines()[0][:220], tail))
            else:
                results.append(Result(name, "PASS", detail))
    finally:
        proc: subprocess.Popen | None = ctx.get("proc")
        if proc is not None and proc.poll() is None:  # never leave a server behind
            proc.terminate()
            try:
                proc.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                proc.kill()
            print("NOTE: the exe was still running after the checks — force-stopped.")
        if ctx.get("log_handle"):
            ctx["log_handle"].close()
        scratch = ctx.get("scratch")
        if scratch is not None and not any(r.status == "FAIL" for r in results):
            shutil.rmtree(scratch, ignore_errors=True)
    _print_report(results, time.monotonic() - started)
    if scratch is not None:
        if any(r.status == "FAIL" for r in results):
            print(f"(scratch CONTENTMAKER_HOME kept for diagnosis: {scratch})")
        elif scratch.exists():
            print(f"NOTE: scratch CONTENTMAKER_HOME not removed: {scratch}")
    return 0 if not any(r.status in ("FAIL",) for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
