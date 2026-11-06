"""App configuration: filesystem paths + server settings (BUILD_PLAN 1.9).

Single home for where ContentMaker puts things on disk (spec §7):
- ``projects/`` — one folder per project: script/ plan/ audio/ visuals/
  output/ state/ (the scaffolder in core.projects builds it);
- ``state/`` — app-level state, gitignored (keys.json lands here, task 2.1);
- ``app/data/`` — editable config tables (platforms.json, prices.json, §5).

Path helpers accept an optional ``base`` (a stand-in for PROJECT_ROOT) so
tests and callers relocate the whole tree without monkeypatching constants.
Server settings are plain data read from an env mapping at call time —
importing this module never binds machine-specific configuration.

Packaged builds (v1.0.1): the *writable* default base (``projects/``,
``state/``) is :func:`app_home` — the executable's own directory when running
frozen (``sys.frozen``), never the one-file extraction dir (``sys._MEIPASS``,
wiped on every run); ``CONTENTMAKER_HOME`` overrides both. In a frozen build
``Path(__file__)`` lives inside the extraction dir, which is exactly where the
read-only bundled content (``app/data``, ``app/web``) lands — so dev behavior
and bundled-data lookup stay unchanged.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
PROJECTS_DIRNAME = "projects"
APP_STATE_DIRNAME = "state"
PROJECT_SUBDIRS: tuple[str, ...] = ("script", "plan", "audio", "visuals", "output", "state")

# Local single-user app holding API keys (§6.5): never expose by default; an
# explicit CONTENTMAKER_HOST override is the user's own decision.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

# Portable/writable home override (v1.0.1 packaging) — wins above frozen mode
# and the repo root alike; see app_home().
CONTENTMAKER_HOME_ENV = "CONTENTMAKER_HOME"


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle (bootloader sets ``sys.frozen``)."""
    return bool(getattr(sys, "frozen", False))


def app_home(env: Mapping[str, str] | None = None) -> Path:
    """Writable home directory — where the default ``projects/`` and ``state/`` live.

    Resolution order: (1) the ``CONTENTMAKER_HOME`` env override, above both
    modes below; (2) frozen — the executable's own directory
    (``Path(sys.executable).parent``), NOT the one-file extraction dir; (3)
    otherwise the repo root (:data:`PROJECT_ROOT` — unchanged dev behavior).
    An explicit ``base=`` passed to the helpers below still wins over all of
    this; only the default lookup is resolved here.
    """
    source = os.environ if env is None else env
    home = source.get(CONTENTMAKER_HOME_ENV)
    if home:
        return Path(home).expanduser()
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return PROJECT_ROOT


def projects_root(base: Path | None = None) -> Path:
    """Root folder holding one subfolder per project: ``<base>/projects``."""
    return (base if base is not None else app_home()) / PROJECTS_DIRNAME


def app_state_dir(base: Path | None = None) -> Path:
    """App-level state folder ``<base>/state`` (keys.json, gitignored, §6.5)."""
    return (base if base is not None else app_home()) / APP_STATE_DIRNAME


def app_data_dir(base: Path | None = None) -> Path:
    """Editable config tables ``<base>/app/data`` (platforms/prices, §5)."""
    root = base if base is not None else PROJECT_ROOT
    return root / "app" / "data"


@dataclass(frozen=True)
class ServerSettings:
    """Where uvicorn should listen; ``from_env`` reads CONTENTMAKER_* keys."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ServerSettings:
        source = os.environ if env is None else env
        host = source.get("CONTENTMAKER_HOST", DEFAULT_HOST)
        port = DEFAULT_PORT
        raw_port = source.get("CONTENTMAKER_PORT")
        if raw_port is not None:
            try:
                port = int(raw_port)
            except ValueError:
                raise ValueError(
                    f"CONTENTMAKER_PORT must be an integer (got {raw_port!r})"
                ) from None
            if not 1 <= port <= 65535:
                raise ValueError(f"CONTENTMAKER_PORT must be 1-65535 (got {port})")
        return cls(host=host, port=port)
