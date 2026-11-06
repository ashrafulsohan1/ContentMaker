"""Cost module: price table, conservative estimates, spend log, caps (BUILD_PLAN 1.6/4.5).

Implements the cost-guardrail chain of spec §6.6 / BUILD_PLAN §1.2:
- ``app/data/prices.json`` is the editable per-provider unit-price table —
  no price is ever hardcoded (risk #1: provider drift; task 2.0 re-verifies).
- Estimates are **conservative**: usage is rounded UP to whole billing units
  (chars → whole 1k blocks, fractional video seconds → whole seconds) and the
  result is ceiled to whole micro-dollars, so an estimate never under-counts
  (risk #5). Mock providers price at exactly 0 → dry-run jobs cost 0.
  ``estimate_total_cost`` sums per-asset estimates exactly (Decimal) with the
  same ceiling, so the confirm-flow job total never sits below the sum of the
  per-asset rows the user is shown (BUILD_PLAN 4.5).
- ``round_usd`` is the display-accuracy primitive (4.5): price-table amounts
  are micro-dollar quantities that binary floats carry noisily; rounding every
  accumulation step keeps running spend an exact sum of whole micro-dollars.
- ``cap_reached`` is the single hard per-job cap check, run by the job engine
  BEFORE each paid call; over-cap assets are skipped, completed ones kept.
  Non-finite values raise instead of comparing — NaN is False against
  everything, so it would silently switch the guardrail off (4.5).
- ``SpendLog`` appends one JSON line per completed provider call to
  ``projects/<name>/state/spend_log.jsonl`` (PROMPT §7 state layout) — the
  machine-readable audit trail paired with job.db (BUILD_PLAN §1.2).
"""
from __future__ import annotations

import json
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal
from pathlib import Path

APP_DATA_DIR = Path(__file__).resolve().parents[1] / "data"
PRICES_FILE = APP_DATA_DIR / "prices.json"

ENGINES: tuple[str, ...] = ("tts", "image", "video")
PRICE_UNITS: dict[str, str] = {
    "tts": "usd_per_1k_chars",
    "image": "usd_per_image",
    "video": "usd_per_second",
}
_CHARS_PER_UNIT = 1_000  # "usd_per_1k_chars" billing block
_MICRO_USD = Decimal("0.000001")  # estimates ceil to whole micro-dollars
CAP_EPSILON = 1e-9  # float-noise tolerance for at-cap comparisons


class PriceTableError(ValueError):
    """The prices.json data is malformed — fail loudly, never guess a price."""


@dataclass(frozen=True)
class EnginePrice:
    """Unit price for one engine of one provider (USD)."""

    engine: str
    unit: str
    price: float
    model: str | None = None  # model id the price belongs to (verified at task 2.0)


@dataclass(frozen=True)
class ProviderPrices:
    """All engine prices for one provider id."""

    provider_id: str
    label: str
    verified: bool  # False = ballpark placeholder pending the M2 research spike
    engines: dict[str, EnginePrice] = field(default_factory=dict)


@dataclass(frozen=True)
class PriceTable:
    """Loaded prices.json; lookups raise with messages naming what's missing."""

    currency: str
    providers: dict[str, ProviderPrices]

    def price_for(self, provider_id: str, engine: str) -> EnginePrice:
        provider = self.providers.get(provider_id)
        if provider is None:
            registered = ", ".join(sorted(self.providers)) or "none"
            raise ValueError(f"no prices for provider {provider_id!r} (providers: {registered})")
        price = provider.engines.get(engine)
        if price is None:
            has = ", ".join(sorted(provider.engines)) or "none"
            raise ValueError(f"provider {provider_id!r} has no {engine!r} price (has: {has})")
        return price


def prices_path() -> Path:
    return PRICES_FILE


def load_prices(path: Path | None = None) -> PriceTable:
    """Load the unit-price table; unknown/invalid data raises PriceTableError."""
    file = path if path is not None else PRICES_FILE
    payload = json.loads(file.read_text(encoding="utf-8"))
    providers_raw = payload.get("providers")
    if not isinstance(providers_raw, dict) or not providers_raw:
        raise PriceTableError(f"{file}: 'providers' must be a non-empty mapping")

    providers: dict[str, ProviderPrices] = {}
    for provider_id, raw in providers_raw.items():
        engines_raw = raw.get("engines")
        if not isinstance(engines_raw, dict):
            raise PriceTableError(f"{file}: provider {provider_id!r} needs an 'engines' mapping")
        engines = {
            engine: _engine_price(file, provider_id, engine, entry)
            for engine, entry in engines_raw.items()
        }
        providers[provider_id] = ProviderPrices(
            provider_id=str(provider_id),
            label=str(raw.get("label", "")),
            verified=bool(raw.get("verified", False)),
            engines=engines,
        )
    return PriceTable(currency=str(payload.get("currency", "USD")), providers=providers)


def _engine_price(file: Path, provider_id: str, engine: str, entry: dict) -> EnginePrice:
    if engine not in PRICE_UNITS:
        known = ", ".join(sorted(PRICE_UNITS))
        raise PriceTableError(
            f"{file}: provider {provider_id!r}: unknown engine {engine!r} (known: {known})"
        )
    expected_unit = PRICE_UNITS[engine]
    unit = entry.get("unit")
    if unit != expected_unit:
        raise PriceTableError(
            f"{file}: provider {provider_id!r} engine {engine!r}:"
            f" unit must be {expected_unit!r} (got {unit!r})"
        )
    return EnginePrice(
        engine=engine,
        unit=unit,
        price=_price_value(file, provider_id, engine, entry.get("price")),
        model=entry.get("model"),
    )


def _price_value(file: Path, provider_id: str, engine: str, value: object) -> float:
    where = f"{file}: provider {provider_id!r} engine {engine!r}"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PriceTableError(f"{where}: 'price' must be a number (got {value!r})")
    price = float(value)
    if not math.isfinite(price) or price < 0:
        raise PriceTableError(
            f"{where}: 'price' must be a finite non-negative number (got {value!r})"
        )
    return price


# --- conservative estimates (risk #5: round up, never under-count) -----------


def estimate_tts_cost(table: PriceTable, provider_id: str, *, text: str) -> float:
    """Whole 1k-char blocks, ceiled up: 1001 chars bill as 2 units."""
    units = math.ceil(len(text) / _CHARS_PER_UNIT)
    return _cost(table, provider_id, "tts", units)


def estimate_image_cost(table: PriceTable, provider_id: str, *, count: int = 1) -> float:
    """Per-image billing: count images (one call = one image)."""
    if count < 0:
        raise ValueError(f"image count must be non-negative (got {count})")
    return _cost(table, provider_id, "image", count)


def estimate_video_cost(table: PriceTable, provider_id: str, *, duration_s: float) -> float:
    """Per-second billing, fractional seconds ceiled up: 5.2s bills as 6s."""
    if not math.isfinite(duration_s):
        raise ValueError(f"duration_s must be finite (got {duration_s!r})")
    if duration_s < 0:
        raise ValueError(f"duration_s must be non-negative (got {duration_s})")
    return _cost(table, provider_id, "video", math.ceil(duration_s))


def _cost(table: PriceTable, provider_id: str, engine: str, units: int) -> float:
    """units × unit-price via Decimal, ceiled to whole micro-dollars (up, always)."""
    price = table.price_for(provider_id, engine).price
    total = Decimal(str(price)) * units
    return float(total.quantize(_MICRO_USD, rounding=ROUND_CEILING))


# --- exact USD display primitives (4.5: running-spend/estimate accuracy) -----


def round_usd(amount: float) -> float:
    """Quantize a USD amount to the whole micro-dollar grid (nearest).

    Price-table amounts are micro-dollar quantities that binary floats only
    carry noisily (0.02+0.02+0.02 → 0.060000000000000005). Rounding every
    accumulation step keeps the running spend an exact sum of whole
    micro-dollars, so the job total equals the sum of its per-asset rows and
    the JSON/SSE display never shows drift. Non-finite input raises: a NaN
    spend would silently disable the cap (NaN comparisons are always False).
    """
    amount = float(amount)
    if not math.isfinite(amount):
        raise ValueError(f"USD amount must be finite (got {amount!r})")
    return float(Decimal(str(amount)).quantize(_MICRO_USD, rounding=ROUND_HALF_EVEN))


def estimate_total_cost(per_asset_usd: Iterable[float]) -> float:
    """Conservative job total: exact decimal sum, ceiled to whole micro-dollars.

    Naive float accumulation under-counts (10 × $0.10 → 0.9999999999999999),
    which would put the confirm-flow total below the sum of the very per-asset
    estimates the user is shown (risk #5: an estimate never under-counts).
    Exact decimal math + the ceiling keeps the total at or above every
    displayed part. Non-finite input raises — the job engine's estimation step
    turns that into a failed job before any paid call.
    """
    total = Decimal(0)
    for value in per_asset_usd:
        value = float(value)
        if not math.isfinite(value):
            raise ValueError(f"per-asset estimate must be finite (got {value!r})")
        total += Decimal(str(value))
    return float(total.quantize(_MICRO_USD, rounding=ROUND_CEILING))


# --- hard per-job cap (§6.6; enforced BEFORE each paid call) ------------------


def cap_reached(
    spend_cap_usd: float | None, spent_usd: float, next_cost_usd: float
) -> bool:
    """True when admitting ``next_cost_usd`` would push the job over its cap.

    No cap (``None``) never blocks; spending up to exactly the cap is allowed.
    ``CAP_EPSILON`` absorbs binary-float noise so 0.1+0.2 vs a 0.3 cap is not
    "over". The job engine calls this before every paid call (§10.4).
    Non-finite cap/spent/next-cost raises instead of comparing: NaN is False
    against everything, so a NaN value would silently switch the guardrail off.
    """
    if spend_cap_usd is None:
        return False
    for name, value in (
        ("spend cap", spend_cap_usd),
        ("spent amount", spent_usd),
        ("next cost", next_cost_usd),
    ):
        if not math.isfinite(value):
            raise ValueError(f"{name} must be a finite number of USD (got {value!r})")
    return spent_usd + next_cost_usd > spend_cap_usd + CAP_EPSILON


# --- spend log (PROMPT §7: projects/<name>/state/spend_log.jsonl) -------------


class SpendLog:
    """Append-only JSONL audit of completed provider calls for one project.

    One line per completed call — zero-cost mock calls included, so the log
    stays at parity with job.db spend events and dry-runs are visible as the
    zero-spend calls they were. A torn final line (crash / OneDrive sync) is
    skipped on read; job.db remains the authoritative spend source.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        *,
        job_id: str,
        project: str,
        engine: str,
        provider_id: str,
        amount_usd: float,
        asset_id: int | None = None,
        event: str = "generation",
    ) -> dict:
        entry = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "event": event,
            "project": project,
            "job_id": job_id,
            "asset_id": asset_id,
            "engine": engine,
            "provider_id": provider_id,
            "amount_usd": amount_usd,
        }
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return entry

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        entries: list[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue  # torn final write; the next record appends cleanly
            if isinstance(parsed, dict):
                entries.append(parsed)
        return entries

    def total(
        self, *, job_id: str | None = None, project: str | None = None
    ) -> float:
        """Exact sum of the matching amounts, on the whole micro-dollar grid.

        ``math.fsum`` adds the doubles without accumulated error and
        ``round_usd`` re-quantizes to the price-table grid, so the audit total
        pairs exactly (==, not approx) with the job's running spend.
        """
        amounts = [
            float(entry["amount_usd"])
            for entry in self.entries()
            if (job_id is None or entry.get("job_id") == job_id)
            and (project is None or entry.get("project") == project)
            and isinstance(entry.get("amount_usd"), (int, float))
            and not isinstance(entry.get("amount_usd"), bool)
        ]
        return round_usd(math.fsum(amounts))
