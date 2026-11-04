"""Seam: SSE live progress + job monitor UI (BUILD_PLAN 1.11) — TestClient only.

Pins spec §6.6/§8 at the HTTP level:

- ``GET /api/jobs/{id}/events`` streams ``text/event-stream`` full-snapshot
  events: a ``snapshot`` on every connect (so a reload or reconnect reattaches
  to the running job, §6.6), ``update`` on every persisted change, and a
  ``final`` event before the stream closes once the job reaches a terminal
  status. The payload is the same JSON as ``/api/jobs/{id}`` plus a progress
  summary, so clients render truth-as-of-now with no catch-up logic.
- ``GET /jobs/{id}`` is the monitor page: per-asset live status rows, overall
  progress (done/total + percent), running spend vs cap, a cancel button while
  the job is cancellable, per-asset retry buttons on failed|skipped rows, and
  the SSE client wiring (data-job-id + /static/monitor.js). The page is always
  rendered fresh from the store, so a plain reload reattaches even without JS.
- ``POST /jobs/{id}/cancel`` and ``POST /jobs/{id}/assets/{asset_id}/retry``
  drive ``JobEngine.cancel`` / ``retry_asset`` (§6.6: cancel keeps completed
  assets and skips never-started ones; retry re-runs one failed|skipped asset
  without redoing done ones; a cancelled job is terminal and refuses retries).
  Both answer a plain form with a 303 redirect and an ``HX-Request`` with the
  updated ``#monitor`` fragment.

The dry-run "mock" TTS is replaced through the registry (same-id re-register
replaces, pinned in 1.4) by a ScriptedTTS whose generate() calls can be held
on per-call gates — making per-asset progress, reattach and cancel-during-call
deterministically observable — or fail non-retryably to produce a partial job.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient

from app.core.jobs import AssetSpec
from app.engines.base import ProviderError, TTSRequest, TTSResult
from app.engines.mocks import MockImageProvider, MockTTSProvider, MockVideoProvider
from app.engines.registry import Registry
from app.main import create_app

SCRIPT = (
    "First beat reads aloud here.\n\n"
    "Second beat reads aloud here.\n\n"
    "Third beat reads aloud here."
)

AUDIO_IMAGES = {
    "platform_id": "instagram",
    "format_id": "instagram_feed_square",
    "audio": "on",
    "visual": "images",
    "dry_run": "on",
}

TERMINAL = {"completed", "partial", "failed", "cancelled"}


# --- fakes --------------------------------------------------------------------


class ScriptedTTS:
    """Dry-run stand-in for the mock TTS (provider_id "mock" replaces it).

    - ``hold(n)`` blocks the next ``n`` generate() invocations, each until its
      own ``unblock()`` (oldest blocked call first) — per-asset progress,
      reattach and cancel-during-call become deterministic.
    - ``mode`` "fail" raises a non-retryable ProviderError (asset fails, job
      partial — the per-asset retry path); "ok" writes a stub file.
    """

    provider_id = "mock"

    def __init__(self) -> None:
        self.mode = "ok"
        self.calls = 0
        self._held = 0  # upcoming generate() calls that must block
        self._credits = 0  # unblock() grants for held calls not waiting yet
        self._waiting: deque[threading.Event] = deque()  # calls blocked right now
        self._lock = threading.Lock()

    def hold(self, calls: int) -> None:
        with self._lock:
            self._held += calls

    def unblock(self) -> None:
        """Release one held call: the oldest blocked one, or a credit for the
        next gated call if none is waiting yet (the worker may still be on its
        way to the gate — unblock() must never be lost)."""
        with self._lock:
            if self._waiting:
                gate: threading.Event | None = self._waiting.popleft()
            elif self._held > 0:
                self._held -= 1  # the credit consumes the hold it releases
                self._credits += 1
                gate = None
            else:
                gate = None
        if gate is not None:
            gate.set()

    def generate(self, request: TTSRequest) -> TTSResult:
        with self._lock:
            self.calls += 1
            gate: threading.Event | None = None
            if self._credits > 0:
                self._credits -= 1
            elif self._held > 0:
                self._held -= 1
                gate = threading.Event()
                self._waiting.append(gate)
        if gate is not None and not gate.wait(timeout=30.0):
            with self._lock:
                try:
                    self._waiting.remove(gate)
                except ValueError:
                    pass
            raise ProviderError(self.provider_id, "gate never released", retryable=False)
        if self.mode == "fail":
            raise ProviderError(self.provider_id, "simulated TTS outage", retryable=False)
        request.out_path.parent.mkdir(parents=True, exist_ok=True)
        request.out_path.write_bytes(b"RIFF scripted")
        return TTSResult(file=request.out_path, duration_s=1.0)

    def estimate_cost_usd(self, request: TTSRequest) -> float:
        return 0.0


class CountingImage(MockImageProvider):
    """Real mock PNG generation with a call counter — proves done assets are
    never regenerated by per-asset retries."""

    provider_id = "mock"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, request):  # noqa: ANN001, ANN201 — mirrors the mock signature
        self.calls += 1
        return super().generate(request)


# --- helpers ------------------------------------------------------------------


def make_client(
    tmp_path,
    *,
    tts: Any = None,
    image: Any = None,
    sse_poll_s: float = 0.02,
) -> TestClient:
    registry = Registry()
    registry.register("tts", tts if tts is not None else MockTTSProvider())
    registry.register("image", image if image is not None else MockImageProvider())
    registry.register("video", MockVideoProvider())
    app = create_app(
        registry=registry,
        projects_root=tmp_path / "projects",
        sse_poll_s=sse_poll_s,
    )
    return TestClient(app)


def new_job(client: TestClient, *, project: str = "demo") -> str:
    """Walk input → options; return the estimated job id (awaiting_confirmation)."""
    resp = client.post(
        "/input", data={"project": project, "script": SCRIPT}, follow_redirects=False
    )
    assert resp.status_code == 303, resp.text
    resp = client.post("/options", data=AUDIO_IMAGES, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    return resp.headers["location"].rsplit("/", 1)[1]


def job_payload(client: TestClient, job_id: str) -> dict:
    return client.get(f"/api/jobs/{job_id}").json()


def job_asset(client: TestClient, job_id: str, index: int) -> dict:
    return job_payload(client, job_id)["assets"][index]


def wait_for(pred: Callable[[], bool], *, timeout_s: float = 15.0, message: str = "") -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.05)
    raise AssertionError(message or "condition never became true")


def wait_terminal(client: TestClient, job_id: str, *, timeout_s: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout_s
    payload: dict = {}
    while time.monotonic() < deadline:
        payload = job_payload(client, job_id)
        if payload["status"] in TERMINAL:
            return payload
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never reached a terminal status: {payload}")


class SseStream:
    """One continuous reader over an httpx SSE streaming response.

    An httpx response can only be iterated once (StreamConsumed), so a single
    daemon thread parses every line into ``events`` while the test thread
    samples that list. All waits are bounded — a stalled stream fails the test
    with what arrived instead of hanging it.
    """

    def __init__(self, resp) -> None:
        self.events: list[tuple[str | None, dict]] = []
        self.finished = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._harvest, args=(resp,), daemon=True)
        self._thread.start()

    def _harvest(self, resp) -> None:
        name: str | None = None
        data_lines: list[str] = []
        try:
            for line in resp.iter_lines():
                if line.startswith(":"):
                    continue  # keep-alive comment
                if line.startswith("event:"):
                    name = line[len("event:"):].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[len("data:"):].strip())
                elif line == "" and (name is not None or data_lines):
                    with self._lock:
                        self.events.append((name, json.loads("\n".join(data_lines))))
                    name, data_lines = None, []
            if name is not None or data_lines:  # stream ended mid-event
                with self._lock:
                    self.events.append((name, json.loads("\n".join(data_lines))))
        except Exception:  # response closed under us (disconnect path)
            pass
        finally:
            self.finished.set()

    def snapshot(self) -> list[tuple[str | None, dict]]:
        with self._lock:
            return list(self.events)

    def require(self, pred, *, timeout_s: float = 15.0, message: str = "") -> list:
        """Wait until ``pred(events)`` holds; return the events seen so far."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            events = self.snapshot()
            if pred(events):
                return events
            time.sleep(0.02)
        raise AssertionError(
            message or f"SSE condition not met within {timeout_s}s; events: {self.snapshot()}"
        )


def monitor_rows(html: str) -> dict[str, str]:
    """Asset id → row HTML for the monitor table."""
    return dict(re.findall(r'<tr data-asset-id="(\d+)">(.*?)</tr>', html, re.DOTALL))


def row_status(row_html: str) -> str:
    match = re.search(r'class="asset-status">([a-z_]+)<', row_html)
    return match.group(1) if match else ""


def retry_hidden(row_html: str, job_id: str, asset_id: str) -> bool | None:
    """Whether the row's retry form carries ``hidden``; None when absent."""
    match = re.search(
        rf'<form[^>]*action="/jobs/{re.escape(job_id)}/assets/{asset_id}/retry"[^>]*>',
        row_html,
        re.DOTALL,
    )
    if match is None:
        return None
    return "hidden" in match.group(0)


# --- SSE endpoint ---------------------------------------------------------------


class TestSseEndpoint:
    def test_unknown_job_is_404(self, tmp_path):
        client = make_client(tmp_path)
        resp = client.get("/api/jobs/job_nope/events")
        assert resp.status_code == 404
        assert resp.json()["detail"]

    def test_stream_headers_and_first_snapshot(self, tmp_path):
        client = make_client(tmp_path)
        job_id = new_job(client)
        with client.stream("GET", f"/api/jobs/{job_id}/events") as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            assert resp.headers["cache-control"] == "no-cache"
            stream = SseStream(resp)
            events = stream.require(lambda evs: len(evs) >= 1)
        assert events[0][0] == "snapshot"
        assert events[0][1]["id"] == job_id
        assert events[0][1]["status"] == "awaiting_confirmation"

    def test_snapshot_carries_full_payload_with_progress(self, tmp_path):
        client = make_client(tmp_path)
        job_id = new_job(client)
        with client.stream("GET", f"/api/jobs/{job_id}/events") as resp:
            stream = SseStream(resp)
            events = stream.require(lambda evs: len(evs) >= 1)
        payload = events[0][1]
        assert {
            "id",
            "project",
            "status",
            "spent_usd",
            "estimate_usd",
            "assets",
            "progress",
        } <= set(payload)
        assert {"id", "kind", "beat_index", "status", "params"} <= set(payload["assets"][0])
        assert payload["progress"] == {"total": 6, "done": 0, "percent": 0}

    def test_stream_reports_updates_then_final_and_closes(self, tmp_path):
        tts = ScriptedTTS()
        tts.hold(2)  # gate tts beat 1, then tts beat 2 — progress in two steps
        client = make_client(tmp_path, tts=tts)
        job_id = new_job(client)
        client.post(f"/jobs/{job_id}/confirm", follow_redirects=False)
        wait_for(
            lambda: job_asset(client, job_id, 0)["status"] == "generating",
            message="first asset never started generating",
        )
        with client.stream("GET", f"/api/jobs/{job_id}/events") as resp:
            stream = SseStream(resp)
            first = stream.require(lambda evs: len(evs) >= 1)
            assert first[0][0] == "snapshot"
            assert first[0][1]["status"] == "running"
            assert first[0][1]["assets"][0]["status"] == "generating"
            tts.unblock()  # asset 1 finishes, image finishes, tts beat 2 gates
            events = stream.require(
                lambda evs: bool(evs) and evs[-1][0] == "update",
                message="no update event after the first asset finished",
            )
            assert events[-1][1]["progress"]["done"] >= 1
            tts.unblock()  # the rest completes
            events = stream.require(
                lambda evs: bool(evs) and evs[-1][0] == "final",
                message="final event never arrived",
            )
            stream.require(  # the stream must close after the final event
                lambda evs: stream.finished.is_set(),
                message="stream did not close after the final event",
            )
        final = events[-1][1]
        assert final["status"] == "completed"
        assert final["progress"] == {"total": 6, "done": 6, "percent": 100}
        assert all(asset["status"] == "done" for asset in final["assets"])

    def test_terminal_job_connects_to_snapshot_then_final_then_eof(self, tmp_path):
        client = make_client(tmp_path)
        job_id = new_job(client)
        client.post(f"/jobs/{job_id}/confirm")
        wait_terminal(client, job_id)
        with client.stream("GET", f"/api/jobs/{job_id}/events") as resp:
            stream = SseStream(resp)
            stream.require(  # must reach EOF, not hang forever
                lambda evs: stream.finished.is_set(),
                message="stream never closed after a terminal snapshot",
            )
        events = stream.snapshot()
        names = [name for name, _ in events]
        assert names == ["snapshot", "final"]
        assert events[0][1]["status"] == "completed"
        assert events[1][1]["status"] == "completed"

    def test_reattach_after_disconnect_gets_fresh_snapshot(self, tmp_path):
        tts = ScriptedTTS()
        tts.hold(1)
        client = make_client(tmp_path, tts=tts)
        job_id = new_job(client)
        client.post(f"/jobs/{job_id}/confirm", follow_redirects=False)
        wait_for(
            lambda: job_asset(client, job_id, 0)["status"] == "generating",
            message="first asset never started generating",
        )
        with client.stream("GET", f"/api/jobs/{job_id}/events") as resp:
            stream = SseStream(resp)
            first = stream.require(lambda evs: len(evs) >= 1)
            assert first[0][0] == "snapshot"
            assert first[0][1]["assets"][0]["status"] == "generating"
        # tab closed → reload → a fresh connection reattaches to the live state (§6.6)
        with client.stream("GET", f"/api/jobs/{job_id}/events") as resp:
            stream = SseStream(resp)
            second = stream.require(lambda evs: len(evs) >= 1)
            assert second[0][0] == "snapshot"
            assert second[0][1]["assets"][0]["status"] == "generating"
            assert second[0][1]["progress"]["done"] == 0
            tts.unblock()
            rest = stream.require(
                lambda evs: bool(evs) and evs[-1][0] == "final",
                message="final event never arrived after reconnect",
            )
        assert rest[-1][1]["status"] == "completed"


# --- monitor page ----------------------------------------------------------------


class TestMonitorPage:
    def test_renders_rows_progress_spend_and_cancel(self, tmp_path):
        client = make_client(tmp_path)
        job_id = new_job(client)
        page = client.get(f"/jobs/{job_id}")
        assert page.status_code == 200
        assert f'data-job-id="{job_id}"' in page.text
        assert 'data-terminal="0"' in page.text
        assert "0 of 6 assets done (0%)" in page.text
        assert "$0.0000" in page.text  # running spend (dry run: zero)
        assert f'action="/jobs/{job_id}/cancel"' in page.text  # cancellable
        rows = monitor_rows(page.text)
        assert len(rows) == 6
        assert all(row_status(body) == "pending" for body in rows.values())

    def test_cancel_button_disappears_when_terminal(self, tmp_path):
        client = make_client(tmp_path)
        job_id = new_job(client)
        client.post(f"/jobs/{job_id}/confirm")
        wait_terminal(client, job_id)
        page = client.get(f"/jobs/{job_id}")
        assert 'data-terminal="1"' in page.text  # JS must not reconnect on reload
        assert f'action="/jobs/{job_id}/cancel"' not in page.text
        assert ">completed<" in page.text

    def test_retry_forms_on_failed_rows_hidden_on_done_rows(self, tmp_path):
        tts = ScriptedTTS()
        tts.mode = "fail"
        client = make_client(tmp_path, tts=tts)
        job_id = new_job(client)
        client.post(f"/jobs/{job_id}/confirm")
        payload = wait_terminal(client, job_id)
        assert payload["status"] == "partial"
        page = client.get(f"/jobs/{job_id}")
        rows = monitor_rows(page.text)
        assert len(rows) == 6
        for asset_id, body in rows.items():
            status = row_status(body)
            hidden = retry_hidden(body, job_id, asset_id)
            if status in ("failed", "skipped"):
                assert hidden is False, (asset_id, status, body)
            else:
                assert hidden is True, (asset_id, status, body)

    def test_no_retry_forms_on_cancelled_job(self, tmp_path):
        tts = ScriptedTTS()
        tts.hold(1)
        client = make_client(tmp_path, tts=tts)
        job_id = new_job(client)
        client.post(f"/jobs/{job_id}/confirm", follow_redirects=False)
        wait_for(
            lambda: job_asset(client, job_id, 0)["status"] == "generating",
            message="first asset never started generating",
        )
        client.post(f"/jobs/{job_id}/cancel", follow_redirects=False)
        tts.unblock()
        wait_terminal(client, job_id)
        page = client.get(f"/jobs/{job_id}")
        assert ">cancelled<" in page.text
        for asset_id, body in monitor_rows(page.text).items():
            assert retry_hidden(body, job_id, asset_id) is None, (asset_id, body)

    def test_page_and_script_wire_the_sse_client(self, tmp_path):
        client = make_client(tmp_path)
        job_id = new_job(client)
        page = client.get(f"/jobs/{job_id}")
        assert f'data-job-id="{job_id}"' in page.text
        assert '<script src="/static/monitor.js"' in page.text
        js = client.get("/static/monitor.js")
        assert js.status_code == 200
        assert "EventSource" in js.text
        assert "/api/jobs/" in js.text  # builds the events URL from data-job-id

    def test_reload_reattaches_to_running_job(self, tmp_path):
        tts = ScriptedTTS()
        tts.hold(1)
        client = make_client(tmp_path, tts=tts)
        job_id = new_job(client)
        client.post(f"/jobs/{job_id}/confirm", follow_redirects=False)
        wait_for(
            lambda: job_asset(client, job_id, 0)["status"] == "generating",
            message="first asset never started generating",
        )
        live = client.get(f"/jobs/{job_id}")  # reload while the job runs
        assert ">running<" in live.text
        assert "generating" in live.text
        assert "0 of 6 assets done (0%)" in live.text
        tts.unblock()
        wait_terminal(client, job_id)
        done = client.get(f"/jobs/{job_id}")  # same URL renders the fresh state
        assert ">completed<" in done.text
        assert "6 of 6 assets done (100%)" in done.text


# --- cancel + retry routes ---------------------------------------------------------


class TestCancelAndRetryRoutes:
    def test_cancel_running_job_keeps_inflight_and_skips_the_rest(self, tmp_path):
        tts = ScriptedTTS()
        tts.hold(1)
        client = make_client(tmp_path, tts=tts)
        job_id = new_job(client)
        client.post(f"/jobs/{job_id}/confirm", follow_redirects=False)
        wait_for(
            lambda: job_asset(client, job_id, 0)["status"] == "generating",
            message="first asset never started generating",
        )
        resp = client.post(f"/jobs/{job_id}/cancel", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/jobs/{job_id}"
        page = client.get(f"/jobs/{job_id}")
        assert ">cancelled<" in page.text
        assert f'action="/jobs/{job_id}/cancel"' not in page.text
        tts.unblock()  # the in-flight call finishes
        client.app.state.jobs.wait_idle()  # worker bookkeeping, not just the status
        payload = job_payload(client, job_id)
        assert payload["status"] == "cancelled"
        statuses = [asset["status"] for asset in payload["assets"]]
        assert statuses[0] == "done"  # in-flight asset kept (§6.6)
        assert set(statuses[1:]) == {"skipped"}
        assert all("cancelled" in (asset["error"] or "") for asset in payload["assets"][1:])

    def test_cancel_terminal_job_is_409(self, tmp_path):
        client = make_client(tmp_path)
        job_id = new_job(client)
        client.post(f"/jobs/{job_id}/confirm")
        wait_terminal(client, job_id)
        resp = client.post(f"/jobs/{job_id}/cancel", follow_redirects=False)
        assert resp.status_code == 409
        assert "illegal" in resp.text.lower()

    def test_cancel_unknown_job_is_404(self, tmp_path):
        client = make_client(tmp_path)
        resp = client.post("/jobs/job_nope/cancel", follow_redirects=False)
        assert resp.status_code == 404

    def test_cancel_htmx_request_returns_monitor_fragment(self, tmp_path):
        client = make_client(tmp_path)
        job_id = new_job(client)
        resp = client.post(
            f"/jobs/{job_id}/cancel", headers={"HX-Request": "true"}, follow_redirects=False
        )
        assert resp.status_code == 200
        assert "<html" not in resp.text.lower()
        assert 'id="monitor"' in resp.text
        assert ">cancelled<" in resp.text

    def test_retry_failed_asset_without_redoing_done_ones(self, tmp_path):
        tts = ScriptedTTS()
        tts.mode = "fail"
        image = CountingImage()
        client = make_client(tmp_path, tts=tts, image=image)
        job_id = new_job(client)
        client.post(f"/jobs/{job_id}/confirm")
        payload = wait_terminal(client, job_id)
        assert payload["status"] == "partial"
        assert image.calls == 3
        failed = [asset["id"] for asset in payload["assets"] if asset["status"] == "failed"]
        assert len(failed) == 3
        tts.mode = "ok"
        resp = client.post(f"/jobs/{job_id}/assets/{failed[0]}/retry", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/jobs/{job_id}"
        payload = wait_terminal(client, job_id)
        assert payload["status"] == "partial"  # one fixed, two still failed
        fixed = next(a for a in payload["assets"] if a["id"] == failed[0])
        assert fixed["status"] == "done"
        assert image.calls == 3  # done images were never re-run
        for asset_id in failed[1:]:
            resp = client.post(f"/jobs/{job_id}/assets/{asset_id}/retry", follow_redirects=False)
            assert resp.status_code == 303
            payload = wait_terminal(client, job_id)
        assert payload["status"] == "completed"
        assert all(asset["status"] == "done" for asset in payload["assets"])
        assert image.calls == 3

    def test_retry_done_asset_is_409(self, tmp_path):
        client = make_client(tmp_path)
        job_id = new_job(client)
        client.post(f"/jobs/{job_id}/confirm")
        payload = wait_terminal(client, job_id)
        done_id = next(a["id"] for a in payload["assets"] if a["status"] == "done")
        resp = client.post(f"/jobs/{job_id}/assets/{done_id}/retry", follow_redirects=False)
        assert resp.status_code == 409
        assert "only failed|skipped" in resp.text

    def test_retry_unknown_asset_is_404(self, tmp_path):
        client = make_client(tmp_path)
        job_id = new_job(client)
        resp = client.post(f"/jobs/{job_id}/assets/9999/retry", follow_redirects=False)
        assert resp.status_code == 404

    def test_retry_unknown_job_is_404(self, tmp_path):
        client = make_client(tmp_path)
        resp = client.post("/jobs/job_nope/assets/1/retry", follow_redirects=False)
        assert resp.status_code == 404

    def test_retry_asset_of_another_job_is_409(self, tmp_path):
        client = make_client(tmp_path)
        job_a = new_job(client, project="first")
        # Asset ids are per-project (each project has its own job.db), so the
        # cross-job case needs a second job in the SAME project — created on
        # the engine seam the wizard route itself uses.
        engine = client.app.state.jobs
        other = engine.create_job(
            "first",
            [
                AssetSpec(
                    kind="tts",
                    beat_index=1,
                    out_path=tmp_path / "projects" / "first" / "audio" / "beat_01.wav",
                    params={"text": "other job text", "voice": "alloy"},
                )
            ],
            providers={"tts": "mock"},
        )
        asset_other = engine.job(other.id).assets[0].id
        resp = client.post(f"/jobs/{job_a}/assets/{asset_other}/retry", follow_redirects=False)
        assert resp.status_code == 409
        assert "does not belong" in resp.text

    def test_retry_on_cancelled_job_is_409(self, tmp_path):
        tts = ScriptedTTS()
        tts.hold(1)
        client = make_client(tmp_path, tts=tts)
        job_id = new_job(client)
        client.post(f"/jobs/{job_id}/confirm", follow_redirects=False)
        wait_for(
            lambda: job_asset(client, job_id, 0)["status"] == "generating",
            message="first asset never started generating",
        )
        client.post(f"/jobs/{job_id}/cancel", follow_redirects=False)
        skipped = next(
            a["id"] for a in job_payload(client, job_id)["assets"] if a["status"] == "skipped"
        )
        tts.unblock()
        wait_terminal(client, job_id)
        resp = client.post(f"/jobs/{job_id}/assets/{skipped}/retry", follow_redirects=False)
        assert resp.status_code == 409
        assert "cancelled" in resp.text

    def test_retry_htmx_request_returns_monitor_fragment(self, tmp_path):
        tts = ScriptedTTS()
        tts.mode = "fail"
        client = make_client(tmp_path, tts=tts)
        job_id = new_job(client)
        client.post(f"/jobs/{job_id}/confirm")
        payload = wait_terminal(client, job_id)
        failed_id = next(a["id"] for a in payload["assets"] if a["status"] == "failed")
        resp = client.post(
            f"/jobs/{job_id}/assets/{failed_id}/retry", headers={"HX-Request": "true"}
        )
        assert resp.status_code == 200
        assert "<html" not in resp.text.lower()
        assert 'id="monitor"' in resp.text


# --- JSON progress -----------------------------------------------------------------


class TestJsonApiProgress:
    def test_json_api_reports_progress(self, tmp_path):
        client = make_client(tmp_path)
        job_id = new_job(client)
        before = job_payload(client, job_id)
        assert before["progress"] == {"total": 6, "done": 0, "percent": 0}
        client.post(f"/jobs/{job_id}/confirm")
        after = wait_terminal(client, job_id)
        assert after["progress"] == {"total": 6, "done": 6, "percent": 100}
