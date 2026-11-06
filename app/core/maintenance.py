"""In-app maintenance: startup + periodic temp/orphan sweep (BUILD_PLAN 1.13).

Implements spec §6.7 — routine housekeeping runs as a low-priority task
inside the app (first sweep at startup, then once per interval), **never
blocking generation jobs**. Concretely, "low priority, never blocking" means:

- the periodic task is its own daemon thread; the job worker is never involved
  and no lock the worker needs is ever held;
- the job.db is only ever **read** (plain SELECTs; WAL readers cannot block
  the §6.6 worker's writes) with a short busy timeout, and an unreadable db
  makes the sweep skip that project instead of guessing;
- scratch of a non-terminal (running/queued/confirming) job is never touched,
  and a min-age guard keeps anything young — including the scratch of an
  ``assemble()`` re-run on a terminal job, or a file a provider call may have
  written the moment a cancel landed — for a later sweep;
- every removal is individually guarded: a locked/undeletable entry is counted
  as a failure and the sweep moves on; the sweep itself never raises into the
  app (``MaintenanceTask`` records a ``last_error`` instead).

What gets swept, in this codebase's on-disk vocabulary (all under each
project of ``projects_root``):

- **temp/scratch** — ``state/scratch/<job_id>`` stitch intermediates
  (assembly/pipeline.py passes them; normally ``shutil.rmtree``'d in
  ``stitch_video``'s finally, but a crash/kill leaves them), plan-less
  ``.<name>.stitch_scratch`` dirs and ``.<name>.<rand>.overlay.txt`` drawtext
  sidecars next to deliverables in ``output/`` (assembly/video.py,
  assembly/images.py). Removed when the owning job is terminal or unknown to
  the project's job.db — and always only once older than the min age.
- **orphans of cancelled runs** — files at the ``out_path`` of a *cancelled*
  job's failed/skipped/generating assets (§6.6's "cancel keeps completed
  assets" is why ``done`` assets are excluded; completed/partial/failed jobs
  are retryable and left alone). Cancelled jobs are per-asset retryable too
  (core/jobs.py ``reopen_for_retry``) — the candidate JOIN is recomputed from
  job.db on every sweep, so a retried asset (``pending``) or reopened job
  (``running``) leaves the candidate set at once, and the age guard protects
  anything fresh. db-derived paths are resolved and only removed when still
  inside the project folder — a corrupt row must never turn maintenance into
  a deletion primitive outside it.

No OS scheduler, no external automation (§6.7, §11.6): the only trigger is
this app's own startup + interval loop.
"""
from __future__ import annotations

import shutil
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.core.jobs import TERMINAL_JOB_STATUSES

DEFAULT_INTERVAL_S = 3600.0  # sweep cadence after the startup sweep
DEFAULT_MIN_AGE_S = 3600.0  # anything younger is assumed possibly-in-use
_DB_BUSY_TIMEOUT_S = 2.0
_MAX_DETAILS = 200

# output/ sidecar patterns produced by the assembly layer (crash leftovers —
# both are removed by their creators' finally blocks on the normal path).
_STITCH_SCRATCH_SUFFIX = ".stitch_scratch"
_OVERLAY_SIDECAR_SUFFIX = ".overlay.txt"
_ORPHAN_ASSET_STATUSES = ("failed", "skipped", "generating")


@dataclass(frozen=True)
class SweepReport:
    """What one sweep pass removed (or failed to remove) across all projects."""

    removed_files: int = 0
    removed_dirs: int = 0
    bytes_freed: int = 0
    failures: int = 0
    details: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class _ProjectJobState:
    """The only job facts the sweep needs, read read-only from ``job.db``."""

    statuses: dict[str, str]
    cancelled_orphan_paths: tuple[Path, ...]


def sweep_projects(
    projects_root: Path | str, *, min_age_s: float = DEFAULT_MIN_AGE_S
) -> SweepReport:
    """One full sweep across every project under ``projects_root``.

    Never raises: unreadable projects are skipped with a detail entry, and
    undeletable entries count as :attr:`SweepReport.failures`. A missing root
    is an empty no-op. ``min_age_s`` guards every removal (see module docstring).
    """
    root = Path(projects_root)
    if not root.is_dir():
        return SweepReport()
    removed_files = removed_dirs = bytes_freed = failures = 0
    details: list[str] = []
    for project_dir in sorted(root.iterdir(), key=lambda p: p.name):
        if not project_dir.is_dir() or project_dir.name.startswith("."):
            continue
        state = _read_project_state(project_dir / "state" / "job.db")
        if state is None:
            details.append(f"skipped {project_dir.name}: state/job.db unreadable")
            continue
        for path, kind, files, size in _stale_scratch(project_dir, state, min_age_s):
            ok, note = _remove_path(path)
            if ok:
                removed_files += files  # a removed dir counts the files inside it
                removed_dirs += kind == "dir"
                bytes_freed += size
                details.append(f"removed scratch {path.name} ({project_dir.name})")
            else:
                failures += 1
                details.append(f"failed to remove {path} ({note})")
        for path, size in _stale_orphans(project_dir, state.cancelled_orphan_paths, min_age_s):
            ok, note = _remove_path(path)
            if ok:
                removed_files += 1
                bytes_freed += size
                details.append(f"removed orphan {path.name} ({project_dir.name})")
            else:
                failures += 1
                details.append(f"failed to remove {path} ({note})")
    return SweepReport(
        removed_files=removed_files,
        removed_dirs=removed_dirs,
        bytes_freed=bytes_freed,
        failures=failures,
        details=tuple(details[-_MAX_DETAILS:]),
    )


# --- what may be removed, per project -------------------------------------------


def _stale_scratch(
    project_dir: Path, state: _ProjectJobState, min_age_s: float
) -> list[tuple[Path, str, int, int]]:
    """Stale temp/scratch entries: ``(path, kind, files, bytes)`` candidates."""
    candidates: list[tuple[Path, str, int, int]] = []
    for entry in _safe_iterdir(project_dir / "state" / "scratch"):
        status = state.statuses.get(entry.name)
        if status is not None and status not in TERMINAL_JOB_STATUSES:
            continue  # a live run owns this scratch — never touch it (§6.6)
        if not _is_stale(entry, min_age_s):
            continue
        kind = "file" if entry.is_file() or entry.is_symlink() else "dir"
        files, size = _tree_stats(entry)
        candidates.append((entry, kind, files, size))
    for entry in _safe_iterdir(project_dir / "output"):
        name = entry.name
        if not name.endswith((_STITCH_SCRATCH_SUFFIX, _OVERLAY_SIDECAR_SUFFIX)):
            continue  # only the assembly layer's own leftovers; never deliverables
        if not _is_stale(entry, min_age_s):
            continue
        kind = "file" if entry.is_file() or entry.is_symlink() else "dir"
        files, size = _tree_stats(entry)
        candidates.append((entry, kind, files, size))
    return candidates


def _stale_orphans(
    project_dir: Path, orphan_paths: tuple[Path, ...], min_age_s: float
) -> list[tuple[Path, int]]:
    """Stale, in-project files left behind by cancelled runs."""
    stale: list[tuple[Path, int]] = []
    for raw in orphan_paths:
        path = Path(raw)
        try:
            resolved = path.resolve()
        except OSError:
            continue
        try:
            resolved.relative_to(project_dir.resolve())
        except ValueError:
            continue  # outside the project — never a maintenance target
        if not resolved.is_file() or path.is_symlink():
            continue
        if not _is_stale(resolved, min_age_s):
            continue
        try:
            stale.append((resolved, resolved.stat().st_size))
        except OSError:
            continue
    return stale


# --- job.db read access -----------------------------------------------------------


def _read_project_state(db_path: Path) -> _ProjectJobState | None:
    """Job statuses + cancelled-run orphan candidates, or None when unreadable.

    Plain SELECT-only connection: in WAL mode readers never block the §6.6
    worker's writes; a short busy timeout and ``sqlite3.Error`` → None keep a
    busy/locked db from ever slowing the sweep down or making it guess.
    """
    if not db_path.is_file():
        return _ProjectJobState(statuses={}, cancelled_orphan_paths=())
    try:
        conn = sqlite3.connect(db_path, timeout=_DB_BUSY_TIMEOUT_S)
        try:
            statuses = {
                str(row[0]): str(row[1])
                for row in conn.execute("SELECT id, status FROM jobs").fetchall()
            }
            orphans = [
                Path(str(row[0]))
                for row in conn.execute(
                    "SELECT a.out_path FROM assets AS a JOIN jobs AS j ON a.job_id = j.id"
                    " WHERE j.status = 'cancelled'"
                    f" AND a.status IN ({', '.join('?' * len(_ORPHAN_ASSET_STATUSES))})",
                    _ORPHAN_ASSET_STATUSES,
                ).fetchall()
            ]
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return _ProjectJobState(statuses=statuses, cancelled_orphan_paths=tuple(orphans))


# --- guarded filesystem primitives -------------------------------------------------


def _safe_iterdir(directory: Path) -> list[Path]:
    try:
        return sorted(directory.iterdir(), key=lambda p: p.name)
    except OSError:
        return []


def _is_stale(path: Path, min_age_s: float) -> bool:
    """Older than ``min_age_s``? A just-written entry (fresh mtime) is never stale.

    Any active write bumps the parent dir's mtime (segment files are created
    directly inside the scratch dir), so the entry's own mtime is a sufficient
    in-use signal; failures to stat read as "not stale" (keep, safe side).
    """
    try:
        return (time.time() - path.stat().st_mtime) >= min_age_s
    except OSError:
        return False


def _tree_stats(path: Path) -> tuple[int, int]:
    """``(files, bytes)`` of a file or a directory tree (best effort, for the report).

    Symlinks count as nothing (the link itself is removed, never its target).
    """
    if path.is_symlink():
        return 0, 0
    if path.is_file():
        try:
            return 1, path.stat().st_size
        except OSError:
            return 0, 0
    files = total = 0
    for child in _safe_iterdir(path):
        child_files, child_bytes = _tree_stats(child)
        files += child_files
        total += child_bytes
    return files, total


def _remove_path(path: Path) -> tuple[bool, str]:
    """Remove a file/dir/symlink; failures return ``(False, reason)`` — never raise.

    Symlinks are unlinked, never traversed. ``shutil.rmtree`` handles the
    read-only-file case on Windows (``onerror``-style retry via ``ignore_errors``
    is intentionally NOT used: a leftover must surface as a failure).
    """
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
            return True, ""
        shutil.rmtree(path)
        return True, ""
    except OSError as exc:
        return False, str(exc)


# --- periodic task: startup sweep + interval loop -----------------------------------


class MaintenanceTask:
    """Owns the maintenance daemon thread: sweep at startup, then per interval.

    ``sweep`` is injectable (tests); it defaults to :func:`sweep_projects` over
    ``projects_root``. :meth:`sweep_once` is serialized (a manual call while
    the loop is sweeping returns the last report instead of overlapping);
    unexpected sweep errors are swallowed into :attr:`last_error` — the task
    itself never dies and never propagates into the app.
    """

    def __init__(
        self,
        projects_root: Path | str,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        min_age_s: float = DEFAULT_MIN_AGE_S,
        sweep: Callable[[], SweepReport] | None = None,
    ) -> None:
        self._projects_root = Path(projects_root)
        self._interval_s = max(0.0, float(interval_s))
        self._min_age_s = float(min_age_s)
        self._sweep = sweep or (
            lambda: sweep_projects(self._projects_root, min_age_s=self._min_age_s)
        )
        self._stop_event = threading.Event()
        self._run_lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._loop, name="contentmaker-maintenance", daemon=True
        )
        self._started = False
        self.runs = 0
        self.last_report: SweepReport | None = None
        self.last_error: str | None = None

    @property
    def is_running(self) -> bool:
        """True while the maintenance thread is alive (started, not stopped)."""
        return self._thread.is_alive()

    def start(self) -> None:
        """Begin the loop (idempotent): first sweep immediately, then per interval."""
        if self._started:
            return
        self._started = True
        self._thread.start()

    def stop(self) -> None:
        """Signal the loop to end and join it (idempotent; safe before start)."""
        self._stop_event.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=5.0)

    def sweep_once(self) -> SweepReport:
        """Run one sweep now; returns the report (or the last one if one is running).

        Never raises: a failing sweep is recorded in :attr:`last_error` and an
        empty report is returned — housekeeping must not be able to take the
        app down with it (§6.7).
        """
        if not self._run_lock.acquire(blocking=False):
            return self.last_report if self.last_report is not None else SweepReport()
        try:
            self.runs += 1
            try:
                self.last_report = self._sweep()
            except Exception as exc:  # noqa: BLE001 — the task must survive anything
                # This sweep accomplished nothing observable — report empty,
                # name the failure; the loop lives on.
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.last_report = SweepReport()
            return self.last_report
        finally:
            self._run_lock.release()

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            self.sweep_once()  # startup sweep, then once per interval (§6.7)
            if self._stop_event.wait(timeout=self._interval_s):
                return
