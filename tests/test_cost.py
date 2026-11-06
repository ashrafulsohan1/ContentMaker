"""Seam: app.core.cost — price table, conservative estimates, spend log, caps (BUILD_PLAN 1.6).

Catches: underestimated spend (rounding that isn't up), silent cap bypass,
spend that never reaches the audit log (or totals wrong), prices drifting
from editable config, and mock jobs costing anything but exactly zero.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.cost import (
    CAP_EPSILON,
    PriceTable,
    PriceTableError,
    SpendLog,
    cap_reached,
    estimate_image_cost,
    estimate_tts_cost,
    estimate_video_cost,
    load_prices,
    prices_path,
)
from app.core.jobs import JobEngine, JobStore
from app.engines.base import ImageRequest, ImageResult
from app.engines.mocks import MockTTSProvider
from app.engines.registry import Registry

TERMINAL = {"completed", "partial", "failed", "cancelled"}


# --- helpers / test doubles --------------------------------------------------


def _write_prices(tmp_path: Path, providers: dict) -> Path:
    file = tmp_path / "prices.json"
    file.write_text(json.dumps({"providers": providers}), encoding="utf-8")
    return file


def _table(tmp_path: Path) -> PriceTable:
    file = _write_prices(
        tmp_path,
        {
            "t": {
                "label": "test provider",
                "verified": True,
                "engines": {
                    "tts": {"unit": "usd_per_1k_chars", "price": 0.03},
                    "image": {"unit": "usd_per_image", "price": 0.1},
                    "video": {"unit": "usd_per_second", "price": 0.3},
                },
            }
        },
    )
    return load_prices(file)


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


@pytest.fixture()
def projects_root(tmp_path: Path) -> Path:
    return tmp_path / "projects"


@pytest.fixture()
def registry() -> Registry:
    reg = Registry()
    reg.register("tts", MockTTSProvider())
    return reg


@pytest.fixture()
def engine(projects_root: Path, registry: Registry):
    eng = JobEngine(registry, projects_root=projects_root, sleep=lambda _s: None)
    yield eng
    eng.stop()


def tts_spec(root: Path, index: int) -> object:
    from app.core.jobs import AssetSpec

    return AssetSpec(
        kind="tts",
        beat_index=index,
        out_path=root / "audio" / f"beat_{index:02d}.wav",
        params={"text": "hello beat", "voice": "demo"},
    )


def img_spec(root: Path, index: int) -> object:
    from app.core.jobs import AssetSpec

    return AssetSpec(
        kind="image",
        beat_index=index,
        out_path=root / "visuals" / f"beat_{index:02d}.png",
        params={"prompt": f"scene {index}", "width": 64, "height": 64},
    )


# --- price table: shipped config file ----------------------------------------


def test_prices_file_has_mock_and_spike_verified_gemini():
    table = load_prices()
    assert table.currency == "USD"
    mock = table.providers["mock"]
    assert mock.verified is True, "mock pricing is exact, not a placeholder"
    for engine in ("tts", "image", "video"):
        assert mock.engines[engine].price == 0.0
    # Research spikes 2.0/4.0 (2026-11-06, research/gemini-api.md §8 +
    # research/gemini-video.md §5) verified the gemini models and prices — no
    # placeholder remains (BUILD_PLAN 2.3 wired the real TTS provider; the
    # placeholder assertion was relaxed there; 4.1 flipped video to 1.2).
    gemini = table.providers["gemini"]
    assert gemini.verified is True
    assert gemini.engines["tts"].model == "gemini-3.8-flash-tts"
    assert gemini.engines["tts"].price == 0.02
    assert gemini.engines["image"].model == "gemini-nano-banana-2.1"
    assert gemini.engines["image"].price == 0.0504
    assert gemini.engines["video"].model == "gemini-omni-1.2-flash"
    assert gemini.engines["video"].price == 0.07


def test_prices_path_points_at_editable_config():
    assert prices_path().name == "prices.json"
    assert prices_path().parent.name == "data"


# --- price table: loader validation ------------------------------------------


def test_price_for_lookup_and_clear_errors(tmp_path: Path):
    table = _table(tmp_path)
    assert table.price_for("t", "tts").price == 0.03
    assert table.price_for("t", "tts").unit == "usd_per_1k_chars"

    with pytest.raises(ValueError, match="no prices for provider 'nope'"):
        table.price_for("nope", "tts")
    with pytest.raises(ValueError, match="has no 'image' price"):
        load_prices(_write_prices(tmp_path, {"x": {"engines": {}}})).price_for("x", "image")


@pytest.mark.parametrize(
    ("engines", "match"),
    [
        ({"tts": {"unit": "usd_per_1k_chars", "price": -0.01}}, "non-negative"),
        ({"subtitles": {"unit": "usd_per_second", "price": 0.1}}, "unknown engine"),
        ({"tts": {"unit": "usd_per_image", "price": 0.03}}, "unit must be"),
        ({"tts": {"unit": "usd_per_1k_chars", "price": "0.03"}}, "must be a number"),
        ({"tts": {"unit": "usd_per_1k_chars", "price": True}}, "must be a number"),
        ({"tts": {"unit": "usd_per_1k_chars", "price": None}}, "got None"),
    ],
)
def test_load_prices_rejects_bad_data(tmp_path: Path, engines: dict, match: str):
    file = _write_prices(tmp_path, {"t": {"engines": engines}})
    with pytest.raises(PriceTableError, match=match):
        load_prices(file)


def test_load_prices_rejects_missing_providers_and_missing_file(tmp_path: Path):
    empty = tmp_path / "empty.json"
    empty.write_text("{}", encoding="utf-8")
    with pytest.raises(PriceTableError, match="providers"):
        load_prices(empty)
    with pytest.raises(FileNotFoundError):
        load_prices(tmp_path / "nope.json")


# --- estimates: conservative rounding up (risk #5) ----------------------------


def test_tts_estimate_rounds_chars_up_to_whole_1k_units(tmp_path: Path):
    table = _table(tmp_path)  # 0.03 per 1,000 chars
    assert estimate_tts_cost(table, "t", text="") == 0.0
    assert estimate_tts_cost(table, "t", text="x" * 1) == 0.03
    assert estimate_tts_cost(table, "t", text="x" * 1000) == 0.03
    assert estimate_tts_cost(table, "t", text="x" * 1001) == 0.06
    assert estimate_tts_cost(table, "t", text="x" * 2500) == 0.09


def test_image_estimate_counts_images(tmp_path: Path):
    table = _table(tmp_path)  # 0.10 per image
    assert estimate_image_cost(table, "t") == 0.1
    assert estimate_image_cost(table, "t", count=3) == 0.3
    with pytest.raises(ValueError, match="non-negative"):
        estimate_image_cost(table, "t", count=-1)


def test_video_estimate_ceils_fractional_seconds(tmp_path: Path):
    table = _table(tmp_path)  # 0.30 per second
    assert estimate_video_cost(table, "t", duration_s=0.0) == 0.0
    assert estimate_video_cost(table, "t", duration_s=0.4) == 0.3  # 0.4s bills as 1s
    assert estimate_video_cost(table, "t", duration_s=5.0) == 1.5
    assert estimate_video_cost(table, "t", duration_s=5.2) == 1.8  # 5.2s bills as 6s
    with pytest.raises(ValueError, match="non-negative"):
        estimate_video_cost(table, "t", duration_s=-1.0)
    with pytest.raises(ValueError, match="finite"):
        estimate_video_cost(table, "t", duration_s=float("nan"))
    with pytest.raises(ValueError, match="finite"):
        estimate_video_cost(table, "t", duration_s=float("inf"))


def test_estimate_math_is_exact_not_float_noisy(tmp_path: Path):
    file = _write_prices(
        tmp_path,
        {"t": {"engines": {"image": {"unit": "usd_per_image", "price": 0.039}}}},
    )
    table = load_prices(file)
    assert estimate_image_cost(table, "t", count=3) == 0.117


def test_mock_provider_estimates_are_exactly_zero():
    table = load_prices()
    for engine_call in (
        lambda: estimate_tts_cost(table, "mock", text="hello beat"),
        lambda: estimate_image_cost(table, "mock", count=7),
        lambda: estimate_video_cost(table, "mock", duration_s=7.3),
    ):
        assert engine_call() == 0.0


def test_estimate_unknown_provider_raises_with_clear_message(tmp_path: Path):
    table = _table(tmp_path)
    with pytest.raises(ValueError, match="no prices for provider 'ghost'"):
        estimate_tts_cost(table, "ghost", text="hi")


# --- hard per-job cap (checked BEFORE each paid call) -------------------------


def test_cap_reached_allows_work_under_cap():
    assert cap_reached(None, 10.0, 5.0) is False  # no cap set
    assert cap_reached(1.0, 0.0, 1.0) is False  # exactly at cap is allowed
    assert cap_reached(1.0, 0.99, 0.01) is False


def test_cap_reached_blocks_work_that_would_exceed_cap():
    assert cap_reached(1.0, 0.75, 0.5) is True
    assert cap_reached(0.0, 0.0, 0.01) is True


def test_cap_reached_tolerates_float_noise():
    assert CAP_EPSILON > 0
    assert cap_reached(0.3, 0.1 + 0.2, 0.0) is False


# --- spend log (projects/<name>/state/spend_log.jsonl, PROMPT §7) -------------


def test_spend_log_appends_json_lines_and_totals(tmp_path: Path):
    log = SpendLog(tmp_path / "state" / "spend_log.jsonl")
    entry = log.record(
        job_id="job_a",
        project="alpha",
        engine="image",
        provider_id="priced",
        amount_usd=1.25,
        asset_id=7,
    )
    log.record(
        job_id="job_b", project="beta", engine="tts", provider_id="priced", amount_usd=0.25
    )

    assert set(entry) == {
        "ts",
        "event",
        "project",
        "job_id",
        "asset_id",
        "engine",
        "provider_id",
        "amount_usd",
    }
    lines = log.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["job_id"] == "job_a"
    assert log.total() == pytest.approx(1.5)
    assert log.total(job_id="job_a") == pytest.approx(1.25)
    assert log.total(project="beta") == pytest.approx(0.25)
    assert log.total(job_id="job_nope") == 0.0


def test_spend_log_missing_file_reads_empty(tmp_path: Path):
    log = SpendLog(tmp_path / "state" / "spend_log.jsonl")
    assert log.entries() == []
    assert log.total() == 0.0


def test_spend_log_tolerates_torn_final_line(tmp_path: Path):
    """A crash/OneDrive-sync tear must not break reads; job.db stays authoritative."""
    log = SpendLog(tmp_path / "spend_log.jsonl")
    log.record(
        job_id="job_a", project="alpha", engine="tts", provider_id="p", amount_usd=0.5
    )
    with log.path.open("a", encoding="utf-8") as fh:
        fh.write('{"job_id": "job_a", "amount_usd": 0.')  # torn write, no newline

    assert len(log.entries()) == 1
    assert log.total() == pytest.approx(0.5)


def test_spend_log_creates_parent_dirs(tmp_path: Path):
    log = SpendLog(tmp_path / "deep" / "nested" / "spend_log.jsonl")
    log.record(
        job_id="job_a", project="alpha", engine="tts", provider_id="p", amount_usd=0.0
    )
    assert log.path.exists()


# --- engine wiring: default log per project, zero-spend mocks -----------------


def test_engine_writes_default_spend_log_per_project(engine, projects_root: Path):
    root = projects_root / "alpha"
    priced = PricedImage(1.0)
    engine._registry.register("image", priced)
    job = engine.create_job(
        "alpha",
        [img_spec(root, 1), img_spec(root, 2), img_spec(root, 3)],
        providers={"image": "priced"},
        spend_cap_usd=5.0,
    )
    engine.estimate(job.id)
    engine.start(job.id)
    final = engine.wait(job.id, TERMINAL)

    assert final.status == "completed"
    log = engine.spend_log("alpha")
    assert log is not None
    assert log.path == projects_root / "alpha" / "state" / "spend_log.jsonl"
    entries = log.entries()
    assert [e["amount_usd"] for e in entries] == [1.0, 1.0, 1.0]
    assert {e["job_id"] for e in entries} == {job.id}
    assert {e["provider_id"] for e in entries} == {"priced"}
    assert {e["engine"] for e in entries} == {"image"}
    assert all(e["asset_id"] is not None for e in entries)
    assert log.total(job_id=job.id) == pytest.approx(final.spent_usd)


def test_mock_job_logs_exact_zero_spend(engine, projects_root: Path):
    root = projects_root / "alpha"
    job = engine.create_job("alpha", [tts_spec(root, 1), tts_spec(root, 2)])
    engine.estimate(job.id)
    engine.start(job.id)
    final = engine.wait(job.id, TERMINAL)

    assert final.status == "completed"
    assert final.estimate_usd == 0.0
    assert final.spent_usd == 0.0
    log = engine.spend_log("alpha")
    assert log is not None
    entries = log.entries()
    assert len(entries) == 2  # audit parity: one line per completed call
    assert all(e["amount_usd"] == 0.0 for e in entries)
    assert log.total(job_id=job.id) == 0.0


def test_engine_honors_explicit_spend_log_factory(projects_root: Path, registry: Registry):
    custom = projects_root.parent / "custom_logs" / "spend.jsonl"
    eng = JobEngine(
        Registry(),
        projects_root=projects_root,
        spend_log_factory=lambda _project: SpendLog(custom),
        sleep=lambda _s: None,
    )
    try:
        root = projects_root / "alpha"
        eng._registry.register("image", PricedImage(0.5))
        job = eng.create_job("alpha", [img_spec(root, 1)], providers={"image": "priced"})
        eng.estimate(job.id)
        eng.start(job.id)
        eng.wait(job.id, TERMINAL)
        assert custom.exists()
        assert len(SpendLog(custom).entries()) == 1
    finally:
        eng.stop()


def test_engine_without_spend_log_still_runs(tmp_path: Path, registry: Registry):
    eng = JobEngine(
        registry,
        store_factory=lambda project: JobStore(tmp_path / project / "state" / "job.db"),
        sleep=lambda _s: None,
    )
    try:
        assert eng.spend_log("alpha") is None
        root = tmp_path / "alpha"
        job = eng.create_job("alpha", [tts_spec(root, 1)])
        eng.estimate(job.id)
        eng.start(job.id)
        final = eng.wait(job.id, TERMINAL)
        assert final.status == "completed"
    finally:
        eng.stop()
