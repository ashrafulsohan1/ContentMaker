"""Platform-correct PNG/JPEG image export (BUILD_PLAN 1.12; spec §6.4).

Every exported image lands at the **exact** platform resolution of its
format (app/data/platforms.json): the source is scaled down/up with aspect
preserved (``force_original_aspect_ratio=decrease``) and padded onto a black
canvas to the target size — never cropped, never stretched. The container
decides the encoder: ``jpg`` → mjpeg, ``png`` → png.

An optional per-image text overlay uses ffmpeg ``drawtext`` (the ruling from
task 1.4 moved mock-image labels here). The overlay text is passed through a
``textfile`` (UTF-8, i18n-friendly) instead of the filter string, so user text
can never break filter escaping; the font comes from :func:`find_font`
(``CONTENTMAKER_FONT`` env override first, then well-known system fonts). A
requested overlay with no usable font is a loud
:class:`~app.assembly.AssemblyError` naming the env var — never a silent
unlabeled export.

Windows-path note: inside a filter string, a drive-colon path must be
single-quoted **and** its colon escaped (``fontfile='C\\:/...'``); the
pre-flight smoke on ffmpeg 8 confirmed that form (a bare quoted path does not
parse). All media I/O goes through :class:`app.assembly.ffmpeg.FFmpeg`.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from app.assembly import AssemblyError
from app.assembly.ffmpeg import FFmpeg, FFmpegError

_FONT_ENV_VAR = "CONTENTMAKER_FONT"
VALID_CONTAINERS = ("png", "jpg", "jpeg")
_JPEG_QUALITY = "2"  # qscale 1–31, 2 ≈ visually lossless for social delivery

# Well-known system fonts, first match wins (Windows is the target platform
# per spec §7; the POSIX/macOS entries keep dev machines working too).
_FONT_CANDIDATES: tuple[Path, ...] = (
    Path("C:/Windows/Fonts/arial.ttf"),
    Path("C:/Windows/Fonts/calibri.ttf"),
    Path("C:/Windows/Fonts/segoeui.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/dejavu/DejaVuSans.ttf"),
    Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
)


def find_font() -> Path | None:
    """A usable .ttf/.otf font path, or None when the system offers none.

    ``CONTENTMAKER_FONT`` (a font file path) wins when set; pointing it at a
    missing file yields None so the overlay error names the env var.
    """
    raw = os.environ.get(_FONT_ENV_VAR)
    if raw:
        path = Path(raw)
        return path if path.is_file() else None
    return next((candidate for candidate in _FONT_CANDIDATES if candidate.is_file()), None)


def export_image(
    src: str | Path,
    out_path: str | Path,
    *,
    width: int,
    height: int,
    container: str,
    overlay_text: str | None = None,
    ffmpeg: FFmpeg | None = None,
) -> Path:
    """Export ``src`` at exactly ``width``×``height`` in ``container``; returns out_path.

    ``container`` is ``png`` or ``jpg``/``jpeg`` (the platform-catalog
    vocabulary, app/data/platforms.json). ``overlay_text`` draws the given text
    near the bottom on a translucent box. Raises
    :class:`~app.assembly.AssemblyError` for an unknown container or a
    requested overlay with no font available.
    """
    runner = ffmpeg if ffmpeg is not None else FFmpeg()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    codec_args = _container_codec(container)
    filters = [
        f"scale={width}:{height}:force_original_aspect_ratio=decrease",
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black",
        "setsar=1",
    ]
    textfile: Path | None = None
    try:
        if overlay_text:
            font = find_font()
            if font is None:
                raise AssemblyError(
                    "text overlay requested but no usable font was found — install a"
                    f" system font or set {_FONT_ENV_VAR} to a .ttf/.otf path"
                )
            textfile = _write_textfile(overlay_text, out_path)
            filters.append(_drawtext_filter(font, textfile, height))
        try:
            runner.run([
                "-i", Path(src),
                "-vf", ",".join(filters),
                *codec_args,
                "-y", out_path,
            ])
        except FFmpegError as exc:  # the documented failure type of this module
            raise AssemblyError(f"image export failed for {Path(src).name}: {exc}") from exc
    finally:
        if textfile is not None:
            textfile.unlink(missing_ok=True)
    return out_path


def _container_codec(container: str) -> list[str]:
    normalized = container.strip().lower()
    if normalized == "png":
        return ["-c:v", "png"]
    if normalized in ("jpg", "jpeg"):
        return ["-c:v", "mjpeg", "-q:v", _JPEG_QUALITY]
    raise AssemblyError(
        f"unknown image container {container!r} — platforms.json supports png and jpg"
    )


def _drawtext_filter(font: Path, textfile: Path, image_height: int) -> str:
    fontsize = max(24, image_height // 20)
    margin = max(16, image_height // 24)
    return (
        f"drawtext=fontfile={_filter_path(font)}:textfile={_filter_path(textfile)}"
        f":fontsize={fontsize}:fontcolor=white"
        f":box=1:boxcolor=black@0.55:boxborderw={max(8, margin // 2)}"
        f":x=(w-text_w)/2:y=h-th-{margin}"
    )


def _filter_path(path: Path) -> str:
    """Quote a Windows/POSIX path for use inside an ffmpeg filter string.

    Single quotes keep the drive colon from splitting the option value, and
    the colon is additionally backslash-escaped (both levels are needed —
    verified against ffmpeg 8: a quoted path without the escape fails to
    parse).
    """
    return "'" + path.resolve().as_posix().replace(":", "\\:") + "'"


def _write_textfile(text: str, out_path: Path) -> Path:
    """UTF-8 sidecar for drawtext's textfile= option (keeps user text out of
    filter-escaping entirely); written next to the output, removed by the caller."""
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", delete=False,
        prefix=f".{out_path.stem}.", suffix=".overlay.txt",
        dir=str(out_path.parent),
    )
    with handle:
        handle.write(text)
    return Path(handle.name)
