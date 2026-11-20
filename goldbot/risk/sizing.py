"""The ONLY place lots are computed (CLAUDE.md section 1, sizing contract).

    lots = floor_to_step(risk_usd / (|entry - sl| / tick_size * tick_value), step)
    lots < volume_min  ->  Skip   (NEVER round up)

All arithmetic is ``Decimal``. ``floor_to_step`` adds a 1e-9 tolerance in lot
units so a quotient such as 0.4999999998 (float noise) still sizes as 0.50.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import Optional, Union

TOLERANCE = Decimal("1e-9")
OVERSHOOT_FACTOR = Decimal("1.10")


def _d(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


@dataclass(frozen=True)
class Sized:
    lots: Decimal
    risk_usd: Decimal          # lots x risk-per-lot: what the stop would actually cost
    risk_per_lot: Decimal


@dataclass(frozen=True)
class Skip:
    reason: str


def floor_to_step(value, step) -> Decimal:
    v, s = _d(value), _d(step)
    if s <= 0:
        raise ValueError("volume step must be positive")
    return ((v + TOLERANCE) / s).to_integral_value(rounding=ROUND_FLOOR) * s


def risk_distance(side: str, bid, ask, sl) -> Decimal:
    """Entry-to-stop distance INCLUDING the spread (long: ask - sl; short: sl - bid)."""
    if side == "BUY":
        return _d(ask) - _d(sl)
    if side == "SELL":
        return _d(sl) - _d(bid)
    raise ValueError(f"side must be BUY or SELL, got {side!r}")


def risk_per_lot(distance, tick_size, tick_value) -> Decimal:
    return abs(_d(distance)) / _d(tick_size) * _d(tick_value)


def size_position(*, risk_usd, entry, sl, tick_size, tick_value, volume_min, volume_step,
                  volume_max: Optional[float] = None) -> Union[Sized, Skip]:
    per_lot = risk_per_lot(_d(entry) - _d(sl), tick_size, tick_value)
    if per_lot <= 0:
        return Skip("zero risk distance")
    lots = floor_to_step(_d(risk_usd) / per_lot, volume_step)
    if volume_max is not None:
        lots = min(lots, floor_to_step(volume_max, volume_step))
    if lots < _d(volume_min):
        return Skip("risk too small for min lot")          # NEVER round up
    return Sized(lots=lots, risk_usd=lots * per_lot, risk_per_lot=per_lot)


def notional(lots, contract_size, price) -> Decimal:
    return _d(lots) * _d(contract_size) * _d(price)


def effective_leverage(notional: Decimal, equity) -> Decimal:
    return _d(notional) / _d(equity)


def actual_risk_usd(*, fill, sl, tick_size, tick_value, lots) -> Decimal:
    """Risk of the position as actually filled."""
    return risk_per_lot(_d(fill) - _d(sl), tick_size, tick_value) * _d(lots)


def overshoot_ok(*, actual, planned) -> bool:
    """After a fill: actual risk must not exceed the plan by more than 10 %."""
    return _d(actual) <= _d(planned) * OVERSHOOT_FACTOR
