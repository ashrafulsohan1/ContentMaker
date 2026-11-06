"""Seams: app.config — filesystem paths + server settings (BUILD_PLAN 1.9).

config.py is the single home for where ContentMaker puts things on disk
(spec §7): the per-project ``projects/`` tree, the app-level gitignored
``state/`` folder (keys.json lands there in task 2.1), and the editable
``app/data/`` config tables (§5). Path helpers accept an optional base so
tests and callers relocate the tree without monkeypatching constants, and
server settings are read from an env mapping at call time — importing the
module never binds machine-specific configuration.
"""
import sys
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


# --- v1.0.1 packaging: frozen-aware writable base ---------------------------


def test_app_home_env_mapping_param(tmp_path: Path):
    # CONTENTMAKER_HOME wins above everything, including frozen mode (below).
    assert config.app_home({"CONTENTMAKER_HOME": str(tmp_path / "portable")}) == (
        tmp_path / "portable"
    )


def test_app_home_non_frozen_is_project_root(monkeypatch: pytest.MonkeyPatch):
    # Dev/repo behavior is byte-identical to pre-packaging: writable base = repo root.
    monkeypatch.delenv("CONTENTMAKER_HOME", raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert config.app_home() == config.PROJECT_ROOT == Path(__file__).resolve().parents[1]


def test_app_home_frozen_is_executable_dir_not_extraction_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # PyInstaller one-file extracts to sys._MEIPASS (wiped per run); the writable
    # base (projects/, state/) must sit next to the exe instead.
    monkeypatch.delenv("CONTENTMAKER_HOME", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "dist" / "ContentMaker.exe"))
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_MEI_extraction"), raising=False)
    assert config.app_home() == (tmp_path / "dist").resolve()
    assert config.projects_root() == (tmp_path / "dist").resolve() / "projects"
    assert config.app_state_dir() == (tmp_path / "dist").resolve() / "state"
    assert config.app_home() != tmp_path / "_MEI_extraction"


def test_contentmaker_home_override_wins_above_frozen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "portable-home"
    monkeypatch.setenv("CONTENTMAKER_HOME", str(home))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "elsewhere" / "ContentMaker.exe"))
    assert config.app_home() == home
    assert config.projects_root() == home / "projects"
    assert config.app_state_dir() == home / "state"


def test_contentmaker_home_override_applies_when_not_frozen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CONTENTMAKER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert config.projects_root() == tmp_path / "home" / "projects"
    assert config.app_state_dir() == tmp_path / "home" / "state"


def test_explicit_base_argument_still_wins_over_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Callers/tests passing an explicit base keep full control — the frozen/env
    # resolution only fills in the DEFAULT (base=None) path.
    monkeypatch.setenv("CONTENTMAKER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "elsewhere" / "exe.exe"))
    assert config.projects_root(tmp_path / "explicit") == tmp_path / "explicit" / "projects"
    assert config.app_state_dir(tmp_path / "explicit") == tmp_path / "explicit" / "state"


def test_is_frozen_mirrors_sys_flag(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert config.is_frozen() is True
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert config.is_frozen() is False
