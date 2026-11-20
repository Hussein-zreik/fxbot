"""Stop-loss / take-profit placement for the gold sleeve.

This module only decides WHERE the stop and target go (1.5 x ATR / 3.0 x ATR by
default). It does not size orders and it does not approve them: sizing is
``risk.sizing`` and approval is ``risk.engine.RiskEngine.approve``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from .config import RiskConfig
from .mt5_connector import SymbolSpec, Tick
from .risk.sizing import floor_to_step


@dataclass(frozen=True)
class Levels:
    side: str
    entry: float
    sl: float
    tp: float
    sl_distance: float


def round_volume_down(volume: float, spec: SymbolSpec) -> float:
    """Round down to the broker's lot step (never round risk UP)."""
    return float(floor_to_step(volume, spec.volume_step))


def round_price(price: float, spec: SymbolSpec) -> float:
    return round(round(price / spec.tick_size) * spec.tick_size, spec.digits)


class RiskManager:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg

    def plan_levels(self, side: str, tick: Tick, atr_value: float,
                    spec: SymbolSpec) -> Tuple[Optional[Levels], str]:
        if not atr_value or atr_value != atr_value or atr_value <= 0:
            return None, "invalid ATR"
        entry = tick.ask if side == "BUY" else tick.bid       # spread is inside the risk
        sign = 1 if side == "BUY" else -1
        sl = round_price(entry - sign * self.cfg.sl_atr_mult * atr_value, spec)
        tp = round_price(entry + sign * self.cfg.tp_atr_mult * atr_value, spec)
        return Levels(side, entry, sl, tp, abs(entry - sl)), "ok"
