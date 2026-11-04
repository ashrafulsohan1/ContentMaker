"""Seams: app.config — filesystem paths + server settings (BUILD_PLAN 1.9).

config.py is the single home for where ContentMaker puts things on disk
(spec §7): the per-project ``projects/`` tree, the app-level gitignored
``state/`` folder (keys.json lands there in task 2.1), and the editable
``app/data/`` config tables (§5). Path helpers accept an optional base so
tests and callers relocate the tree without monkeypatching constants, and
server settings are read from an env mapping at call time — importing the
module never binds machine-specific configuration.
"""
from pathlib import Path

import pytest

from app import config


def test_project_root_is_the_repo_root():
    assert config.PROJECT_ROOT == Path(__file__).resolve().parents[1]
    assert (config.PROJECT_ROOT / "app" / "config.py").is_file()


def test_projects_root_default_and_override(tmp_path: Path):
    assert config.projects_root() == config.PROJECT_ROOT / "projects"
    assert config.projects_root(tmp_path) == tmp_path / "projects"


def test_app_state_dir_default_and_override(tmp_path: Path):
    # App-level state (keys.json, gitignored) is <root>/state — NOT inside projects/.
    assert config.app_state_dir() == config.PROJECT_ROOT / "state"
    assert config.app_state_dir(tmp_path) == tmp_path / "state"


def test_app_data_dir_default_and_override(tmp_path: Path):
    # Editable config tables (§5) live at <root>/app/data next to the loaders.
    assert config.app_data_dir() == config.PROJECT_ROOT / "app" / "data"
    assert config.app_data_dir(tmp_path) == tmp_path / "app" / "data"


def test_project_subdirs_match_spec_layout_exactly():
    # spec §7: script/ plan/ audio/ visuals/ output/ state/ — the scaffolder
    # (core.projects) builds exactly these, in this order.
    assert config.PROJECT_SUBDIRS == ("script", "plan", "audio", "visuals", "output", "state")


def test_server_settings_default_is_local_only():
    settings = config.ServerSettings.from_env({})
    assert settings.host == config.DEFAULT_HOST == "127.0.0.1"
    assert settings.port == config.DEFAULT_PORT
    assert 1 <= settings.port <= 65535


def test_server_settings_reads_env_overrides():
    settings = config.ServerSettings.from_env(
        {"CONTENTMAKER_HOST": "127.0.0.1", "CONTENTMAKER_PORT": "9443"}
    )
    assert settings.host == "127.0.0.1"
    assert settings.port == 9443


def test_server_settings_rejects_non_numeric_port():
    with pytest.raises(ValueError, match="CONTENTMAKER_PORT"):
        config.ServerSettings.from_env({"CONTENTMAKER_PORT": "http"})


def test_server_settings_rejects_out_of_range_port():
    with pytest.raises(ValueError, match="CONTENTMAKER_PORT"):
        config.ServerSettings.from_env({"CONTENTMAKER_PORT": "70000"})
