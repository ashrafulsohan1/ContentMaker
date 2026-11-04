"""Seams: app.core.projects — project scaffolder + unicode-preserving slugify (BUILD_PLAN 1.9).

``create_project`` builds the spec §7 layout ``projects/<slug>/{script,plan,
audio,visuals,output,state}`` exactly once per name — a duplicate fails loudly
instead of silently merging into an existing project. The slug policy is
i18n-friendly (spec §7, D3: no English assumptions in file naming): unicode
letters, marks, and digits of ANY script are kept verbatim (NFC-normalized,
case preserved); whitespace folds to ``-``; and ONLY characters that are
unsafe as a single path component are removed — the Windows-forbidden
``<>:"/\\|?*`` set, dots (unsafe at either end on Windows/POSIX), control and
invisible format characters (they break name equality without being seen),
and Windows-reserved device names (CON, NUL, COM1-9, …), which cannot exist
as folders on the target platform (spec §7: Windows 10/11).
"""
from pathlib import Path

import pytest

from app import config
from app.core.projects import Project, ProjectError, ProjectExists, create_project, slugify

# --- create_project: the spec §7 layout --------------------------------------


def test_create_project_builds_the_spec_layout(tmp_path: Path):
    project = create_project("My Project", root=tmp_path)
    assert isinstance(project, Project)
    assert project.name == "My Project"
    assert project.slug == "My-Project"
    assert project.path == tmp_path / "My-Project"
    created = sorted(p.name for p in project.path.iterdir())
    assert created == sorted(config.PROJECT_SUBDIRS)
    for kind in ("script", "plan", "audio", "visuals", "output", "state"):
        assert project.subdir(kind).is_dir()
    assert sorted(project.subdirs()) == sorted(config.PROJECT_SUBDIRS)


def test_create_project_creates_the_projects_root_itself(tmp_path: Path):
    base = tmp_path / "relocated" / "projects-root"  # does not exist yet
    project = create_project("demo", root=base)
    assert project.path.is_dir()
    assert project.subdir("state").is_dir()


def test_create_project_rejects_duplicates_loudly(tmp_path: Path):
    create_project("dup", root=tmp_path)
    with pytest.raises(ProjectExists, match="dup"):
        create_project("dup", root=tmp_path)


def test_subdir_rejects_unknown_folder_kind(tmp_path: Path):
    project = create_project("x", root=tmp_path)
    with pytest.raises(ValueError, match="unknown project folder"):
        project.subdir("nope")


# --- slugify: preserve unicode, strip only path-unsafe ------------------------


def test_slugify_preserves_unicode_letters_of_any_script():
    assert slugify("Мой Проект") == "Мой-Проект"
    assert slugify("プロジェクト 名前") == "プロジェクト-名前"
    assert slugify("Καλημέρα κόσμε") == "Καλημέρα-κόσμε"


def test_slugify_preserves_combining_marks_and_non_ascii_digits():
    # Devanagari vowel signs are Mn — stripping them would corrupt the word;
    # Arabic-Indic digits are Nd — digits of any script count as digits.
    assert slugify("हिन्दी ١٢٣") == "हिन्दी-١٢٣"


def test_slugify_keeps_case_and_ascii_digits():
    assert slugify("MiXeD Case 42") == "MiXeD-Case-42"


def test_slugify_nfc_normalizes_so_equivalent_inputs_collide():
    assert slugify("e\u0301tude") == "étude"  # decomposed é composes under NFC


def test_slugify_strips_only_path_unsafe_characters():
    assert slugify('a/b\\c:d*e?f"g<h>i|j') == "abcdefghij"
    assert slugify("../../etc/passwd") == "etcpasswd"  # traversal neutralized


def test_slugify_folds_whitespace_runs_and_trims_edges():
    assert slugify("  many   spaces  ") == "many-spaces"
    assert slugify("tab\tand\nnewline") == "tab-and-newline"
    assert slugify("a - b") == "a-b"
    assert slugify("trailing---dashes---") == "trailing-dashes"


def test_slugify_drops_dots_and_invisible_characters():
    assert slugify("v2.0") == "v20"
    assert slugify("name.") == "name"  # trailing dot is invalid on Windows
    assert slugify(".hidden") == "hidden"  # leading dot hides the folder on POSIX
    assert slugify("a\u200bb\ufeffc\x7f") == "abc"  # ZWSP / BOM / DEL never reach disk


def test_slugify_empty_result_is_an_error():
    for bad in ("", "   ", "???", "///", "\u200b\u200b", "."):
        with pytest.raises(ProjectError, match="no usable characters"):
            slugify(bad)


def test_slugify_rejects_windows_reserved_device_names():
    for bad in ("CON", "nul", "Com1", "lpt4"):
        with pytest.raises(ProjectError, match="reserved Windows"):
            slugify(bad)


def test_slugified_unicode_name_scaffolds_verbatim(tmp_path: Path):
    project = create_project("プロジェクト 名前", root=tmp_path)
    assert project.slug == "プロジェクト-名前"
    assert project.path.is_dir() and project.subdir("audio").is_dir()
