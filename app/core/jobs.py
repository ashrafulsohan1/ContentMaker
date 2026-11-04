"""Job engine: lifecycle, SQLite persistence, single worker thread (BUILD_PLAN 1.5).

Implements spec §6.6:
- Lifecycle ``draft → estimating → awaiting_confirmation → running →
  completed | partial | failed | cancelled``, with the transition table
  enforced at the persistence boundary (JobStore.update_job_status).
- Per-asset statuses ``pending | generating | done | failed | skipped``.
- Every state transition is persisted to ``projects/<name>/state/job.db``
  (stdlib sqlite3, WAL mode — OneDrive-safe, risk #7) before it is visible.
- One background worker thread processes queued jobs sequentially; closing
  the tab cannot kill it. ``resume()`` re-enqueues jobs found ``running`` on
  disk after a server restart (mocked-level resume; the real kill-mid-job
  test is task 4.3).
- Bounded retry with exponential backoff for retryable ProviderError values;
  non-retryable errors fail the asset immediately. A failed asset never
  kills the job — the run finalizes as ``partial`` (or ``failed`` if nothing
  completed), and ``retry_asset`` re-runs just that asset.
- Hard per-job spend cap is checked BEFORE each paid call via
  ``core.cost.cap_reached`` (estimate vs spent); over-cap assets are skipped
  before any provider call, completed assets are kept, and the job marks
  ``partial`` (§10.4). Each completed call also appends one line to the
  project's ``state/spend_log.jsonl`` audit trail (core.cost.SpendLog,
  PROMPT §7); zero-cost mock calls are logged as the 0.0 they cost.
- Cancel flips the persisted status; the worker keeps completed assets,
  skips never-started ones and leaves the status ``cancelled``.

Terminal job statuses are final except via ``JobStore.reopen_for_retry``
(sanctioned re-open used by per-asset retry; task 4.4 builds the API on it).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.cost import SpendLog, cap_reached
from app.engines.base import (
    ImageRequest,
    ProviderError,
    TTSRequest,
    VideoRequest,
)
from app.engines.registry import Registry

JOB_STATUSES: tuple[str, ...] = (
    "draft",
    "estimating",
    "awaiting_confirmation",
    "running",
    "completed",
    "partial",
    "failed",
    "cancelled",
)
ASSET_STATUSES: tuple[str, ...] = ("pending", "generating", "done", "failed", "skipped")
TERMINAL_JOB_STATUSES: frozenset[str] = frozenset({"completed", "partial", "failed", "cancelled"})

DEFAULT_PROVIDER_ID = "mock"

_JOB_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"estimating", "cancelled"}),
    "estimating": frozenset({"awaiting_confirmation", "failed", "cancelled"}),
    "awaiting_confirmation": frozenset({"running", "cancelled"}),
    "running": frozenset({"completed", "partial", "failed", "cancelled"}),
    # Terminal: no exits — except JobStore.reopen_for_retry (per-asset retry path).
    "completed": frozenset(),
    "partial": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}

_ASSET_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"generating", "skipped"}),
    "generating": frozenset({"done", "failed", "pending", "skipped"}),
    "done": frozenset(),  # completed work is never silently reopened
    "failed": frozenset({"pending", "skipped"}),  # retry / explicit skip (§6.6)
    "skipped": frozenset({"pending"}),  # regenerate a skipped asset
}

class IllegalTransition(ValueError):
    """A job/asset status change that the §6.6 state machine does not allow."""


class JobNotFound(KeyError):
    def __init__(self, job_id: str) -> None:
        super().__init__(f"no job with id '{job_id}'")
        self.job_id = job_id


class AssetNotFound(KeyError):
    def __init__(self, asset_id: int) -> None:
        super().__init__(f"no asset with id {asset_id}")
        self.asset_id = asset_id


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _default_job_id() -> str:
    """Spec §6.2 shape (job_YYYYMMDD_HHMMSS) + 4 hex chars for uniqueness."""
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return f"job_{stamp}_{uuid.uuid4().hex[:4]}"


@dataclass(frozen=True, kw_only=True)
class AssetSpec:
    """Declarative unit of work submitted with a job; serialized to the db row.

    ``params`` are provider-request fields per kind: tts → text/voice[/pace/tone];
    image → prompt[/style_hint/seed/width/height]; video → prompt[/duration_s/…].
    ``out_path`` is the absolute, Windows-safe artifact path the provider writes.
    """

    kind: str  # "tts" | "image" | "video"
    beat_index: int
    out_path: Path
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class Asset:
    id: int
    job_id: str
    kind: str
    beat_index: int
    params: dict[str, Any]
    out_path: Path
    status: str
    attempts: int = 0
    estimated_cost_usd: float | None = None
    spent_usd: float = 0.0
    error: str | None = None


@dataclass
class Job:
    id: str
    project: str
    status: str
    created_at: str
    updated_at: str
    providers: dict[str, str] = field(default_factory=dict)
    spend_cap_usd: float | None = None
    spent_usd: float = 0.0
    estimate_usd: float | None = None
    error: str | None = None
    assets: tuple[Asset, ...] = ()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    project       TEXT NOT NULL,
    status        TEXT NOT NULL,
    providers     TEXT NOT NULL DEFAULT '{}',
    spend_cap_usd REAL,
    spent_usd     REAL NOT NULL DEFAULT 0.0,
    estimate_usd  REAL,
    error         TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assets (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id             TEXT NOT NULL REFERENCES jobs(id),
    kind               TEXT NOT NULL,
    beat_index         INTEGER NOT NULL,
    params             TEXT NOT NULL,
    out_path           TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'pending',
    attempts           INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL,
    spent_usd          REAL NOT NULL DEFAULT 0.0,
    error              TEXT
);
CREATE INDEX IF NOT EXISTS idx_assets_job ON assets(job_id);
"""


def _asset_from_row(row: sqlite3.Row) -> Asset:
    return Asset(
        id=row["id"],
        job_id=row["job_id"],
        kind=row["kind"],
        beat_index=row["beat_index"],
        params=json.loads(row["params"]),
        out_path=Path(row["out_path"]),
        status=row["status"],
        attempts=row["attempts"],
        estimated_cost_usd=row["estimated_cost_usd"],
        spent_usd=row["spent_usd"],
        error=row["error"],
    )


def _job_from_row(row: sqlite3.Row, assets: tuple[Asset, ...] = ()) -> Job:
    return Job(
        id=row["id"],
        project=row["project"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        providers=json.loads(row["providers"]),
        spend_cap_usd=row["spend_cap_usd"],
        spent_usd=row["spent_usd"],
        estimate_usd=row["estimate_usd"],
        error=row["error"],
        assets=assets,
    )


class JobStore:
    """SQLite persistence for one project's jobs: ``projects/<name>/state/job.db``.

    WAL mode + busy_timeout (OneDrive lock-retry, risk #7). Connections are
    opened per operation, so worker and request threads can share the store.
    """

    def __init__(self, db_path: Path, *, busy_timeout_s: float = 5.0) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._busy_timeout_s = busy_timeout_s
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=self._busy_timeout_s)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -- jobs ------------------------------------------------------------------

    def create_job(
        self,
        job_id: str,
        project: str,
        assets: list[AssetSpec],
        *,
        providers: dict[str, str] | None = None,
        spend_cap_usd: float | None = None,
    ) -> Job:
        now = _utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO jobs (id, project, status, providers, spend_cap_usd,"
                " created_at, updated_at) VALUES (?, ?, 'draft', ?, ?, ?, ?)",
                (
                    job_id,
                    project,
                    json.dumps(providers or {}, sort_keys=True),
                    spend_cap_usd,
                    now,
                    now,
                ),
            )
            for spec in assets:
                conn.execute(
                    "INSERT INTO assets (job_id, kind, beat_index, params, out_path)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        job_id,
                        spec.kind,
                        spec.beat_index,
                        json.dumps(spec.params, sort_keys=True, ensure_ascii=False),
                        str(spec.out_path),
                    ),
                )
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> Job:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise JobNotFound(job_id)
            assets = tuple(
                _asset_from_row(r)
                for r in conn.execute(
                    "SELECT * FROM assets WHERE job_id = ? ORDER BY id", (job_id,)
                ).fetchall()
            )
        return _job_from_row(row, assets)

    def list_jobs(self, project: str | None = None) -> list[Job]:
        query = "SELECT * FROM jobs"
        params: tuple = ()
        if project is not None:
            query += " WHERE project = ?"
            params = (project,)
        query += " ORDER BY created_at, id"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_job_from_row(row) for row in rows]

    def update_job_status(self, job_id: str, status: str, *, error: str | None = None) -> None:
        if status not in JOB_STATUSES:
            raise IllegalTransition(f"'{status}' is not a job status")
        with self._connect() as conn:
            current = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if current is None:
                raise JobNotFound(job_id)
            allowed = _JOB_TRANSITIONS[current["status"]]
            if status != current["status"] and status not in allowed:
                raise IllegalTransition(
                    f"illegal job transition {current['status']} → {status} for '{job_id}'"
                )
            if status == current["status"]:
                return  # idempotent no-op
            conn.execute(
                "UPDATE jobs SET status = ?, error = COALESCE(?, error),"
                " updated_at = ? WHERE id = ?",
                (status, error, _utc_now_iso(), job_id),
            )

    def reopen_for_retry(self, job_id: str) -> None:
        """Sanctioned terminal → running move, only for per-asset retry (§6.6)."""
        with self._connect() as conn:
            current = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if current is None:
                raise JobNotFound(job_id)
            if current["status"] not in ("partial", "failed", "completed"):
                raise IllegalTransition(
                    f"reopen_for_retry requires partial|failed|completed"
                    f" (job '{job_id}' is '{current['status']}')"
                )
            conn.execute(
                "UPDATE jobs SET status = 'running', updated_at = ? WHERE id = ?",
                (_utc_now_iso(), job_id),
            )

    def set_estimate(self, job_id: str, estimate_usd: float) -> None:
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is None:
                raise JobNotFound(job_id)
            conn.execute(
                "UPDATE jobs SET estimate_usd = ?, updated_at = ? WHERE id = ?",
                (estimate_usd, _utc_now_iso(), job_id),
            )

    def record_spend(self, job_id: str, amount_usd: float) -> None:
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is None:
                raise JobNotFound(job_id)
            conn.execute(
                "UPDATE jobs SET spent_usd = spent_usd + ?, updated_at = ? WHERE id = ?",
                (amount_usd, _utc_now_iso(), job_id),
            )

    # -- assets ----------------------------------------------------------------

    def list_assets(self, job_id: str) -> list[Asset]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM assets WHERE job_id = ? ORDER BY id", (job_id,)
            ).fetchall()
        return [_asset_from_row(row) for row in rows]

    def get_asset(self, asset_id: int) -> Asset:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if row is None:
            raise AssetNotFound(asset_id)
        return _asset_from_row(row)

    def update_asset(
        self,
        asset_id: int,
        *,
        status: str | None = None,
        attempts: int | None = None,
        error: str | None = None,
        spent_usd: float | None = None,
        estimated_cost_usd: float | None = None,
    ) -> None:
        with self._connect() as conn:
            row = conn.execute("SELECT status FROM assets WHERE id = ?", (asset_id,)).fetchone()
            if row is None:
                raise AssetNotFound(asset_id)
            if status is not None:
                if status not in ASSET_STATUSES:
                    raise IllegalTransition(f"'{status}' is not an asset status")
                allowed = _ASSET_TRANSITIONS[row["status"]]
                if status != row["status"] and status not in allowed:
                    raise IllegalTransition(
                        f"illegal asset transition {row['status']} → {status} (asset {asset_id})"
                    )
            sets, params = [], []
            if status is not None:
                sets.append("status = ?")
                params.append(status)
            if attempts is not None:
                sets.append("attempts = ?")
                params.append(attempts)
            if error is not None:
                sets.append("error = ?")
                params.append(error)
            if spent_usd is not None:
                sets.append("spent_usd = ?")
                params.append(spent_usd)
            if estimated_cost_usd is not None:
                sets.append("estimated_cost_usd = ?")
                params.append(estimated_cost_usd)
            if not sets:
                return
            conn.execute(f"UPDATE assets SET {', '.join(sets)} WHERE id = ?", (*params, asset_id))

    def skip_pending(self, job_id: str, *, error: str) -> int:
        """Mark every pending asset of the job skipped (cancel / cap paths)."""
        skipped = 0
        for asset in self.list_assets(job_id):
            if asset.status == "pending":
                self.update_asset(asset.id, status="skipped", error=error)
                skipped += 1
        return skipped


class JobEngine:
    """Owns the job queue and the single background worker thread (§6.6)."""

    def __init__(
        self,
        registry: Registry,
        *,
        projects_root: Path | None = None,
        store_factory: Callable[[str], JobStore] | None = None,
        spend_log_factory: Callable[[str], SpendLog] | None = None,
        max_attempts: int = 3,
        backoff_base_s: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        job_id_factory: Callable[[], str] | None = None,
        worker_poll_s: float = 0.05,
    ) -> None:
        if store_factory is None and projects_root is None:
            raise ValueError("JobEngine needs projects_root or an explicit store_factory")
        self._registry = registry
        self._projects_root = Path(projects_root) if projects_root else None
        self._store_factory = store_factory or self._default_store_factory
        if spend_log_factory is not None:
            self._spend_log_factory = spend_log_factory
        elif self._projects_root is not None:
            self._spend_log_factory = self._default_spend_log_factory
        else:  # explicit store without a log location — run without the audit trail
            self._spend_log_factory = None
        self._spend_logs: dict[str, SpendLog] = {}
        self._stores: dict[str, JobStore] = {}
        self._job_index: dict[str, str] = {}  # job_id → project (accelerates lookups)
        self._max_attempts = max(1, max_attempts)
        self._backoff_base_s = backoff_base_s
        self._sleep = sleep
        self._sleep_intervals: list[float] = []
        self._job_id_factory = job_id_factory or _default_job_id
        self._worker_poll_s = worker_poll_s
        self._queue: deque[tuple[str, str]] = deque()  # (project, job_id)
        self._active: set[str] = set()
        self._cond = threading.Condition()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._worker, name="contentmaker-job-worker", daemon=True
        )
        self._thread.start()

    # -- wiring ----------------------------------------------------------------

    def _default_store_factory(self, project: str) -> JobStore:
        assert self._projects_root is not None
        return JobStore(self._projects_root / project / "state" / "job.db")

    def _default_spend_log_factory(self, project: str) -> SpendLog:
        assert self._projects_root is not None
        return SpendLog(self._projects_root / project / "state" / "spend_log.jsonl")

    def spend_log(self, project: str) -> SpendLog | None:
        """The project's JSONL spend audit trail, or None when unconfigured."""
        if self._spend_log_factory is None:
            return None
        log = self._spend_logs.get(project)
        if log is None:
            log = self._spend_log_factory(project)
            self._spend_logs[project] = log
        return log

    def store(self, project: str) -> JobStore:
        store = self._stores.get(project)
        if store is None:
            store = self._store_factory(project)
            self._stores[project] = store
        return store

    def _all_stores(self) -> dict[str, JobStore]:
        if self._projects_root is None:
            return dict(self._stores)
        for db in sorted(self._projects_root.glob("*/state/job.db")):
            project = db.parent.parent.name
            self.store(project)
        return dict(self._stores)

    def _resolve(self, job_id: str) -> Job:
        project = self._job_index.get(job_id)
        if project is not None:
            return self.store(project).get_job(job_id)
        for project, store in self._all_stores().items():
            try:
                job = store.get_job(job_id)
            except JobNotFound:
                continue
            self._job_index[job_id] = project
            return job
        raise JobNotFound(job_id)

    @property
    def worker_ident(self) -> int | None:
        return self._thread.ident

    # -- lifecycle API (the UI/API layer drives these, task 1.10/1.11) ---------

    def create_job(
        self,
        project: str,
        assets: list[AssetSpec],
        *,
        providers: dict[str, str] | None = None,
        spend_cap_usd: float | None = None,
        job_id: str | None = None,
    ) -> Job:
        job = self.store(project).create_job(
            job_id or self._job_id_factory(),
            project,
            assets,
            providers=providers,
            spend_cap_usd=spend_cap_usd,
        )
        self._job_index[job.id] = project
        return job

    def estimate(self, job_id: str) -> Job:
        """draft → estimating → sum per-asset provider estimates → awaiting_confirmation."""
        job = self._resolve(job_id)
        store = self.store(job.project)
        store.update_job_status(job_id, "estimating")
        try:
            total = 0.0
            for asset in store.list_assets(job_id):
                provider = self._provider(job.providers, asset.kind)
                estimate = float(provider.estimate_cost_usd(self._build_request(asset)))
                store.update_asset(asset.id, estimated_cost_usd=estimate)
                total += estimate
        except Exception as exc:
            store.update_job_status(job_id, "failed", error=f"estimation failed: {exc}")
            return self.job(job_id)
        store.set_estimate(job_id, total)
        store.update_job_status(job_id, "awaiting_confirmation")
        return self.job(job_id)

    def start(self, job_id: str) -> Job:
        """awaiting_confirmation → running, then hand to the worker thread."""
        job = self._resolve(job_id)
        if job.status != "awaiting_confirmation":
            raise IllegalTransition(
                f"job '{job_id}' must be awaiting_confirmation to start"
                f" (now '{job.status}'); run estimate() and confirm first (§6.6 gate)"
            )
        self.store(job.project).update_job_status(job_id, "running")
        self._enqueue(job.project, job_id)
        return self.job(job_id)

    def cancel(self, job_id: str) -> Job:
        """Persist ``cancelled``; never-started assets are skipped immediately.

        Done assets are kept (§6.6). The one in-flight ``generating`` asset, if
        any, resolves via the worker: its provider call finishes → ``done``; a
        cancel seen between retries marks it ``skipped``.
        """
        job = self._resolve(job_id)
        store = self.store(job.project)
        store.update_job_status(job_id, "cancelled")  # worker never starts a new asset now
        store.skip_pending(job_id, error="cancelled before start")
        return self.job(job_id)

    def retry_asset(self, job_id: str, asset_id: int) -> Job:
        """Re-run one failed/skipped asset without redoing completed ones (§6.6)."""
        job = self._resolve(job_id)
        asset = self.store(job.project).get_asset(asset_id)
        if asset.job_id != job_id:
            raise ValueError(f"asset {asset_id} does not belong to job '{job_id}'")
        if asset.status not in ("failed", "skipped"):
            raise ValueError(
                f"asset {asset_id} is '{asset.status}'; only failed|skipped assets can be retried"
            )
        store = self.store(job.project)
        store.update_asset(asset_id, status="pending", error=None)
        if job.status in TERMINAL_JOB_STATUSES:
            store.reopen_for_retry(job_id)
        self._enqueue(job.project, job_id)
        return self.job(job_id)

    def resume(self) -> list[Job]:
        """Re-enqueue jobs persisted as ``running`` (server restarted mid-job).

        Assets stuck in ``generating`` reset to ``pending``; jobs still in
        draft/estimating/awaiting_confirmation stay for the user to re-drive.
        """
        resumed: list[Job] = []
        for project, store in self._all_stores().items():
            for job in store.list_jobs():
                if job.status != "running":
                    continue
                for asset in store.list_assets(job.id):
                    if asset.status == "generating":
                        store.update_asset(asset.id, status="pending", error=None)
                self._job_index[job.id] = project
                self._enqueue(project, job.id)
                resumed.append(store.get_job(job.id))
        return resumed

    def job(self, job_id: str) -> Job:
        return self._resolve(job_id)

    def list_jobs(self, project: str | None = None) -> list[Job]:
        if project is not None:
            return self.store(project).list_jobs(project)
        jobs: list[Job] = []
        for store in self._all_stores().values():
            jobs.extend(store.list_jobs())
        return sorted(jobs, key=lambda j: (j.created_at, j.id))

    def wait(self, job_id: str, statuses: set[str] | tuple[str, ...], timeout: float = 30.0) -> Job:
        """Block until the job reaches one of ``statuses`` (test/SSE helper)."""
        deadline = time.monotonic() + timeout
        wanted = set(statuses)
        while True:
            job = self.job(job_id)
            if job.status in wanted:
                return job
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"job '{job_id}' did not reach {sorted(wanted)} within {timeout}s"
                    f" (now '{job.status}')"
                )
            time.sleep(min(self._worker_poll_s, remaining))

    def wait_idle(self, timeout: float = 30.0) -> None:
        """Block until the worker has no queued or in-flight jobs (quiescence).

        Unlike ``wait`` (which returns as soon as the persisted status shows),
        this guarantees the worker finished all bookkeeping for every job —
        e.g. an in-flight asset cancelled mid-call has been written as done.
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._queue or self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"worker still busy after {timeout}s"
                        f" (queued={len(self._queue)}, active={sorted(self._active)})"
                    )
                self._cond.wait(timeout=min(self._worker_poll_s, remaining))

    def stop(self) -> None:
        self._stop_event.set()
        with self._cond:
            self._cond.notify_all()
        if self._thread.is_alive():
            self._thread.join(timeout=5.0)

    # -- worker ----------------------------------------------------------------

    def _enqueue(self, project: str, job_id: str) -> None:
        with self._cond:
            if job_id in self._active:
                return  # already queued or in flight; the running pass covers it
            self._active.add(job_id)
            self._queue.append((project, job_id))
            self._cond.notify_all()

    def _worker(self) -> None:
        while not self._stop_event.is_set():
            with self._cond:
                while not self._queue and not self._stop_event.is_set():
                    self._cond.wait(timeout=self._worker_poll_s)
                if self._stop_event.is_set():
                    return
                project, job_id = self._queue.popleft()
            try:
                self._run_job(project, job_id)
            except Exception:  # noqa: S110 — a crashed run must not kill the worker
                pass
            finally:
                with self._cond:
                    self._active.discard(job_id)
                    self._cond.notify_all()

    def _run_job(self, project: str, job_id: str) -> None:
        store = self.store(project)
        job = store.get_job(job_id)
        if job.status == "cancelled":  # cancelled while still queued
            store.skip_pending(job_id, error="cancelled before start")
            return
        if job.status != "running":  # resume() re-entry lands here as 'running'
            store.update_job_status(job_id, "running")
        for asset in store.list_assets(job_id):
            if store.get_job(job_id).status == "cancelled":
                break
            if asset.status != "pending":
                continue  # done/failed stay; failed needs explicit retry_asset
            if not self._process_asset(store, job_id, asset, project):
                break
        if store.get_job(job_id).status == "cancelled":
            store.skip_pending(job_id, error="cancelled before start")
            return
        self._finalize(store, job_id)

    def _process_asset(
        self, store: JobStore, job_id: str, asset: Asset, project: str
    ) -> bool:
        """Run one asset with bounded retry + backoff. False ⇒ job cancelled."""
        job = store.get_job(job_id)
        try:
            provider = self._provider(job.providers, asset.kind)
            request = self._build_request(asset)
        except Exception as exc:
            store.update_asset(
                asset.id, status="failed", error=f"asset cannot run: {exc}"
            )
            return True

        estimate = asset.estimated_cost_usd
        if estimate is None:
            estimate = float(provider.estimate_cost_usd(request))
            store.update_asset(asset.id, estimated_cost_usd=estimate)
        if cap_reached(job.spend_cap_usd, job.spent_usd, estimate):
            store.update_asset(
                asset.id,
                status="skipped",
                error=(
                    f"spend cap {job.spend_cap_usd:.2f} USD reached"
                    f" (spent {job.spent_usd:.2f}); skipped before paid call"
                ),
            )
            return True

        store.update_asset(asset.id, status="generating")
        for attempt in range(1, self._max_attempts + 1):
            if store.get_job(job_id).status == "cancelled":
                store.update_asset(asset.id, status="skipped", error="cancelled mid-asset")
                return False
            try:
                provider.generate(request)
                cost = float(provider.estimate_cost_usd(request))
                store.record_spend(job_id, cost)
                log = self.spend_log(project)
                if log is not None:
                    log.record(
                        job_id=job_id,
                        project=project,
                        engine=asset.kind,
                        provider_id=provider.provider_id,
                        amount_usd=cost,
                        asset_id=asset.id,
                    )
                store.update_asset(
                    asset.id, status="done", attempts=attempt, spent_usd=cost, error=None
                )
                return True
            except ProviderError as exc:
                store.update_asset(
                    asset.id, attempts=attempt, error=f"{exc.provider_id}: {exc.message}"
                )
                if not exc.retryable or attempt >= self._max_attempts:
                    store.update_asset(asset.id, status="failed")
                    return True
                interval = self._backoff_base_s * (2 ** (attempt - 1))
                self._sleep_intervals.append(interval)
                self._sleep(interval)
            except Exception as exc:  # unexpected — fail the asset, keep the job alive
                store.update_asset(
                    asset.id,
                    status="failed",
                    attempts=attempt,
                    error=f"unexpected error: {exc}",
                )
                return True
        return True  # unreachable: the loop always returns

    def _finalize(self, store: JobStore, job_id: str) -> None:
        if store.get_job(job_id).status != "running":
            return  # cancelled (or otherwise final) — leave it
        statuses = {asset.status for asset in store.list_assets(job_id)}
        if statuses <= {"done"}:
            final = "completed"
        elif "done" in statuses:
            final = "partial"
        else:
            final = "failed"
        store.update_job_status(job_id, final)

    # -- provider plumbing -----------------------------------------------------

    def _provider(self, providers: dict[str, str], kind: str):
        provider_id = providers.get(kind, DEFAULT_PROVIDER_ID)
        return self._registry.get(kind, provider_id)  # ValueError names what's missing (§6.5)

    @staticmethod
    def _build_request(asset: Asset) -> TTSRequest | ImageRequest | VideoRequest:
        params = asset.params
        out_path = Path(asset.out_path)
        if asset.kind == "tts":
            missing = {"text", "voice"} - params.keys()
            if missing:
                raise ValueError(
                    f"tts asset (beat {asset.beat_index}) missing params: {sorted(missing)}"
                )
            return TTSRequest(
                text=params["text"],
                voice=params["voice"],
                pace=float(params.get("pace", 1.0)),
                tone=params.get("tone"),
                out_path=out_path,
            )
        if asset.kind == "image":
            if "prompt" not in params:
                raise ValueError(
                    f"image asset (beat {asset.beat_index}) missing params: ['prompt']"
                )
            return ImageRequest(
                prompt=params["prompt"],
                style_hint=params.get("style_hint"),
                seed=params.get("seed"),
                width=int(params.get("width", 1024)),
                height=int(params.get("height", 1024)),
                out_path=out_path,
            )
        if asset.kind == "video":
            if "prompt" not in params:
                raise ValueError(
                    f"video asset (beat {asset.beat_index}) missing params: ['prompt']"
                )
            return VideoRequest(
                prompt=params["prompt"],
                duration_s=float(params.get("duration_s", 5.0)),
                width=int(params.get("width", 1024)),
                height=int(params.get("height", 1024)),
                out_path=out_path,
            )
        raise ValueError(
            f"unknown asset kind '{asset.kind}' (known kinds: tts, image, video)"
        )
