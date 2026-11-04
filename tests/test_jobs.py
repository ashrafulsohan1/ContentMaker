"""Seam: app.core.jobs (BUILD_PLAN 1.5).

Job engine catches: lost job state on restart (per-transition persistence,
WAL mode, resume), runaway paid calls (hard cap checked before each call),
lost completed work on cancel (done assets kept, pending skipped), infinite
retry loops (bounded attempts + exponential backoff), cryptic failures
(provider/asset errors carried on the row), and illegal lifecycle moves
(§6.6 transition table enforced at the persistence boundary).
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from app.core.jobs import (
    AssetSpec,
    IllegalTransition,
    JobEngine,
    JobNotFound,
    JobStore,
)
from app.engines.base import (
    ImageRequest,
    ImageResult,
    ProviderError,
    TTSRequest,
    TTSResult,
)
from app.engines.mocks import MockImageProvider, MockTTSProvider
from app.engines.registry import Registry

TERMINAL = {"completed", "partial", "failed", "cancelled"}


# --- test doubles -----------------------------------------------------------


class FlakyTTS:
    """Fails the first ``failures`` calls, then writes a fake WAV."""

    provider_id = "flaky"

    def __init__(self, failures: int = 0, retryable: bool = True, cost: float = 0.0):
        self.failures = failures
        self.retryable = retryable
        self.cost = cost
        self.calls = 0
        self.threads: set[int] = set()

    def generate(self, request: TTSRequest) -> TTSResult:
        self.calls += 1
        self.threads.add(threading.get_ident())
        if self.calls <= self.failures:
            raise ProviderError(
                self.provider_id, f"synthesis failed (call {self.calls})", retryable=self.retryable
            )
        request.out_path.parent.mkdir(parents=True, exist_ok=True)
        request.out_path.write_bytes(b"fake-wav")
        return TTSResult(file=request.out_path, duration_s=1.0)

    def estimate_cost_usd(self, request: TTSRequest) -> float:
        return self.cost


class PricedImage:
    """Succeeds immediately; every call 'costs' ``cost`` USD."""

    provider_id = "priced"

    def __init__(self, cost: float):
        self.cost = cost
        self.calls = 0

    def generate(self, request: ImageRequest) -> ImageResult:
        self.calls += 1
        request.out_path.parent.mkdir(parents=True, exist_ok=True)
        request.out_path.write_bytes(b"fake-png")
        return ImageResult(file=request.out_path)

    def estimate_cost_usd(self, request: ImageRequest) -> float:
        return self.cost


class CancelProbe:
    """Blocks inside the 2nd generate until the job is cancelled, then succeeds."""

    provider_id = "cancel_probe"

    def __init__(self, engine: JobEngine, job_id: str):
        self.engine = engine
        self.job_id = job_id
        self.second_call_started = threading.Event()
        self.calls = 0

    def generate(self, request: TTSRequest) -> TTSResult:
        self.calls += 1
        if self.calls >= 2:
            self.second_call_started.set()
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if self.engine.job(self.job_id).status == "cancelled":
                    break
                time.sleep(0.01)
        request.out_path.parent.mkdir(parents=True, exist_ok=True)
        request.out_path.write_bytes(b"fake-wav")
        return TTSResult(file=request.out_path, duration_s=1.0)

    def estimate_cost_usd(self, request: TTSRequest) -> float:
        return 0.0


# --- fixtures / helpers ------------------------------------------------------


@pytest.fixture()
def projects_root(tmp_path: Path) -> Path:
    return tmp_path / "projects"


@pytest.fixture()
def registry() -> Registry:
    reg = Registry()
    reg.register("tts", MockTTSProvider())
    reg.register("image", MockImageProvider())
    return reg


@pytest.fixture()
def engine(projects_root: Path, registry: Registry):
    eng = JobEngine(
        registry,
        projects_root=projects_root,
        max_attempts=3,
        backoff_base_s=0.01,
        sleep=lambda _s: None,  # instant backoff; intervals asserted via eng.slept
    )
    eng.slept = eng._sleep_intervals  # exposed list of backoff intervals used
    yield eng
    eng.stop()


def tts_spec(root: Path, index: int, text: str = "hello beat") -> AssetSpec:
    return AssetSpec(
        kind="tts",
        beat_index=index,
        out_path=root / "audio" / f"beat_{index:02d}.wav",
        params={"text": text, "voice": "demo"},
    )


def img_spec(root: Path, index: int) -> AssetSpec:
    return AssetSpec(
        kind="image",
        beat_index=index,
        out_path=root / "visuals" / f"beat_{index:02d}.png",
        params={"prompt": f"scene {index}", "width": 64, "height": 64},
    )


def run_to_end(engine: JobEngine, job_id: str, timeout: float = 15.0):
    return engine.wait(job_id, TERMINAL, timeout=timeout)


# --- tests -------------------------------------------------------------------


def test_create_job_starts_draft_with_pending_assets(engine, projects_root: Path):
    root = projects_root / "alpha"
    job = engine.create_job("alpha", [tts_spec(root, 1), img_spec(root, 1)])
    assert job.id.startswith("job_")
    assert job.project == "alpha"
    assert job.status == "draft"
    assert [a.status for a in job.assets] == ["pending", "pending"]
    assert [a.kind for a in job.assets] == ["tts", "image"]


def test_estimate_sums_provider_costs_and_gates_start(engine, projects_root: Path):
    root = projects_root / "alpha"
    registry = engine._registry
    registry.register("tts", FlakyTTS(failures=0, cost=0.25))
    registry.register("image", PricedImage(0.5))

    job = engine.create_job(
        "alpha",
        [tts_spec(root, 1), tts_spec(root, 2), img_spec(root, 1)],
        providers={"tts": "flaky", "image": "priced"},
    )
    with pytest.raises(IllegalTransition, match="awaiting_confirmation"):
        engine.start(job.id)  # must estimate + confirm first (§6.6 gate)

    estimated = engine.estimate(job.id)
    assert estimated.status == "awaiting_confirmation"
    assert estimated.estimate_usd == pytest.approx(2 * 0.25 + 0.5)
    # persisted, not just in memory
    assert engine.store("alpha").get_job(job.id).estimate_usd == pytest.approx(1.0)


def test_lifecycle_run_completes_and_persists_to_project_state_db(
    engine, projects_root: Path, registry: Registry
):
    root = projects_root / "alpha"
    job = engine.create_job("alpha", [tts_spec(root, 1), img_spec(root, 1)])
    engine.estimate(job.id)
    engine.start(job.id)
    final = run_to_end(engine, job.id)

    assert final.status == "completed"
    assert [a.status for a in final.assets] == ["done", "done"]
    assert (root / "audio" / "beat_01.wav").exists()
    assert (root / "visuals" / "beat_01.png").exists()

    # every transition was persisted: a fresh store on the same db sees the end state
    db_path = projects_root / "alpha" / "state" / "job.db"
    fresh = JobStore(db_path)
    persisted = fresh.get_job(job.id)
    assert persisted.status == "completed"
    assert [a.status for a in persisted.assets] == ["done", "done"]
    assert persisted.created_at <= persisted.updated_at


def test_retryable_failures_back_off_then_succeed(engine, projects_root: Path, registry: Registry):
    root = projects_root / "alpha"
    flaky = FlakyTTS(failures=2, retryable=True)
    registry.register("tts", flaky)
    job = engine.create_job("alpha", [tts_spec(root, 1)], providers={"tts": "flaky"})
    engine.estimate(job.id)
    engine.start(job.id)
    final = run_to_end(engine, job.id)

    assert final.status == "completed"
    asset = final.assets[0]
    assert asset.status == "done"
    assert asset.attempts == 3
    assert flaky.calls == 3
    assert engine.slept == [0.01, 0.02]  # exponential backoff: base * 2^0, base * 2^1


def test_retry_exhaustion_marks_asset_failed_job_partial(
    engine, projects_root: Path, registry: Registry
):
    root = projects_root / "alpha"
    flaky = FlakyTTS(failures=99, retryable=True)
    registry.register("tts", flaky)
    job = engine.create_job(
        "alpha", [img_spec(root, 1), tts_spec(root, 1)], providers={"tts": "flaky"}
    )
    engine.estimate(job.id)
    engine.start(job.id)
    final = run_to_end(engine, job.id)

    assert final.status == "partial"  # one done, one failed — job survived (§6.6)
    img_asset, tts_asset = final.assets
    assert img_asset.status == "done"
    assert tts_asset.status == "failed"
    assert tts_asset.attempts == 3
    assert tts_asset.error is not None and "synthesis failed" in tts_asset.error
    assert flaky.calls == 3


def test_non_retryable_error_fails_without_retries(engine, projects_root: Path, registry: Registry):
    root = projects_root / "alpha"
    flaky = FlakyTTS(failures=1, retryable=False)
    registry.register("tts", flaky)
    job = engine.create_job(
        "alpha", [img_spec(root, 1), tts_spec(root, 1)], providers={"tts": "flaky"}
    )
    engine.estimate(job.id)
    engine.start(job.id)
    final = run_to_end(engine, job.id)

    assert final.status == "partial"
    assert final.assets[1].status == "failed"
    assert final.assets[1].attempts == 1
    assert engine.slept == []  # non-retryable → no backoff sleeps at all


def test_spend_cap_stops_paid_calls_and_marks_partial(
    engine, projects_root: Path, registry: Registry
):
    root = projects_root / "alpha"
    priced = PricedImage(1.0)
    registry.register("image", priced)
    job = engine.create_job(
        "alpha",
        [img_spec(root, 1), img_spec(root, 2), img_spec(root, 3)],
        providers={"image": "priced"},
        spend_cap_usd=1.5,
    )
    engine.estimate(job.id)
    engine.start(job.id)
    final = run_to_end(engine, job.id)

    assert final.status == "partial"
    assert [a.status for a in final.assets] == ["done", "skipped", "skipped"]
    assert final.spent_usd == pytest.approx(1.0)
    assert priced.calls == 1  # cap checked BEFORE the paid call: calls 2–3 never happened
    assert "cap" in (final.assets[1].error or "").lower()
    assert final.assets[0].status == "done"  # completed asset kept


def test_cancel_keeps_completed_assets(engine, projects_root: Path):
    root = projects_root / "alpha"
    job = engine.create_job(
        "alpha",
        [tts_spec(root, 1), tts_spec(root, 2), tts_spec(root, 3)],
        providers={"tts": "cancel_probe"},
    )
    probe = CancelProbe(engine, job.id)
    engine._registry.register("tts", probe)
    engine.estimate(job.id)
    engine.start(job.id)
    assert probe.second_call_started.wait(10.0)  # asset 2 is mid-generate
    engine.cancel(job.id)
    engine.wait_idle(10.0)  # in-flight asset resolved + worker bookkeeping done
    final = engine.job(job.id)

    assert final.status == "cancelled"
    statuses = [a.status for a in final.assets]
    assert statuses[0] == "done"  # completed asset kept
    assert statuses[1] == "done"  # finished its in-flight call before noticing
    assert statuses[2] == "skipped"  # never started


def test_store_transition_rules_and_terminal_finality(engine, projects_root: Path):
    store = engine.store("tproj")
    root = projects_root / "tproj"
    job_id = "job_test_transitions"
    store.create_job(
        job_id=job_id,
        project="tproj",
        assets=[tts_spec(root, 1)],
    )
    assert store.get_job(job_id).status == "draft"

    with pytest.raises(IllegalTransition):
        store.update_job_status(job_id, "running")  # draft → running is illegal
    store.update_job_status(job_id, "estimating")
    with pytest.raises(IllegalTransition):
        store.update_job_status(job_id, "running")  # estimating → running is illegal
    store.update_job_status(job_id, "awaiting_confirmation")
    store.update_job_status(job_id, "running")
    store.update_job_status(job_id, "running")  # same-status is a no-op
    store.update_job_status(job_id, "completed")

    with pytest.raises(IllegalTransition):
        store.update_job_status(job_id, "running")  # terminal is final
    store.reopen_for_retry(job_id)  # sanctioned re-open for per-asset retry
    assert store.get_job(job_id).status == "running"


def test_state_db_lives_under_project_state_dir_in_wal_mode(engine, projects_root: Path):
    root = projects_root / "alpha"
    job = engine.create_job("alpha", [tts_spec(root, 1)])
    db_path = projects_root / "alpha" / "state" / "job.db"
    assert db_path.exists()

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()
    assert [j.id for j in engine.list_jobs("alpha")] == [job.id]


def test_resume_requeues_running_jobs_after_restart(engine, projects_root: Path):
    root = projects_root / "alpha"
    job = engine.create_job("alpha", [tts_spec(root, 1), tts_spec(root, 2), tts_spec(root, 3)])
    engine.estimate(job.id)

    # simulate a crash mid-run: state on disk says running, asset 1 done, asset 2 generating
    store = engine.store("alpha")
    store.update_job_status(job.id, "running")
    a1, a2 = store.list_assets(job.id)[:2]
    store.update_asset(a1.id, status="generating")
    store.update_asset(a1.id, status="done")
    store.update_asset(a2.id, status="generating")

    resumed = JobEngine(
        engine._registry,
        projects_root=projects_root,
        max_attempts=3,
        backoff_base_s=0.01,
        sleep=lambda _s: None,
    )
    try:
        assert [j.id for j in resumed.resume()] == [job.id]
        final = run_to_end(resumed, job.id)
        assert final.status == "completed"
        assert [a.status for a in final.assets] == ["done", "done", "done"]
    finally:
        resumed.stop()


def test_retry_asset_redoes_only_failed_asset(engine, projects_root: Path, registry: Registry):
    root = projects_root / "alpha"
    flaky = FlakyTTS(failures=99, retryable=True)
    registry.register("tts", flaky)
    job = engine.create_job(
        "alpha", [img_spec(root, 1), tts_spec(root, 1)], providers={"tts": "flaky"}
    )
    engine.estimate(job.id)
    engine.start(job.id)
    partial = run_to_end(engine, job.id)
    assert partial.status == "partial"

    registry.register("tts", FlakyTTS(failures=0))  # provider heals; same provider_id
    retried = engine.retry_asset(job.id, partial.assets[1].id)
    final = run_to_end(engine, job.id)

    assert final.status == "completed"
    assert final.assets[0].status == "done" and final.assets[0].attempts == 1  # not redone
    assert final.assets[1].status == "done" and final.assets[1].attempts == 1
    assert retried.status == "running"


def test_single_worker_thread_processes_queued_jobs(
    engine, projects_root: Path, registry: Registry
):
    root = projects_root / "alpha"
    flaky = FlakyTTS(failures=0)
    registry.register("tts", flaky)
    job_a = engine.create_job("alpha", [tts_spec(root, 1)], providers={"tts": "flaky"})
    job_b = engine.create_job("alpha", [tts_spec(root, 2)], providers={"tts": "flaky"})
    for job in (job_a, job_b):
        engine.estimate(job.id)
        engine.start(job.id)

    assert run_to_end(engine, job_a.id).status == "completed"
    assert run_to_end(engine, job_b.id).status == "completed"
    assert flaky.threads == {engine.worker_ident}  # both ran on the one worker thread


def test_unknown_job_raises_jobnotfound(engine):
    with pytest.raises(JobNotFound):
        engine.job("job_missing")
    with pytest.raises(JobNotFound):
        engine.cancel("job_missing")
