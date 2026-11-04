"""Seam: app.core.maintenance — in-app housekeeping sweep (BUILD_PLAN 1.13).

Pins spec §6.7: temp/scratch cleanup and orphan sweeps of cancelled runs run
inside the app (startup + periodic interval), low-priority, never blocking
generation jobs. Concretely, in this codebase's on-disk vocabulary:

- the stitch scratch of a *running* job (``state/scratch/<job_id>``,
  assembly/pipeline.py) and the file of a *done* asset are never touched
  (§6.6: cancel keeps completed assets);
- fresh entries are never removed (an age guard covers in-flight ``assemble()``
  re-runs on terminal jobs and any race the status read misses);
- stale scratch of terminal or unknown jobs, plan-less ``.stitch_scratch``
  dirs, ``.overlay.txt`` drawtext sidecars (assembly/images.py), and files of
  failed/skipped assets of *cancelled* jobs (terminal — reopen_for_retry
  rejects cancelled, core/jobs.py) are removed;
- an unreadable job.db skips the project instead of guessing;
- db-derived paths outside the project are never deleted;
- a sweep never raises into the app and counts what it could not remove;
- the periodic task is its own daemon thread — the job worker is never involved.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi.testclient import TestClient

import app.core.maintenance as maintenance_module
from app.core.jobs import AssetSpec, JobStore
from app.core.maintenance import MaintenanceTask, SweepReport, sweep_projects
from app.main import create_app

OLD_AGE_S = 7200.0  # older than any default min_age — safely stale


def _project(tmp_path: Path, name: str = "demo") -> Path:
    project = tmp_path / "projects" / name
    for sub in ("script", "plan", "audio", "visuals", "output", "state"):
        (project / sub).mkdir(parents=True)
    return project


def _store(project: Path) -> JobStore:
    return JobStore(project / "state" / "job.db")


def _age(path: Path, *, age_s: float = OLD_AGE_S) -> None:
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))


def _add_job(
    store: JobStore,
    job_id: str,
    *,
    final_status: str,
    assets: list[tuple[str, int, Path, str]],
) -> None:
    """Create a job in ``final_status``; assets are (kind, beat, out_path, status)."""
    store.create_job(
        job_id,
        "demo",
        [AssetSpec(kind=kind, beat_index=beat, out_path=path) for kind, beat, path, _ in assets],
    )
    if final_status == "cancelled":
        store.update_job_status(job_id, "cancelled")  # draft → cancelled is legal
    else:
        for status in ("estimating", "awaiting_confirmation", "running"):
            store.update_job_status(job_id, status)
            if status == final_status:  # walk stops at the requested status
                break
    for asset in store.list_assets(job_id):
        target = next(
            status
            for kind, beat, _, status in assets
            if kind == asset.kind and beat == asset.beat_index
        )
        if target in ("generating", "done", "failed"):
            store.update_asset(asset.id, status="generating")
        if target in ("done", "failed"):
            store.update_asset(asset.id, status=target)
        elif target == "skipped":
            store.update_asset(asset.id, status="skipped")  # pending → skipped


# --- temp/scratch sweep -------------------------------------------------------


def test_removes_stale_scratch_of_terminal_job(tmp_path: Path):
    project = _project(tmp_path)
    _store(project)
    scratch = project / "state" / "scratch" / "job_1"
    scratch.mkdir(parents=True)
    segment = scratch / "beat_001.mp4"
    segment.write_bytes(b"x" * 100)
    _age(scratch)

    report = sweep_projects(tmp_path / "projects")

    assert not scratch.exists()
    assert report.removed_dirs == 1
    assert report.removed_files == 1
    assert report.bytes_freed == 100


def test_never_touches_running_or_awaiting_job_scratch(tmp_path: Path):
    project = _project(tmp_path)
    for status in ("running", "awaiting_confirmation"):
        job_id = f"job_{status}"
        _add_job(_store(project), job_id, final_status=status, assets=[])
        scratch = project / "state" / "scratch" / job_id
        scratch.mkdir(parents=True)
        (scratch / "beat_001.mp4").write_bytes(b"x")
        _age(scratch)

    report = sweep_projects(tmp_path / "projects")

    for status in ("running", "awaiting_confirmation"):
        assert (project / "state" / "scratch" / f"job_{status}").exists()
    assert report.removed_dirs == 0


def test_keeps_fresh_scratch_even_for_terminal_jobs(tmp_path: Path):
    # A terminal job can be re-assembled right now (engine.assemble) — the age
    # guard is what makes that race impossible, so fresh scratch stays.
    project = _project(tmp_path)
    _store(project)
    scratch = project / "state" / "scratch" / "job_1"
    scratch.mkdir(parents=True)
    (scratch / "beat_001.mp4").write_bytes(b"x")  # mtime = now

    sweep_projects(tmp_path / "projects")

    assert scratch.exists()


def test_removes_stale_scratch_of_jobs_unknown_to_the_db(tmp_path: Path):
    project = _project(tmp_path)
    _store(project)  # schema exists, job does not (db recreated / job_id stale)
    scratch = project / "state" / "scratch" / "job_gone"
    scratch.mkdir(parents=True)
    (scratch / "beat_001.mp4").write_bytes(b"x")
    _age(scratch)
    loose = project / "state" / "scratch" / "stray.tmp"
    loose.write_bytes(b"y")
    _age(loose)

    sweep_projects(tmp_path / "projects")

    assert not scratch.exists()
    assert not loose.exists()


def test_unreadable_job_db_skips_the_project_instead_of_guessing(tmp_path: Path):
    project = _project(tmp_path)
    (project / "state" / "job.db").write_bytes(b"definitely not a sqlite db")
    scratch = project / "state" / "scratch" / "job_1"
    scratch.mkdir(parents=True)
    (scratch / "beat_001.mp4").write_bytes(b"x")
    _age(scratch)

    report = sweep_projects(tmp_path / "projects")  # must not raise

    assert scratch.exists()
    assert report.removed_dirs == 0
    assert report.failures == 0
    assert any("job.db" in detail for detail in report.details)


def test_removes_stale_planless_scratch_and_overlay_sidecars_from_output(tmp_path: Path):
    project = _project(tmp_path)
    _store(project)
    stitch = project / "output" / ".deliverable.stitch_scratch"
    stitch.mkdir(parents=True)
    (stitch / "beat_001.mp4").write_bytes(b"x" * 30)
    _age(stitch)
    sidecar = project / "output" / ".beat_01.ab12cd34.overlay.txt"
    sidecar.write_text("overlay text", encoding="utf-8")
    _age(sidecar)
    deliverable = project / "output" / "deliverable.mp4"
    deliverable.write_bytes(b"real output")

    report = sweep_projects(tmp_path / "projects")

    assert not stitch.exists()
    assert not sidecar.exists()
    assert deliverable.exists()  # real deliverables are never candidates
    assert report.bytes_freed == 30 + len("overlay text")


# --- orphaned artifacts of cancelled runs ---------------------------------------


def test_removes_failed_and_skipped_asset_files_of_cancelled_jobs(tmp_path: Path):
    project = _project(tmp_path)
    _add_job(
        _store(project),
        "job_1",
        final_status="cancelled",
        assets=[
            ("tts", 1, project / "audio" / "beat_01.wav", "failed"),
            ("image", 2, project / "visuals" / "beat_02.png", "skipped"),
        ],
    )
    failed_file = project / "audio" / "beat_01.wav"
    failed_file.write_bytes(b"a" * 11)
    _age(failed_file)
    skipped_file = project / "visuals" / "beat_02.png"
    skipped_file.write_bytes(b"b" * 7)
    _age(skipped_file)

    report = sweep_projects(tmp_path / "projects")

    assert not failed_file.exists()
    assert not skipped_file.exists()
    assert report.removed_files == 2
    assert report.bytes_freed == 18


def test_done_asset_files_of_cancelled_jobs_are_kept(tmp_path: Path):
    # §6.6: cancel keeps completed assets — only failed/skipped/generating
    # leftovers of a cancelled run are orphans.
    project = _project(tmp_path)
    _add_job(
        _store(project),
        "job_1",
        final_status="cancelled",
        assets=[
            ("tts", 1, project / "audio" / "beat_01.wav", "done"),
            ("image", 2, project / "visuals" / "beat_02.png", "failed"),
        ],
    )
    done_file = project / "audio" / "beat_01.wav"
    done_file.write_bytes(b"kept")
    _age(done_file)
    failed_file = project / "visuals" / "beat_02.png"
    failed_file.write_bytes(b"orphan")
    _age(failed_file)

    sweep_projects(tmp_path / "projects")

    assert done_file.exists()
    assert not failed_file.exists()


def test_retryable_jobs_are_left_alone(tmp_path: Path):
    # failed/partial jobs can be reopened for retry (core/jobs.py) — their
    # failed-asset files may be regenerated onto, so maintenance never
    # pre-emptively deletes them.
    project = _project(tmp_path)
    for final_status in ("partial", "failed"):
        _add_job(
            _store(project),
            f"job_{final_status}",
            final_status=final_status,
            assets=[("tts", 1, project / "audio" / f"{final_status}.wav", "failed")],
        )
        orphan = project / "audio" / f"{final_status}.wav"
        orphan.write_bytes(b"x")
        _age(orphan)

    sweep_projects(tmp_path / "projects")

    assert (project / "audio" / "partial.wav").exists()
    assert (project / "audio" / "failed.wav").exists()


def test_never_deletes_paths_outside_the_project(tmp_path: Path):
    # A corrupt/hostile db row must not turn maintenance into a deletion
    # primitive: db-derived paths are only removed inside the project folder.
    project = _project(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"mine")
    _age(outside)
    _add_job(
        _store(project),
        "job_1",
        final_status="cancelled",
        assets=[("tts", 1, outside, "failed")],
    )

    sweep_projects(tmp_path / "projects")

    assert outside.exists()


def test_fresh_orphan_files_wait_for_a_later_sweep(tmp_path: Path):
    # A file can have been written by the in-flight provider call the moment
    # cancel landed; the age guard is what keeps a just-finished asset file
    # (marked done after our db read) from ever being deleted.
    project = _project(tmp_path)
    _add_job(
        _store(project),
        "job_1",
        final_status="cancelled",
        assets=[("tts", 1, project / "audio" / "beat_01.wav", "skipped")],
    )
    fresh = project / "audio" / "beat_01.wav"
    fresh.write_bytes(b"maybe in flight")

    sweep_projects(tmp_path / "projects")

    assert fresh.exists()


# --- robustness & reporting ------------------------------------------------------


def test_missing_projects_root_is_a_noop(tmp_path: Path):
    report = sweep_projects(tmp_path / "does_not_exist")

    assert report == SweepReport()


def test_locked_entry_counts_as_failure_and_sweep_continues(tmp_path: Path, monkeypatch):
    project = _project(tmp_path)
    _store(project)
    locked = project / "state" / "scratch" / "job_locked"
    locked.mkdir(parents=True)
    _age(locked)
    other = _project(tmp_path, "other")
    _store(other)
    other_scratch = other / "state" / "scratch" / "job_ok"
    other_scratch.mkdir(parents=True)
    _age(other_scratch)

    real_rmtree = maintenance_module.shutil.rmtree

    def failing_rmtree(path, *args, **kwargs):
        if Path(path) == locked:
            raise OSError("sharing violation")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(maintenance_module.shutil, "rmtree", failing_rmtree)

    report = sweep_projects(tmp_path / "projects")  # must not raise

    assert locked.exists()
    assert not other_scratch.exists()
    assert report.failures == 1
    assert any("job_locked" in detail for detail in report.details)


# --- periodic task (startup + interval, its own thread) --------------------------


def test_task_sweeps_on_start_then_on_interval_and_stop_joins():
    runs: list[int] = []
    task = MaintenanceTask(
        Path("nowhere"), interval_s=0.02, sweep=lambda: runs.append(1) or SweepReport()
    )

    task.start()
    deadline = time.monotonic() + 5.0
    while len(runs) < 3 and time.monotonic() < deadline:
        time.sleep(0.005)
    task.stop()

    assert len(runs) >= 3  # startup sweep + interval sweeps
    assert not task.is_running
    assert task.runs == len(runs)


def test_task_is_idempotent_to_start_and_stop():
    task = MaintenanceTask(Path("nowhere"), interval_s=3600, sweep=SweepReport)

    task.start()
    thread = task._thread
    task.start()  # second start must not spawn a second thread
    task.stop()
    task.stop()  # second stop is a no-op

    assert not thread.is_alive()
    assert not task.is_running


def test_task_sweep_once_returns_report_and_records_failures():
    calls: list[int] = []
    boom = RuntimeError("sweep exploded")

    def sweep():
        calls.append(1)
        if len(calls) == 1:
            return SweepReport(removed_files=2)
        raise boom

    task = MaintenanceTask(Path("nowhere"), sweep=sweep)

    first = task.sweep_once()
    assert first.removed_files == 2
    assert task.last_report is first
    second = task.sweep_once()
    assert second.removed_files == 0  # error swallowed, empty report returned
    assert "sweep exploded" in (task.last_error or "")
    assert task.runs == 2


def test_task_default_sweep_targets_projects_root(tmp_path: Path):
    task = MaintenanceTask(tmp_path / "projects", interval_s=3600)
    report = task.sweep_once()  # real sweep path, missing root — must not raise

    assert report == SweepReport()
    assert task.last_error is None


# --- app wiring: startup + shutdown via lifespan ---------------------------------


def test_create_app_runs_maintenance_across_the_lifespan(tmp_path: Path):
    app = create_app(projects_root=tmp_path / "projects")
    task = app.state.maintenance
    assert isinstance(task, MaintenanceTask)

    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert task.is_running  # startup sweep + interval loop are live

    assert not task.is_running  # shutdown stops maintenance before the worker


def test_create_app_can_disable_maintenance(tmp_path: Path):
    app = create_app(projects_root=tmp_path / "projects", maintenance_enabled=False)

    assert app.state.maintenance is None
