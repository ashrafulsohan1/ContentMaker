"""Confirm-before-spend + spend caps with the real provider in the loop (BUILD_PLAN 2.5).

The §6.6 guardrail machinery — the ``awaiting_confirmation`` gate, running
spend, and the hard per-job cap — was built in M1 (1.5/1.6) and only ever
exercised against priced test doubles and zero-cost mocks. Task 2.5 pins the
whole chain END-TO-END with the real :class:`GeminiTTSProvider` (2.3, offline
via ``httpx.MockTransport``) driving the job engine, so the M2 gate's
"estimate shown and confirmed before any spend; no network call without
confirmation" holds on the paid path, not just the dry-run path:

- **Estimate before spend**: ``estimate()`` derives the per-beat and total
  numbers from the editable price table (never hardcoded) with zero HTTP
  traffic, and lands the job in ``awaiting_confirmation``; ``start()`` is
  refused before that.
- **Confirmation admits exactly the confirmed batch**: one paid call per
  beat, to the configured model, with the beat text verbatim — and nothing
  before the confirm.
- **Running spend**: ``spent_usd`` accumulates per paid call on the job and
  each asset row, the JSONL spend log stays at parity, and the API payload
  that the monitor/SSE display carries all of it.
- **Hard cap**: a cap that the next paid call would exceed stops paid work
  BEFORE that call (the provider is never dialed), keeps completed beats,
  and marks the job ``partial``; re-trying a cap-skipped beat while the cap
  still binds is skipped again — the cap cannot be sneak-past via retry.
- **Broken estimation fails the job before any paid call** — a batch whose
  cost cannot be computed never falls through to the provider.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest

from app.api.jobs import job_payload
from app.core.cost import load_prices
from app.core.jobs import (
    TERMINAL_JOB_STATUSES,
    AssetSpec,
    IllegalTransition,
    JobEngine,
)
from app.engines.base import TTSRequest, TTSResult
from app.engines.registry import Registry
from app.providers.gemini.client import GeminiClient
from app.providers.gemini.tts import GeminiTTSProvider

KEY = "AIza" + "0123456789abcdef" * 3  # realistic shape, never a real key
INTERACTIONS_PATH = "/v1beta/interactions"

#: The spike-verified unit price for the tts engine — read from the editable
#: table, never hardcoded (BUILD_PLAN 1.6; the tests re-derive every amount).
GEMINI_TTS_PRICE = load_prices().price_for("gemini", "tts").price
GEMINI_TTS_MODEL = load_prices().price_for("gemini", "tts").model

#: 1000 chars of text → exactly one whole 1k-char billing block → PRICE per beat.
BEAT_TEXT = "x" * 1000


# --- harness: the real provider on a recording (offline) transport ------------


class RecordedGemini:
    """The real :class:`GeminiTTSProvider` on an offline recording transport.

    ``requests`` is every HTTP request the provider made — the zero-network
    claims below are asserted against it, not against a mock provider.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        payload = {
            "id": "v1_test_interaction",
            "object": "interaction",
            "status": "completed",
            "model": GEMINI_TTS_MODEL,
            "steps": [
                {
                    "type": "model_output",
                    "content": [
                        {
                            "type": "audio",
                            "mime_type": "audio/wav",
                            "data": base64.b64encode(b"RIFFfake-wav-bytes").decode("ascii"),
                        }
                    ],
                }
            ],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(200, json=payload)

        client = GeminiClient(KEY, transport=httpx.MockTransport(handler))
        self.provider = GeminiTTSProvider(client, probe=lambda _path: 1.5)


@pytest.fixture()
def paid_tts() -> RecordedGemini:
    return RecordedGemini()


@pytest.fixture()
def projects_root(tmp_path: Path) -> Path:
    return tmp_path / "projects"


@pytest.fixture()
def engine(tmp_path: Path, paid_tts: RecordedGemini):
    registry = Registry()
    registry.register("tts", paid_tts.provider)
    eng = JobEngine(
        registry,
        projects_root=tmp_path / "projects",
        max_attempts=2,
        backoff_base_s=0.01,
        sleep=lambda _s: None,
    )
    yield eng
    eng.stop()


def tts_spec(root: Path, index: int, text: str = BEAT_TEXT) -> AssetSpec:
    return AssetSpec(
        kind="tts",
        beat_index=index,
        out_path=root / "audio" / f"beat_{index:02d}.wav",
        params={"text": text, "voice": "Kore"},
    )


def priced_job(
    engine: JobEngine,
    projects_root: Path,
    *,
    beats: int = 3,
    spend_cap_usd: float | None = None,
    provider_id: str = "gemini",
):
    specs = [tts_spec(projects_root / "alpha", i) for i in range(1, beats + 1)]
    return engine.create_job(
        "alpha",
        specs,
        providers={"tts": provider_id},
        spend_cap_usd=spend_cap_usd,
    )


class BrokenEstimate:
    """A provider whose cost cannot be computed — estimation must stop the job."""

    provider_id = "broken"

    def __init__(self) -> None:
        self.generate_calls = 0

    def generate(self, request: TTSRequest) -> TTSResult:
        self.generate_calls += 1
        request.out_path.parent.mkdir(parents=True, exist_ok=True)
        request.out_path.write_bytes(b"fake-wav")
        return TTSResult(file=request.out_path, duration_s=1.0)

    def estimate_cost_usd(self, request: TTSRequest) -> float:
        raise ValueError("no price for this beat's text")


# --- estimate shown before any paid batch --------------------------------------


def test_estimate_shows_prices_json_math_gates_start_and_never_dials(
    engine, projects_root: Path, paid_tts: RecordedGemini
):
    job = priced_job(engine, projects_root)

    # §6.6 gate: the job cannot start before an estimate was confirmed.
    with pytest.raises(IllegalTransition, match="awaiting_confirmation"):
        engine.start(job.id)
    assert paid_tts.requests == []  # refusing the gate never touches the network

    estimated = engine.estimate(job.id)
    assert estimated.status == "awaiting_confirmation"
    assert estimated.estimate_usd == pytest.approx(3 * GEMINI_TTS_PRICE)
    assert [a.estimated_cost_usd for a in estimated.assets] == pytest.approx(
        [GEMINI_TTS_PRICE] * 3
    )
    # Estimation is local math against the price table — zero paid calls.
    assert paid_tts.requests == []


def test_confirmation_admits_exactly_one_paid_call_per_beat(
    engine, projects_root: Path, paid_tts: RecordedGemini
):
    job = priced_job(engine, projects_root)
    engine.estimate(job.id)
    assert paid_tts.requests == []  # nothing before the explicit confirm

    engine.start(job.id)
    final = engine.wait(job.id, TERMINAL_JOB_STATUSES)

    assert final.status == "completed"
    assert len(paid_tts.requests) == 3  # per-beat TTS is a §2 constraint
    for request in paid_tts.requests:
        assert request.method == "POST"
        assert request.url.path.endswith(INTERACTIONS_PATH)
        body = json.loads(request.content.decode("utf-8"))
        assert body["model"] == GEMINI_TTS_MODEL  # model id from prices.json
        assert body["input"][0]["content"][0]["text"] == BEAT_TEXT  # verbatim


# --- running spend tracked (and carried by the display payload) -----------------


def test_running_spend_tracks_each_paid_beat_and_matches_spend_log(
    engine, projects_root: Path, paid_tts: RecordedGemini
):
    job = priced_job(engine, projects_root)
    engine.estimate(job.id)
    engine.start(job.id)
    final = engine.wait(job.id, TERMINAL_JOB_STATUSES)

    assert final.estimate_usd == pytest.approx(final.spent_usd)
    assert final.spent_usd == pytest.approx(3 * GEMINI_TTS_PRICE)
    assert [a.spent_usd for a in final.assets] == pytest.approx([GEMINI_TTS_PRICE] * 3)

    log = engine.spend_log("alpha")
    assert log is not None
    entries = log.entries()
    assert len(entries) == 3
    assert all(e["provider_id"] == "gemini" for e in entries)
    assert all(e["engine"] == "tts" for e in entries)
    assert [e["amount_usd"] for e in entries] == pytest.approx([GEMINI_TTS_PRICE] * 3)
    assert {e["job_id"] for e in entries} == {final.id}
    assert log.total(job_id=final.id) == pytest.approx(final.spent_usd)

    # The payload the monitor and SSE stream display carries the running spend.
    shown = job_payload(engine.job(final.id))
    assert shown["spent_usd"] == pytest.approx(final.spent_usd)
    assert shown["estimate_usd"] == pytest.approx(final.estimate_usd)
    assert shown["spend_cap_usd"] is None
    assert all(a["spent_usd"] == pytest.approx(GEMINI_TTS_PRICE) for a in shown["assets"])


# --- hard per-job cap enforced before each paid call -----------------------------


def test_cap_stops_paid_calls_keeps_completed_and_marks_partial(
    engine, projects_root: Path, paid_tts: RecordedGemini
):
    cap = GEMINI_TTS_PRICE * 1.5  # admits beat 1, blocks beat 2 and 3
    job = priced_job(engine, projects_root, spend_cap_usd=cap)
    engine.estimate(job.id)
    engine.start(job.id)
    final = engine.wait(job.id, TERMINAL_JOB_STATUSES)

    assert final.status == "partial"  # completed beats kept, the rest skipped
    assert [a.status for a in final.assets] == ["done", "skipped", "skipped"]
    assert final.spent_usd == pytest.approx(GEMINI_TTS_PRICE)
    assert len(paid_tts.requests) == 1  # the blocked beats were never dialed
    for asset in final.assets[1:]:
        assert asset.spent_usd == 0.0
        assert asset.error is not None
        assert "spend cap" in asset.error
        assert "skipped before paid call" in asset.error

    log = engine.spend_log("alpha")
    assert log is not None
    assert len(log.entries()) == 1
    assert log.total(job_id=final.id) == pytest.approx(GEMINI_TTS_PRICE)


def test_retrying_a_cap_skipped_asset_stays_blocked_while_cap_binds(
    engine, projects_root: Path, paid_tts: RecordedGemini
):
    cap = GEMINI_TTS_PRICE * 1.5
    job = priced_job(engine, projects_root, spend_cap_usd=cap)
    engine.estimate(job.id)
    engine.start(job.id)
    final = engine.wait(job.id, TERMINAL_JOB_STATUSES)
    blocked = final.assets[1]
    assert blocked.status == "skipped"

    engine.retry_asset(final.id, blocked.id)
    rerun = engine.wait(final.id, TERMINAL_JOB_STATUSES)

    assert rerun.status == "partial"
    assert rerun.assets[1].status == "skipped"  # the cap still binds
    assert rerun.assets[1].error is not None and "spend cap" in rerun.assets[1].error
    assert rerun.spent_usd == pytest.approx(GEMINI_TTS_PRICE)  # no new spend
    assert len(paid_tts.requests) == 1  # and still no hidden paid call


def test_failed_estimation_never_reaches_a_paid_call(
    tmp_path: Path, projects_root: Path
):
    broken = BrokenEstimate()
    registry = Registry()
    registry.register("tts", broken)
    engine = JobEngine(
        registry,
        projects_root=projects_root,
        max_attempts=2,
        backoff_base_s=0.01,
        sleep=lambda _s: None,
    )
    try:
        job = priced_job(engine, projects_root, beats=1, provider_id="broken")

        final = engine.estimate(job.id)

        assert final.status == "failed"
        assert final.error is not None
        assert "estimation failed" in final.error
        assert "no price for this beat's text" in final.error
        assert [a.status for a in final.assets] == ["pending"]  # nothing ran
        assert broken.generate_calls == 0  # no paid call without an estimate
        with pytest.raises(IllegalTransition):
            engine.start(job.id)  # the gate stays shut on a dead estimate
    finally:
        engine.stop()


# --- the cap is honored through the type layer too -------------------------------


def test_priced_job_carries_its_cap_into_the_payload(
    engine, projects_root: Path, paid_tts: RecordedGemini
):
    cap = round(GEMINI_TTS_PRICE * 1.5, 6)
    job = priced_job(engine, projects_root, spend_cap_usd=cap)
    engine.estimate(job.id)
    shown = job_payload(engine.job(job.id))
    assert shown["spend_cap_usd"] == pytest.approx(cap)
    assert shown["status"] == "awaiting_confirmation"
    assert shown["spent_usd"] == 0.0
    assert shown["estimate_usd"] == pytest.approx(3 * GEMINI_TTS_PRICE)
