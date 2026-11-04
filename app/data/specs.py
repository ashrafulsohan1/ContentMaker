"""Platform/format catalog loader (spec §5).

The catalog lives in platforms.json as editable configuration data — adding
a platform or format never touches code. Values are current platform
guidance; adjust the JSON as platforms change.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent
PLATFORMS_FILE = DATA_DIR / "platforms.json"

_VALID_KINDS = {"video", "image", "carousel"}


@dataclass(frozen=True)
class FormatSpec:
    id: str
    label: str
    kind: str  # "video" | "image" | "carousel"
    aspect: tuple[int, int]
    max_duration_s: int | None
    resolution: tuple[int, int]
    container: str  # "mp4" | "jpg" | "png"
    min_images: int = 1
    max_images: int = 1


@dataclass(frozen=True)
class PlatformSpec:
    id: str
    name: str
    formats: tuple[FormatSpec, ...]


def platforms_path() -> Path:
    return PLATFORMS_FILE


def _pair(value: list[int]) -> tuple[int, int]:
    w, h = int(value[0]), int(value[1])
    if w <= 0 or h <= 0:
        raise ValueError(f"aspect/resolution must be positive, got {value!r}")
    return (w, h)


def _format(raw: dict) -> FormatSpec:
    kind = raw["kind"]
    if kind not in _VALID_KINDS:
        raise ValueError(f"format {raw['id']!r}: unknown kind {kind!r}")
    max_dur = raw.get("max_duration_s")
    return FormatSpec(
        id=raw["id"],
        label=raw["label"],
        kind=kind,
        aspect=_pair(raw["aspect"]),
        max_duration_s=None if max_dur is None else int(max_dur),
        resolution=_pair(raw["resolution"]),
        container=raw["container"],
        min_images=int(raw.get("min_images", 1)),
        max_images=int(raw.get("max_images", 1)),
    )


def load_platforms(path: Path | None = None) -> list[PlatformSpec]:
    file = path if path is not None else PLATFORMS_FILE
    payload = json.loads(file.read_text(encoding="utf-8"))
    platforms = [
        PlatformSpec(
            id=p["id"],
            name=p["name"],
            formats=tuple(_format(f) for f in p["formats"]),
        )
        for p in payload["platforms"]
    ]
    return platforms


def lookup_format(
    platform_id: str, format_id: str, platforms: list[PlatformSpec] | None = None
) -> FormatSpec:
    for platform in platforms if platforms is not None else load_platforms():
        if platform.id == platform_id:
            for fmt in platform.formats:
                if fmt.id == format_id:
                    return fmt
            raise ValueError(f"unknown format '{format_id}' on platform '{platform_id}'")
    raise ValueError(f"unknown platform '{platform_id}'")
