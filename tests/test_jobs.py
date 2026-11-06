"""Seam: app.core.jobs (BUILD_PLAN 1.5).

Job engine catches: lost job state on restart (per-transition persistence,
WAL mode, resume), runaway paid calls (hard cap checked before each call),
lost completed work on cancel (done assets kept, pending skipped), infinite
retry loops (bounded attempts + exponential backoff), cryptic failures
(provider/asset errors carried on the row), and illegal lifecycle moves
(§6.6 transition table enforced at the persistence boundary).
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
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


class RunGateTTS:
    """Call 1 fails non-retryably; call 2 blocks until ``release`` is set.

    Gives a deterministic mid-run window: while the worker sits inside asset
    2's provider call, the test can retry asset 1 on the *running* job — the
    exact moment the monitor's retry button is offered (§6.6 per-asset regen).
    """

    provider_id = "run_gate"

    def __init__(self) -> None:
        self.in_flight = threading.Event()  # set when call 2 is inside generate()
        self.release = threading.Event()
        self.calls = 0

    def generate(self, request: TTSRequest) -> TTSResult:
        self.calls += 1
        if self.calls == 1:
            raise ProviderError(self.provider_id, "call 1 fails", retryable=False)
        self.in_flight.set()
        if not self.release.wait(timeout=30.0):
            raise ProviderError(self.provider_id, "gate never released", retryable=False)
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


def test_retry_asset_while_job_is_running_is_honored(projects_root: Path):
    """The monitor offers per-asset retry while a job is running (§6.6) — the
    request must actually redo the asset.

    Regression: ``_enqueue`` swallowed the retry while the job was ``_active``
    and the worker's asset snapshot never revisited it, leaving the asset
    ``pending`` forever — unretryable (only failed|skipped) and unassemblable,
    while the UI reported "Asset N re-queued for generation".
    """
    reg = Registry()
    gate = RunGateTTS()
    reg.register("tts", gate)
    eng = JobEngine(
        reg,
        projects_root=projects_root,
        max_attempts=3,
        backoff_base_s=0.01,
        sleep=lambda _s: None,
    )
    try:
        root = projects_root / "alpha"
        job = eng.create_job(
            "alpha", [tts_spec(root, 1), tts_spec(root, 2)], providers={"tts": "run_gate"}
        )
        eng.estimate(job.id)
        eng.start(job.id)
        assert gate.in_flight.wait(timeout=15.0)  # worker inside asset 2's provider call
        failed = next(a for a in eng.job(job.id).assets if a.status == "failed")
        retried = eng.retry_asset(job.id, failed.id)  # mid-run retry on the running job
        assert retried.status == "running"

        gate.release.set()
        eng.wait_idle(timeout=30.0)
        final = eng.job(job.id)
        assert final.status == "completed", [
            (a.id, a.status, a.error) for a in final.assets
        ]
        assert all(a.status == "done" for a in final.assets)
    finally:
        gate.release.set()
        eng.stop()


def test_worker_crash_marks_job_failed_and_worker_survives(
    engine, projects_root: Path, monkeypatch
):
    """A crashed ``_run_job`` (e.g. the OneDrive/sqlite lock risk #7) must not
    strand the job as ``running`` forever — it is marked ``failed`` with the
    error recorded, and the worker goes on to process the next job."""
    original_list_assets = JobStore.list_assets
    state = {"armed": False}

    def exploding_list_assets(self, job_id):
        if state["armed"]:
            state["armed"] = False
            raise RuntimeError("simulated store crash")
        return original_list_assets(self, job_id)

    monkeypatch.setattr(JobStore, "list_assets", exploding_list_assets)

    root = projects_root / "alpha"
    job = engine.create_job("alpha", [tts_spec(root, 1)])
    engine.estimate(job.id)
    state["armed"] = True
    engine.start(job.id)
    failed = engine.wait(job.id, {"failed"}, timeout=10.0)
    assert failed.error is not None and "simulated store crash" in failed.error

    # the worker survived the crash and still runs the next job
    job2 = engine.create_job("alpha", [tts_spec(root, 2)])
    engine.estimate(job2.id)
    engine.start(job2.id)
    assert run_to_end(engine, job2.id).status == "completed"


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


# --- BUILD_PLAN 4.3: resume after a real server kill -------------------------


_KILL_CHILD_SOURCE = '''\
"""Kill-mid-job child (BUILD_PLAN 4.3): runs a real JobEngine worker and
blocks inside beat 2's provider call so the parent can kill the process.
argv: <repo_root> <projects_root>"""
import sys
import time
from pathlib import Path

repo_root, projects_root = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(repo_root))

from app.core.jobs import AssetSpec, JobEngine  # noqa: E402
from app.engines.base import TTSRequest, TTSResult  # noqa: E402
from app.engines.registry import Registry  # noqa: E402


class ChildTTS:
    provider_id = "killprobe"

    def generate(self, request: TTSRequest) -> TTSResult:
        if request.out_path.name == "beat_02.wav":
            time.sleep(120)  # the parent kills the process inside this call
        request.out_path.parent.mkdir(parents=True, exist_ok=True)
        request.out_path.write_bytes(b"child-wav")
        return TTSResult(file=request.out_path, duration_s=1.0)

    def estimate_cost_usd(self, request: TTSRequest) -> float:
        return 0.25


registry = Registry()
registry.register("tts", ChildTTS())
engine = JobEngine(
    registry,
    projects_root=projects_root,
    max_attempts=3,
    backoff_base_s=0.01,
    sleep=lambda _s: None,
)
root = projects_root / "killproj"
specs = [
    AssetSpec(
        kind="tts",
        beat_index=index,
        out_path=root / "audio" / f"beat_{index:02d}.wav",
        params={"text": f"beat {index}", "voice": "demo"},
    )
    for index in (1, 2, 3)
]
job = engine.create_job("killproj", specs, providers={"tts": "killprobe"})
engine.estimate(job.id)
engine.start(job.id)
store = engine.store("killproj")
deadline = time.monotonic() + 30.0
while time.monotonic() < deadline:
    snapshot = {a.beat_index: a.status for a in store.list_assets(job.id)}
    if snapshot == {1: "done", 2: "generating", 3: "pending"}:
        break
    time.sleep(0.02)
else:
    print("CHILD_NEVER_MIDJOB", snapshot, flush=True)
    sys.exit(3)
print("CHILD_MIDJOB", job.id, flush=True)
time.sleep(120)  # killed here, mid-job, by the parent
'''


class ResumedTTS:
    """Records the file names the resuming engine actually (re)generates."""

    provider_id = "killprobe"

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def generate(self, request: TTSRequest) -> TTSResult:
        self.calls.append(request.out_path.name)
        request.out_path.parent.mkdir(parents=True, exist_ok=True)
        request.out_path.write_bytes(b"parent-wav")
        return TTSResult(file=request.out_path, duration_s=1.0)

    def estimate_cost_usd(self, request: TTSRequest) -> float:
        return 0.25


def _read_db(db_path: Path):
    """(job_status, {beat_index: status}, [job_id]) or None while locked.

    Single-job db; the child is the only writer, so a locked read just means
    "retry in a moment".
    """
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            jobs = conn.execute("SELECT id, status FROM jobs").fetchall()
            assets = conn.execute("SELECT beat_index, status FROM assets ORDER BY id").fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return None
    if not jobs:  # schema bootstrapped, create_job not committed yet
        return None
    return jobs[0][1], {beat: status for beat, status in assets}, [row[0] for row in jobs]


def test_resume_after_real_process_kill_mid_job(tmp_path: Path, projects_root: Path):
    """BUILD_PLAN 4.3: a job survives a hard process kill mid-job.

    A child process runs a real worker; the parent watches the persisted WAL
    db, ``kill()``s the process while beat 2 is mid-generate (the closest
    testable equivalent of a dead server), then a fresh JobEngine on the same
    persisted state resumes: completed beat 1 is never redone, the in-flight
    beat 2 restarts, spend is not double-counted.
    """
    repo_root = Path(__file__).resolve().parents[1]
    child_script = tmp_path / "kill_child_job.py"
    child_script.write_text(_KILL_CHILD_SOURCE, encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-I", str(child_script), str(repo_root), str(projects_root)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(repo_root),
    )
    reaped = False

    def kill_and_reap() -> tuple[str, str]:
        nonlocal reaped
        proc.kill()
        out, err = proc.communicate(timeout=15)
        reaped = True
        return out, err

    db = projects_root / "killproj" / "state" / "job.db"
    mid_job = {1: "done", 2: "generating", 3: "pending"}
    beat1 = projects_root / "killproj" / "audio" / "beat_01.wav"
    try:
        deadline = time.monotonic() + 60.0
        while True:
            snapshot = _read_db(db) if db.exists() else None
            if snapshot is not None and snapshot[0] == "running" and snapshot[1] == mid_job:
                break  # child blocked inside beat 2's provider call
            if proc.poll() is not None:  # died before the window (import error, …)
                out, err = proc.communicate()
                reaped = True
                pytest.fail(f"kill-child exited early: stdout={out!r} stderr={err!r}")
            if time.monotonic() > deadline:
                out, err = kill_and_reap()
                pytest.fail(f"child never reached mid-job state: stdout={out!r} stderr={err!r}")
            time.sleep(0.02)

        # The child confirms the window from its own side. Safe to block: every
        # child path prints a line or exits, so readline cannot hang.
        line = proc.stdout.readline().strip()
        if not line.startswith("CHILD_MIDJOB"):
            out, err = kill_and_reap()
            pytest.fail(f"kill-child did not confirm mid-job: line={line!r} stderr={err!r}")

        out, err = kill_and_reap()
        assert not err, (out, err)
        # The durable state resume() starts from — committed WAL survives the kill.
        snapshot = _read_db(db)
        assert snapshot is not None, "killed process left no readable job row"
        status, by_beat, job_ids = snapshot
        assert (status, by_beat) == ("running", mid_job)
        job_id = job_ids[0]
        assert beat1.read_bytes() == b"child-wav"

        registry = Registry()
        resumed_calls: list[str] = []
        registry.register("tts", ResumedTTS(resumed_calls))
        engine2 = JobEngine(
            registry,
            projects_root=projects_root,
            max_attempts=3,
            backoff_base_s=0.01,
            sleep=lambda _s: None,
        )
        try:
            assert [j.id for j in engine2.resume()] == [job_id]
            assert [(a.beat_index, a.status) for a in engine2.job(job_id).assets] == [
                (1, "done"),  # completed work kept
                (2, "pending"),  # in-flight work reset for a clean redo
                (3, "pending"),
            ]

            final = engine2.wait(job_id, TERMINAL, timeout=15.0)
            assert final.status == "completed"
            assert [(a.beat_index, a.status, a.attempts) for a in final.assets] == [
                (1, "done", 1),  # attempts untouched: not re-run
                (2, "done", 1),
                (3, "done", 1),
            ]
            # the resuming engine re-called only beats 2 and 3 — beat 1 kept
            assert resumed_calls == ["beat_02.wav", "beat_03.wav"]
            assert beat1.read_bytes() == b"child-wav"  # completed artifact untouched
            assert (
                projects_root / "killproj" / "audio" / "beat_02.wav"
            ).read_bytes() == b"parent-wav"
            # beat 1's spend (recorded by the killed process) is kept; the
            # resume re-records only the two redone beats.
            assert final.spent_usd == pytest.approx(0.75)
            spend_lines = [
                json.loads(line)
                for line in (projects_root / "killproj" / "state" / "spend_log.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            assert sorted(e["asset_id"] for e in spend_lines) == sorted(a.id for a in final.assets)
        finally:
            engine2.stop()
    finally:
        if not reaped:
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=15)


def test_resume_leaves_non_running_jobs_alone(engine, projects_root: Path):
    """resume() is only for jobs caught mid-run by a restart: draft /
    awaiting_confirmation stay for the user to re-drive, terminal stay final."""
    root = projects_root / "alpha"
    draft = engine.create_job("alpha", [tts_spec(root, 1)])
    gated = engine.create_job("alpha", [tts_spec(root, 2)])
    engine.estimate(gated.id)
    finished = engine.create_job("alpha", [tts_spec(root, 3)])
    engine.estimate(finished.id)
    engine.start(finished.id)
    assert run_to_end(engine, finished.id).status == "completed"

    assert engine.resume() == []
    assert engine.job(draft.id).status == "draft"
    assert engine.job(gated.id).status == "awaiting_confirmation"
    assert engine.job(finished.id).status == "completed"
