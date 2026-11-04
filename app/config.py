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
"""
from __future__ import annotations

import os
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


def projects_root(base: Path | None = None) -> Path:
    """Root folder holding one subfolder per project: ``<base>/projects``."""
    return (base if base is not None else PROJECT_ROOT) / PROJECTS_DIRNAME


def app_state_dir(base: Path | None = None) -> Path:
    """App-level state folder ``<base>/state`` (keys.json, gitignored, §6.5)."""
    return (base if base is not None else PROJECT_ROOT) / APP_STATE_DIRNAME


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
