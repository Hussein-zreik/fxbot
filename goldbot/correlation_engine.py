"""B. DXY and inter-market metal correlation engine.

Produces two independent score blocks:

* DXY block (+/- weight_dxy): a Donchian breakout in the USD proxy. USD
  breaking up is bearish gold, breaking down is bullish gold.
* Silver confluence block (+/- weight_silver): gold and silver break
  structure in the same direction while the USD proxy moves the other way
  and the gold/silver return correlation is intact. If gold breaks out but
  silver does not confirm, the move is flagged as an inter-market divergence
  (probable fakeout) and the block scores 0.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from .config import StrategyConfig
from .indicators import (breakout_direction, rate_of_change_pct,
                         rolling_return_correlation)

log = logging.getLogger(__name__)


@dataclass
class CorrelationSnapshot:
    correlation: float = float("nan")
    gold_break: int = 0
    silver_break: int = 0
    usd_break: int = 0
    usd_roc_pct: float = float("nan")
    usd_dir: int = 0
    divergence: bool = False
    dxy_score: int = 0
    silver_score: int = 0
    usd_source: str = ""
    ok: bool = False
    notes: list = field(default_factory=list)


def invert_fx_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Turn EURUSD bars into a USD-strength proxy (1 / EURUSD)."""
    out = df.copy()
    out["open"] = 1.0 / df["open"]
    out["close"] = 1.0 / df["close"]
    out["high"] = 1.0 / df["low"]   # inversion swaps the extremes
    out["low"] = 1.0 / df["high"]
    return out


def score_correlation(gold: pd.DataFrame, silver: pd.DataFrame,
                      usd: pd.DataFrame, cfg: StrategyConfig) -> CorrelationSnapshot:
    """Pure scoring function over aligned closed-bar frames."""
    snap = CorrelationSnapshot(ok=True)

    merged = gold[["time", "close"]].merge(silver[["time", "close"]], on="time",
                                           suffixes=("_g", "_s"))
    snap.correlation = rolling_return_correlation(merged["close_g"], merged["close_s"],
                                                  cfg.correlation_window)

    period, lookback = cfg.donchian_period, cfg.breakout_lookback_bars
    snap.gold_break = breakout_direction(gold, period, lookback)
    snap.silver_break = breakout_direction(silver, period, lookback)
    snap.usd_break = breakout_direction(usd, period, lookback)
    snap.usd_roc_pct = rate_of_change_pct(usd["close"], cfg.usd_roc_bars)
    if not math.isnan(snap.usd_roc_pct) and abs(snap.usd_roc_pct) >= cfg.usd_roc_min_pct:
        snap.usd_dir = 1 if snap.usd_roc_pct > 0 else -1

    # DXY block: USD breakout is inverse to gold.
    if snap.usd_break:
        snap.dxy_score = -snap.usd_break * cfg.weight_dxy
        snap.notes.append(f"USD {'up' if snap.usd_break > 0 else 'down'}side break")

    # Silver confluence / divergence block.
    g, s = snap.gold_break, snap.silver_break
    if g and s != g:
        snap.divergence = True
        snap.notes.append("gold break NOT confirmed by silver (divergence)")
    elif g and s == g:
        if math.isnan(snap.correlation) or snap.correlation < cfg.min_correlation:
            snap.notes.append(f"gold/silver correlation too low ({snap.correlation:.2f})")
        elif snap.usd_dir != -g:
            snap.notes.append("metals broke out but USD not moving inversely")
        else:
            snap.silver_score = g * cfg.weight_silver
            snap.notes.append(f"gold+silver {'up' if g > 0 else 'down'}side confluence")
    return snap


class CorrelationEngine:
    """Pulls MT5 bars for gold, silver and the USD proxy, then scores them."""

    def __init__(self, connector, cfg: StrategyConfig, gold: str, silver: str,
                 eurusd: Optional[str], dxy: Optional[str]):
        if not dxy and not eurusd:
            raise ValueError("Need either a DXY symbol or EURUSD for the USD proxy")
        self.connector = connector
        self.cfg = cfg
        self.gold, self.silver, self.eurusd, self.dxy = gold, silver, eurusd, dxy
        self.bars = max(cfg.correlation_window, cfg.donchian_period) \
            + cfg.breakout_lookback_bars + 50

    def _usd_bars(self) -> Optional[pd.DataFrame]:
        tf = self.cfg.correlation_timeframe
        if self.dxy:
            return self.connector.get_rates(self.dxy, tf, self.bars)
        fx = self.connector.get_rates(self.eurusd, tf, self.bars)
        return invert_fx_bars(fx) if fx is not None else None

    def evaluate(self) -> CorrelationSnapshot:
        tf = self.cfg.correlation_timeframe
        try:
            gold = self.connector.get_rates(self.gold, tf, self.bars)
            silver = self.connector.get_rates(self.silver, tf, self.bars)
            usd = self._usd_bars()
        except Exception as exc:  # noqa: BLE001
            log.error("Correlation data fetch failed: %s", exc)
            return CorrelationSnapshot(notes=[f"data error: {exc}"])
        if gold is None or silver is None or usd is None:
            return CorrelationSnapshot(notes=["missing bars for gold/silver/USD"])
        snap = score_correlation(gold, silver, usd, self.cfg)
        snap.usd_source = self.dxy or f"1/{self.eurusd}"
        return snap
