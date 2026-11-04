"""Project scaffolder: the spec §7 on-disk layout + unicode-safe slugs (BUILD_PLAN 1.9).

``create_project`` builds ``projects/<slug>/{script,plan,audio,visuals,output,
state}`` exactly once per name — a duplicate fails loudly instead of silently
merging into an existing project's files. The slug policy is i18n-friendly
(spec §7, D3 — "no English assumptions in file naming"): unicode letters,
marks, and digits of any script are kept verbatim (NFC-normalized, case
preserved); whitespace folds to ``-``; and ONLY characters that are unsafe as
a single path component are removed — the Windows-forbidden ``<>:"/\\|?*``
set, dots (invalid at either end on Windows, hide the folder on POSIX),
control/invisible format characters (they break name equality without being
seen), and Windows-reserved device names (CON, NUL, COM1-9, …), which cannot
exist as folders on the target platform (spec §7: Windows 10/11).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from app.config import PROJECT_SUBDIRS, projects_root


class ProjectError(ValueError):
    """A project name/layout request that cannot be satisfied safely."""


class ProjectExists(ProjectError):
    """The target project folder is already on disk."""


# Forbidden inside a single Windows path component, plus the POSIX separator
# and the dot — removed from slugs regardless of current platform so a project
# created anywhere survives Windows (spec §7: Windows is the target machine).
_PATH_UNSAFE = '<>:"/\\|?*.'

# Categories never safe in a folder name: C0/C1 controls (Cc), format and
# invisible characters (Cf — ZWSP/BOM/soft hyphen break name equality
# invisibly), surrogates (Cs), private use (Co), unassigned (Cn), and any
# leftover spaces (Z*) after whitespace folding.
_STRIP_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zs", "Zl", "Zp"})

# Windows maps these names to devices no matter the content (§7 target
# platform); a project folder with one of them cannot exist there.
_RESERVED_WINDOWS = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{n}" for n in range(1, 10)}
    | {f"LPT{n}" for n in range(1, 10)}
)

_WHITESPACE_RUN = re.compile(r"\s+")
_DASH_RUN = re.compile(r"-{2,}")


@dataclass(frozen=True)
class Project:
    """A scaffolded project: display name, filesystem-safe slug, location."""

    name: str
    slug: str
    root: Path

    @property
    def path(self) -> Path:
        return self.root / self.slug

    def subdir(self, kind: str) -> Path:
        """Path of one of the spec §7 subfolders (script/plan/audio/…/state)."""
        if kind not in PROJECT_SUBDIRS:
            known = ", ".join(PROJECT_SUBDIRS)
            raise ProjectError(f"unknown project folder {kind!r} (known: {known})")
        return self.path / kind

    def subdirs(self) -> dict[str, Path]:
        return {kind: self.subdir(kind) for kind in PROJECT_SUBDIRS}


def slugify(name: str) -> str:
    """Filesystem-safe project slug: unicode-preserving, path-unsafe-stripping.

    Keeps letters/marks/digits of any script (NFC, case preserved), folds
    whitespace runs to ``-``, and strips everything else that could make the
    name unusable as a single path component. Raises ProjectError when nothing
    usable remains or the result is a Windows-reserved device name.
    """
    slug = unicodedata.normalize("NFC", name)
    slug = _WHITESPACE_RUN.sub("-", slug)  # slugify: whitespace folds to '-'
    slug = "".join(
        ch
        for ch in slug
        if ch not in _PATH_UNSAFE and unicodedata.category(ch) not in _STRIP_CATEGORIES
    )
    slug = _DASH_RUN.sub("-", slug).strip("-")
    if not slug:
        raise ProjectError(f"project name {name!r} contains no usable characters")
    if slug.upper() in _RESERVED_WINDOWS:
        raise ProjectError(
            f"project slug {slug!r} is a reserved Windows device name; pick another name"
        )
    return slug


def create_project(name: str, *, root: Path | None = None) -> Project:
    """Create ``<root>/<slug>/`` with the six spec §7 subfolders.

    ``root`` defaults to ``config.projects_root()`` (created too, parents
    included). An existing target raises ProjectExists — project folders are
    never silently reused.
    """
    base = root if root is not None else projects_root()
    project = Project(name=name, slug=slugify(name), root=base)
    try:
        project.path.mkdir(parents=True)  # strict: an existing project is an error
    except FileExistsError as exc:
        raise ProjectExists(
            f"project {project.slug!r} already exists at {project.path}"
        ) from exc
    for kind in PROJECT_SUBDIRS:
        project.subdir(kind).mkdir()
    return project
