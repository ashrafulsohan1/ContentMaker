"""Seam: per-asset retry/regen on REAL providers (BUILD_PLAN 4.4, spec §6.6).

The retry machinery itself is older than this task — ``JobEngine.retry_asset``
(1.5), the monitor's per-asset retry buttons (1.11) and the regen-then-reassemble
flow (1.12) are pinned at mocked level by test_jobs.py / test_sse_monitor.py /
test_preview_export.py. Task 4.4 is the real-provider + API proof:

- **The JSON API carries the mutation.** ``POST
  /api/jobs/{job_id}/assets/{asset_id}/retry`` re-queues one failed|skipped
  asset — the JSON surface was read-only before 4.4. Same contract as the
  engine: 404 unknown job/asset, 409 when the asset is not failed|skipped,
  belongs to another job, or the job is cancelled (reopen_for_retry refuses).
- **A retry rides the real Gemini adapters** (TTS / image / video, offline via
  httpx.MockTransport) and must not redo already-paid assets — on the wire:
  the provider is called for the retried beat only (done beats are never
  re-POSTed), and a retried call is a fresh, idempotent re-POST (same body as
  the failed one — the adapters keep no session to resume).
- **Money**: only the successful call is charged (the failed attempt costs
  nothing), ``spent_usd`` equals the sum of the per-asset estimates, and the
  spend log carries exactly one line per asset with the real provider id.
- **Style consistency survives regeneration** (§6.3 hard requirement): the
  image adapter's anchor semantics hold across a retry — the first
  *successful* render anchors the directory, so retrying the failed first
  beat anchors it to the surviving paid beat, and the retried request
  attaches that reference image.
- **The monitor form drives the same engine path on a real-provider job**
  (UI surface of 4.4; the JSON surface is tested directly).

Jobs are driven through the engine directly (the flow's options step is not
under test here); the app is created with a dedicated assembler-less engine —
deliverable re-assembly after a retry is 1.12's pinned seam, and here the
provider wire is the truth under test.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.jobs import AssetSpec, JobEngine
from app.engines.base import ProviderError
from app.engines.mocks import MockImageProvider, MockTTSProvider, MockVideoProvider
from app.engines.registry import Registry
from app.main import create_app
from app.providers.gemini.client import GeminiClient
from app.providers.gemini.image import GeminiImageProvider
from app.providers.gemini.tts import GeminiTTSProvider
from app.providers.gemini.video import GeminiVideoProvider

TERMINAL = {"completed", "partial", "failed", "cancelled"}

KEY = "AIza" + "0123456789abcdef" * 3  # realistic shape, never a real key
INTERACTIONS_PATH = "/v1beta/interactions"

#: Recorded response shapes (research spike): audio/image blocks are inline
#: base64; video rides the Files API (uri delivery → metadata poll → download).
WAV_BYTES = b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00" + b"\x00" * 16
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-but-provider-shaped"
MP4_BYTES = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 8
FILE_ID = "abc123video"
FILE_URI = f"https://generativelanguage.googleapis.com/v1beta/files/{FILE_ID}:download?alt=media"


def b64(payload: bytes) -> str:
    return base64.b64encode(payload).decode("ascii")


def interaction_envelope(block: dict[str, Any]) -> dict[str, Any]:
    """The unary Interactions envelope (spike §1.1): one model_output step."""
    return {
        "id": "v1_test_interaction",
        "object": "interaction",
        "status": "completed",
        "steps": [{"type": "model_output", "content": [block]}],
    }


def audio_ok() -> httpx.Response:
    return httpx.Response(
        200,
        json=interaction_envelope(
            {"type": "audio", "mime_type": "audio/wav", "data": b64(WAV_BYTES)}
        ),
    )


def image_ok() -> httpx.Response:
    return httpx.Response(
        200,
        json=interaction_envelope(
            {"type": "image", "mime_type": "image/png", "data": b64(PNG_BYTES)}
        ),
    )


# --- harness ------------------------------------------------------------------------


class FakeProbe:
    """Stands in for ffprobe offline; records every path it was asked about."""

    def __init__(self, duration_s: float = 2.5) -> None:
        self.duration_s = duration_s
        self.probed: list[Path] = []

    def __call__(self, path: Path | str) -> float:
        self.probed.append(Path(path))
        return self.duration_s


class FakeClock:
    """Deterministic monotonic clock + no-op sleep for the Veo poll loop."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        self.now += 0.05
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


class Wire:
    """Recording MockTransport handler with once-only permanent failures.

    ``fail_once`` maps a request-body needle → how many POSTs carrying it are
    answered with a permanent Gemini 400 (HTTP status decides: not retryable —
    the asset fails immediately, exactly the "user retries later" scenario)
    before the real handler answers. Every request is recorded.
    """

    def __init__(
        self,
        respond: Callable[[httpx.Request], httpx.Response],
        fail_once: dict[str, int] | None = None,
    ) -> None:
        self.respond = respond
        self.fail_once = dict(fail_once or {})
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "POST" and request.url.path == INTERACTIONS_PATH:
            body = request.content.decode("utf-8")
            for needle, remaining in self.fail_once.items():
                if remaining and needle in body:
                    self.fail_once[needle] -= 1
                    return httpx.Response(
                        400,
                        json={
                            "error": {
                                "code": 400,
                                "message": f"simulated permanent failure ({needle})",
                            }
                        },
                    )
        return self.respond(request)

    def post_count(self, needle: str) -> int:
        """How many generation POSTs carried ``needle`` (beat text/prompt)."""
        return sum(
            1 for r in self.requests if r.method == "POST" and needle in r.content.decode("utf-8")
        )

    def post_bodies(self, needle: str) -> list[dict[str, Any]]:
        return [
            json.loads(r.content.decode("utf-8"))
            for r in self.requests
            if r.method == "POST" and needle in r.content.decode("utf-8")
        ]


class Harness:
    """App + dedicated assembler-less engine over one registry (a context
    manager: ``with Harness(tmp_path, registry) as h:`` stops the worker)."""

    def __init__(self, tmp_path: Path, registry: Registry) -> None:
        self.root = tmp_path
        app = create_app(
            registry=registry,
            projects_root=tmp_path / "projects",
            maintenance_enabled=False,
        )
        app.state.jobs.stop()  # replaced below — this harness drives its own engine
        self.engine = JobEngine(registry, projects_root=tmp_path / "projects")
        app.state.jobs = self.engine
        self.client = TestClient(app)

    def __enter__(self) -> Harness:
        return self

    def __exit__(self, *exc: object) -> None:
        self.engine.stop()

    def project_dir(self, project: str) -> Path:
        return self.root / "projects" / project

    def spend_log_lines(self, project: str) -> list[dict[str, Any]]:
        path = self.project_dir(project) / "state" / "spend_log.jsonl"
        return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line]

    def run_to_terminal(
        self,
        project: str,
        assets: list[AssetSpec],
        *,
        providers: dict[str, str] | None = None,
    ) -> Any:
        job = self.engine.create_job(project, assets, providers=providers)
        self.engine.estimate(job.id)
        self.engine.start(job.id)
        job = self.engine.wait(job.id, TERMINAL)
        self.engine.wait_idle()
        return job

    def failed_asset_id(self, job: Any) -> int:
        return next(a.id for a in job.assets if a.status == "failed")


def mock_registry(fail_tts: MockTTSProvider | None = None) -> Registry:
    registry = Registry()
    registry.register("tts", fail_tts if fail_tts is not None else MockTTSProvider())
    registry.register("image", MockImageProvider())
    registry.register("video", MockVideoProvider())
    return registry


class BeatFailTTS(MockTTSProvider):
    """Mock TTS that fails a beat's FIRST call permanently, then succeeds."""

    provider_id = "mock"

    def __init__(self) -> None:
        self.failures_left: dict[str, int] = {}

    def fail_once(self, out_path: Path) -> None:
        self.failures_left[out_path.name] = 1

    def generate(self, request):  # noqa: ANN001, ANN201 — mirrors the mock signature
        if self.failures_left.get(request.out_path.name):
            self.failures_left[request.out_path.name] -= 1
            raise ProviderError(self.provider_id, "simulated permanent failure", retryable=False)
        return super().generate(request)


def tts_assets(root: Path, project: str, texts: dict[int, str]) -> list[AssetSpec]:
    return [
        AssetSpec(
            kind="tts",
            beat_index=index,
            out_path=root / "projects" / project / "audio" / f"beat_{index:02d}.wav",
            params={"text": text, "voice": "Kore"},
        )
        for index, text in texts.items()
    ]


def spent_estimates(job: Any) -> float:
    return sum(float(a.estimated_cost_usd) for a in job.assets)


# --- JSON API: the retry mutation (BUILD_PLAN 4.4) -----------------------------------


class TestJsonRetryEndpoint:
    @pytest.fixture()
    def partial(self, tmp_path):
        """A partial job on the mock TTS (beat 2 failed once, others done)."""
        tts = BeatFailTTS()
        tts.fail_once(tmp_path / "projects" / "demo" / "audio" / "beat_02.wav")
        with Harness(tmp_path, mock_registry(tts)) as h:
            job = h.run_to_terminal(
                "demo", tts_assets(h.root, "demo", {1: "One.", 2: "Two.", 3: "Three."})
            )
            assert job.status == "partial"
            yield h, job, tts

    def test_requeues_failed_asset_and_returns_the_job(self, partial):
        h, job, tts = partial
        failed_id = h.failed_asset_id(job)
        resp = h.client.post(f"/api/jobs/{job.id}/assets/{failed_id}/retry")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == job.id
        assert body["status"] == "running"  # terminal → running (sanctioned re-open)
        row = next(a for a in body["assets"] if a["id"] == failed_id)
        assert row["error"] is None  # the failure is cleared on re-queue
        assert row["status"] in {"pending", "generating"}  # re-queued for the worker
        final = h.engine.wait(job.id, TERMINAL)
        assert final.status == "completed"  # the retry succeeded (failure was once)

    def test_payload_matches_the_read_api_shape(self, partial):
        h, job, _ = partial
        failed_id = h.failed_asset_id(job)
        resp = h.client.post(f"/api/jobs/{job.id}/assets/{failed_id}/retry")
        keys = set(resp.json())
        assert keys == set(h.client.get(f"/api/jobs/{job.id}").json())

    def test_unknown_job_is_404(self, partial):
        h, _, _ = partial
        resp = h.client.post("/api/jobs/job_nope/assets/1/retry")
        assert resp.status_code == 404
        assert "job_nope" in resp.json()["detail"]

    def test_unknown_asset_is_404(self, partial):
        h, job, _ = partial
        resp = h.client.post(f"/api/jobs/{job.id}/assets/99999/retry")
        assert resp.status_code == 404
        assert "99999" in resp.json()["detail"]

    def test_done_asset_is_409(self, partial):
        h, job, _ = partial
        done_id = next(a.id for a in job.assets if a.status == "done")
        resp = h.client.post(f"/api/jobs/{job.id}/assets/{done_id}/retry")
        assert resp.status_code == 409
        assert "only failed|skipped" in resp.json()["detail"]

    def test_asset_of_another_job_is_409(self, partial):
        h, job, _ = partial
        # Asset ids are per-project (each project has its own job.db), so the
        # cross-job case needs a second job in the SAME project.
        other = h.run_to_terminal("demo", tts_assets(h.root, "demo", {1: "Other."}))
        resp = h.client.post(f"/api/jobs/{job.id}/assets/{other.assets[0].id}/retry")
        assert resp.status_code == 409
        assert "does not belong" in resp.json()["detail"]

    def test_cancelled_job_is_409(self, tmp_path):
        with Harness(tmp_path, mock_registry()) as h:
            job = h.engine.create_job("demo", tts_assets(h.root, "demo", {1: "One.", 2: "Two."}))
            h.engine.estimate(job.id)
            h.engine.cancel(job.id)  # never started → every asset skipped
            skipped_id = next(a.id for a in h.engine.job(job.id).assets)
            resp = h.client.post(f"/api/jobs/{job.id}/assets/{skipped_id}/retry")
            assert resp.status_code == 409
            assert "cancelled" in resp.json()["detail"]


# --- real Gemini TTS: a retry re-POSTs only the failed beat --------------------------


class TestRealProviderTtsRetry:
    def test_retry_reruns_only_the_failed_beat_on_the_real_tts_provider(self, tmp_path):
        texts = {
            1: "First beat reads aloud here.",
            2: "Second beat reads aloud here.",
            3: "Third beat reads aloud here.",
        }
        wire = Wire(lambda req: audio_ok(), fail_once={texts[2]: 1})
        tts = GeminiTTSProvider(
            GeminiClient(KEY, transport=httpx.MockTransport(wire)), probe=FakeProbe()
        )
        registry = Registry()
        registry.register("tts", tts)
        with Harness(tmp_path, registry) as h:
            job = h.run_to_terminal(
                "demo", tts_assets(h.root, "demo", texts), providers={"tts": "gemini"}
            )
            assert job.status == "partial"
            spent_after_partial = job.spent_usd
            paid_bytes = {
                a.id: h.project_dir("demo").joinpath(*a.out_path.parts[-2:]).read_bytes()
                for a in job.assets
                if a.status == "done"
            }
            assert all(count == 1 for count in [wire.post_count(t) for t in texts.values()])

            failed_id = h.failed_asset_id(job)
            resp = h.client.post(f"/api/jobs/{job.id}/assets/{failed_id}/retry")
            assert resp.status_code == 200

            final = h.engine.wait(job.id, TERMINAL)
            h.engine.wait_idle()
            assert final.status == "completed"
            assert all(a.status == "done" for a in final.assets)

            # On the wire: paid beats were never re-called; the failed one twice.
            assert wire.post_count(texts[1]) == 1
            assert wire.post_count(texts[2]) == 2
            assert wire.post_count(texts[3]) == 1
            # The retry is an idempotent re-POST — same body as the failed call
            # (the adapter keeps no session to resume, §6.6 regen = fresh call).
            bodies = wire.post_bodies(texts[2])
            assert bodies[0] == bodies[1]
            assert bodies[1]["input"][0]["content"][0]["text"] == texts[2]
            assert bodies[1]["generation_config"] == {"speech_config": [{"voice": "Kore"}]}

            # Paid assets' files were never overwritten by the retry.
            for asset in final.assets:
                if asset.id in paid_bytes:
                    path = h.project_dir("demo").joinpath(*asset.out_path.parts[-2:])
                    assert path.read_bytes() == paid_bytes[asset.id]

            # Money: only the successful call is charged — the failed attempt
            # cost nothing, so spent == sum of the per-asset estimates exactly.
            assert float(final.spent_usd) == pytest.approx(spent_estimates(final))
            assert float(final.spent_usd) == pytest.approx(
                float(spent_after_partial) + float(final.assets[1].estimated_cost_usd)
            )
            for asset in final.assets:
                assert float(asset.spent_usd) == pytest.approx(float(asset.estimated_cost_usd))
            lines = h.spend_log_lines("demo")
            assert sorted(line["asset_id"] for line in lines) == sorted(a.id for a in final.assets)
            assert all(line["provider_id"] == "gemini" for line in lines)
            assert sum(line["amount_usd"] for line in lines) == pytest.approx(
                float(final.spent_usd)
            )


# --- real Gemini image: retry keeps the §6.3 anchor semantics ------------------------


class TestRealProviderImageRetry:
    def test_retrying_the_failed_first_beat_anchors_to_the_surviving_paid_one(self, tmp_path):
        prompts = {1: "Neon skyline establishing shot", 2: "Neon skyline alley follow shot"}
        wire = Wire(lambda req: image_ok(), fail_once={prompts[1]: 1})
        image = GeminiImageProvider(GeminiClient(KEY, transport=httpx.MockTransport(wire)))
        registry = Registry()
        registry.register("image", image)
        visuals = tmp_path / "projects" / "demo" / "visuals"
        assets = [
            AssetSpec(
                kind="image",
                beat_index=index,
                out_path=visuals / f"beat_{index:02d}.png",
                params={
                    "prompt": prompt,
                    "style_hint": "moody neon",
                    "width": 1024,
                    "height": 1024,
                },
            )
            for index, prompt in prompts.items()
        ]
        with Harness(tmp_path, registry) as h:
            job = h.run_to_terminal("demo", assets, providers={"image": "gemini"})
            assert job.status == "partial"

            # Beat 2 ran after nothing had succeeded → its one POST carries no
            # reference image; it is the directory's anchor now.
            second_bodies = wire.post_bodies(prompts[2])
            assert len(second_bodies) == 1
            content = second_bodies[0]["input"][0]["content"]
            assert [block["type"] for block in content] == ["text"]
            anchor_path = image.anchor_for(visuals)
            assert anchor_path is not None and anchor_path.name == "beat_02.png"
            paid_bytes = (visuals / "beat_02.png").read_bytes()

            failed_id = h.failed_asset_id(job)
            resp = h.client.post(f"/api/jobs/{job.id}/assets/{failed_id}/retry")
            assert resp.status_code == 200
            final = h.engine.wait(job.id, TERMINAL)
            h.engine.wait_idle()
            assert final.status == "completed"

            # The retried beat-1 POST is anchored to the surviving paid beat:
            # the reference block carries beat_02's exact bytes (§6.3 — a
            # regeneration must not drift from the visuals it joins).
            retried = wire.post_bodies(prompts[1])
            assert len(retried) == 2
            content = retried[1]["input"][0]["content"]
            assert [block["type"] for block in content] == ["text", "image"]
            assert content[1]["data"] == b64(paid_bytes)
            assert "style anchor" in content[0]["text"]
            # The paid beat was never re-rendered, and its file is untouched.
            assert wire.post_count(prompts[2]) == 1
            assert (visuals / "beat_02.png").read_bytes() == paid_bytes

            assert float(final.spent_usd) == pytest.approx(spent_estimates(final))
            lines = h.spend_log_lines("demo")
            assert sorted(line["asset_id"] for line in lines) == sorted(a.id for a in final.assets)
            assert all(line["provider_id"] == "gemini" for line in lines)


# --- real Veo: a retry re-POSTs a fresh interaction, paid clips stay -----------------


class TestRealProviderVideoRetry:
    def test_retry_reposts_a_fresh_interaction_and_never_redoes_the_paid_clip(self, tmp_path):
        prompts = {1: "Lighthouse at dawn, waves crashing", 2: "Lighthouse at dusk, gulls wheeling"}

        def respond(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and ":download" in request.url.path:
                return httpx.Response(200, content=MP4_BYTES)
            if request.method == "GET" and request.url.path.startswith("/v1beta/files/"):
                return httpx.Response(
                    200,
                    json={
                        "name": f"files/{FILE_ID}",
                        "state": "ACTIVE",
                        "uri": FILE_URI,
                        "downloadUri": FILE_URI,
                    },
                )
            return httpx.Response(
                200,
                json=interaction_envelope(
                    {"type": "video", "mime_type": "video/mp4", "uri": FILE_URI}
                ),
            )

        wire = Wire(respond, fail_once={prompts[2]: 1})
        clock = FakeClock()
        video = GeminiVideoProvider(
            GeminiClient(KEY, transport=httpx.MockTransport(wire)),
            probe=FakeProbe(),
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        registry = Registry()
        registry.register("video", video)
        visuals = tmp_path / "projects" / "demo" / "visuals"
        assets = [
            AssetSpec(
                kind="video",
                beat_index=index,
                out_path=visuals / f"beat_{index:02d}.mp4",
                params={"prompt": prompt, "duration_s": 8.0, "width": 1920, "height": 1080},
            )
            for index, prompt in prompts.items()
        ]
        with Harness(tmp_path, registry) as h:
            job = h.run_to_terminal("demo", assets, providers={"video": "gemini"})
            assert job.status == "partial"
            paid_bytes = (visuals / "beat_01.mp4").read_bytes()

            failed_id = h.failed_asset_id(job)
            resp = h.client.post(f"/api/jobs/{job.id}/assets/{failed_id}/retry")
            assert resp.status_code == 200
            final = h.engine.wait(job.id, TERMINAL)
            h.engine.wait_idle()
            assert final.status == "completed"

            # Wire: the paid clip's creation POST happened exactly once; the
            # failed beat was POSTed twice; no polling sleeps leaked into the
            # test wall clock (the injected clock took them).
            assert wire.post_count(prompts[1]) == 1
            assert wire.post_count(prompts[2]) == 2
            # A retry is a FRESH interaction — byte-identical request shape,
            # no interaction/task reuse, no background flags (4.1's contract).
            bodies = wire.post_bodies(prompts[2])
            assert bodies[0] == bodies[1]
            for body in bodies:
                assert not {"task", "store", "stream"} & body.keys()
            # The paid clip's file was never overwritten by the retry.
            assert (visuals / "beat_01.mp4").read_bytes() == paid_bytes

            # Money: each clip paid exactly once.
            assert float(final.spent_usd) == pytest.approx(spent_estimates(final))
            lines = h.spend_log_lines("demo")
            assert sorted(line["asset_id"] for line in lines) == sorted(a.id for a in final.assets)
            assert all(line["provider_id"] == "gemini" for line in lines)


# --- the monitor form drives the same path on a real-provider job (UI) ---------------


class TestMonitorFormOnRealProviderJob:
    def test_form_retry_reattaches_and_completes_the_real_tts_job(self, tmp_path):
        texts = {1: "First beat reads aloud here.", 2: "Second beat reads aloud here."}
        wire = Wire(lambda req: audio_ok(), fail_once={texts[2]: 1})
        tts = GeminiTTSProvider(
            GeminiClient(KEY, transport=httpx.MockTransport(wire)), probe=FakeProbe()
        )
        registry = Registry()
        registry.register("tts", tts)
        with Harness(tmp_path, registry) as h:
            job = h.run_to_terminal(
                "demo", tts_assets(h.root, "demo", texts), providers={"tts": "gemini"}
            )
            assert job.status == "partial"
            failed_id = h.failed_asset_id(job)

            resp = h.client.post(f"/jobs/{job.id}/assets/{failed_id}/retry", follow_redirects=False)
            assert resp.status_code == 303
            assert resp.headers["location"] == f"/jobs/{job.id}"

            final = h.engine.wait(job.id, TERMINAL)
            h.engine.wait_idle()
            assert final.status == "completed"
            assert wire.post_count(texts[1]) == 1  # the paid beat stayed paid
            assert wire.post_count(texts[2]) == 2  # only beat 2 was re-called
