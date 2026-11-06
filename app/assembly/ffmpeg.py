"""ffmpeg/ffprobe subprocess wrapper (BUILD_PLAN 1.8).

Assembly (1.12) and manifest duration probing (2.4) go through this module,
never through raw subprocess calls, so every ffmpeg/ffprobe invocation gets:

- **Absolute Windows-safe paths** (risk #3): every ``Path`` argument is
  resolved to an absolute path before the child is spawned — no invocation
  depends on the current working directory, and spaces/CJK in OneDrive-style
  paths survive (args pass through a plain list with ``shell=False``, so no
  manual quoting is ever needed).
- **One loud failure type**: missing binary, nonzero exit, timeout, and
  unparsable probe output all raise :class:`FFmpegError` carrying the exit
  code and the stderr tail — the message names what actually went wrong
  (§6.5 spirit), nothing fails silently.
- **A timeout** (default 300 s) so a hung encode can never hang the job
  worker thread (§6.6).

``-nostdin`` is forced on every ``run``: interactive prompts reading the
parent's stdin would hang the background worker. Pass ``-y`` yourself to
overwrite outputs; the wrapper owns no semantic flags.

Binary resolution (v1.0.1): default ``ffmpeg_bin``/``ffprobe_bin`` come from
the 3-tier resolver :mod:`app.core.binaries` (``CONTENTMAKER_FFMPEG_DIR`` env
dir → bundled ``ffmpeg_bin`` → PATH) at instantiation time; nothing resolving
raises :class:`~app.core.binaries.PackagingError` naming all three tiers.
Explicit bins still win — pass ``ffmpeg_bin=``/``ffprobe_bin=`` to pin.

Durations are probed, never trusted (BUILD_PLAN §1.2 "Durations"):
``probe_duration`` returns raw ffprobe seconds as a float — callers round to
whole milliseconds when writing the timing manifest (tasks 1.7/2.4).
"""
from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.binaries import resolve_ffmpeg, resolve_ffprobe

_DEFAULT_TIMEOUT_S = 300.0
_STDERR_TAIL_LINES = 8


class FFmpegError(RuntimeError):
    """An ffmpeg/ffprobe invocation failed.

    ``returncode`` is the child's exit code (``None`` when the binary never
    spawned or timed out); ``stderr_tail`` carries the last stderr lines so
    failures say exactly what ffmpeg objected to.
    """

    def __init__(
        self, message: str, *, returncode: int | None = None, stderr_tail: str = ""
    ) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr_tail = stderr_tail


@dataclass(frozen=True)
class FFmpeg:
    """Thin wrapper around the ffmpeg/ffprobe system binaries.

    ``run`` takes a plain argument sequence: ``Path`` objects are resolved to
    absolute paths (cwd-independent, Windows-safe); ``str`` arguments pass
    through untouched — pass ``Path`` for anything path-like. The wrapper
    never touches directories; callers create output directories.
    """

    ffmpeg_bin: str = field(default_factory=resolve_ffmpeg)
    ffprobe_bin: str = field(default_factory=resolve_ffprobe)
    timeout_s: float = _DEFAULT_TIMEOUT_S

    def run(self, args: Sequence[str | Path]) -> subprocess.CompletedProcess[str]:
        """Run ffmpeg with ``-nostdin`` forced; returns the completed process.

        Raises :class:`FFmpegError` on a missing binary, a timeout, or a
        nonzero exit (with the stderr tail in the message).
        """
        argv = [self.ffmpeg_bin, "-nostdin", *(_abs(arg) for arg in args)]
        proc = self._spawn(argv)
        if proc.returncode != 0:
            tail = _stderr_tail(proc.stderr)
            raise FFmpegError(
                f"ffmpeg failed (exit {proc.returncode}): {tail}",
                returncode=proc.returncode,
                stderr_tail=tail,
            )
        return proc

    def probe(self, path: str | Path) -> dict[str, Any]:
        """ffprobe a media file; returns the parsed JSON (``format`` + ``streams``)."""
        file = _abs(path)
        argv = [
            self.ffprobe_bin,
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            file,
        ]
        proc = self._spawn(argv)
        if proc.returncode != 0:
            tail = _stderr_tail(proc.stderr)
            raise FFmpegError(
                f"ffprobe failed for {file} (exit {proc.returncode}): {tail}",
                returncode=proc.returncode,
                stderr_tail=tail,
            )
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise FFmpegError(f"ffprobe returned invalid JSON for {file}: {exc}") from exc
        if not isinstance(data, dict):
            raise FFmpegError(f"ffprobe returned unexpected JSON for {file}: {proc.stdout[:200]!r}")
        return data

    def probe_duration(self, path: str | Path) -> float:
        """Probed duration in seconds — the authoritative value for the manifest.

        Returns the raw ffprobe float; callers round to whole milliseconds
        (tasks 1.7/2.4). Raises :class:`FFmpegError` when the file has no
        duration (images and other non-timeline files) or the reported value
        is not a number.
        """
        file = _abs(path)
        fmt = self.probe(file).get("format")
        raw = fmt.get("duration") if isinstance(fmt, dict) else None
        if raw is None:
            raise FFmpegError(
                f"{file}: ffprobe reports no duration (format.duration missing) —"
                " not an audio/video file?"
            )
        try:
            return float(raw)
        except (TypeError, ValueError):
            raise FFmpegError(f"{file}: ffprobe duration {raw!r} is not a number") from None

    def _spawn(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise FFmpegError(
                f"{argv[0]!r} timed out after {self.timeout_s:g}s: {_argv_echo(argv)}",
                stderr_tail=_stderr_tail(exc.stderr),
            ) from exc
        except OSError as exc:
            raise FFmpegError(
                f"cannot run {argv[0]!r} — resolved via CONTENTMAKER_FFMPEG_DIR /"
                " bundled ffmpeg_bin / PATH but failed to launch (see /healthz):"
                f" {exc}"
            ) from exc


def probe_duration(path: str | Path) -> float:
    """Module-level shortcut for :meth:`FFmpeg.probe_duration` (task 2.4 wiring)."""
    return FFmpeg().probe_duration(path)


def _abs(value: str | Path) -> str:
    """Absolute path for Path args (cwd-independent, Windows-safe); str passes through."""
    if isinstance(value, Path):
        return str(value.expanduser().resolve())
    return str(value)


def _stderr_tail(text: str | bytes | None) -> str:
    """Last non-empty stderr lines, joined single-line for error/log friendliness."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return " | ".join(lines[-_STDERR_TAIL_LINES:])


def _argv_echo(argv: list[str]) -> str:
    """Bounded command echo for timeout messages."""
    return " ".join(argv)[:200]
