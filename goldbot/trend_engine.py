"""E. Multi-timeframe trend engine (default: H4 and H1).

For each timeframe the trend is classified from three classic tools:

* EMA alignment - up if close > EMA50 > EMA200 and EMA50 is rising
  (mirror for down).
* ADX strength  - ADX >= ``adx_min`` (20) means the market is trending;
  below it the market is treated as ranging (no trend).
* Structure     - the last two swing highs/lows. An uptrend is vetoed if
  structure shows lower highs AND lower lows, and vice versa.

Each trending timeframe adds its weight (default 10 + 10 = +/-20). The trend
of ``filter_timeframe`` (default H4) also acts as a FILTER: no BUY while it
is down, no SELL while it is up. A ranging filter timeframe blocks nothing.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List

import pandas as pd

from .config import TrendConfig
from .indicators import adx, ema, swing_structure

log = logging.getLogger(__name__)


@dataclass
class TimeframeTrend:
    timeframe: str
    direction: int = 0          # +1 up, -1 down, 0 range/unknown
    ema_dir: int = 0
    structure: int = 0
    adx: float = float("nan")
    ok: bool = False

    def label(self) -> str:
        name = {1: "UP", -1: "DOWN", 0: "RANGE"}[self.direction]
        return f"{self.timeframe} {name}"


@dataclass
class TrendSnapshot:
    score: int = 0
    filter_dir: int = 0         # trend of the filter timeframe
    frames: List[TimeframeTrend] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def summary(self) -> str:
        return " ".join(f.label() for f in self.frames) or "n/a"


def classify_trend(df: pd.DataFrame, timeframe: str, cfg: TrendConfig) -> TimeframeTrend:
    """Pure trend classification of one timeframe's closed bars."""
    out = TimeframeTrend(timeframe=timeframe)
    if df is None or len(df) < cfg.ema_slow + cfg.ema_slope_bars:
        return out
    close = df["close"]
    fast, slow = ema(close, cfg.ema_fast), ema(close, cfg.ema_slow)
    c, f, s = close.iat[-1], fast.iat[-1], slow.iat[-1]
    slope = f - fast.iat[-1 - cfg.ema_slope_bars]
    if any(math.isnan(v) for v in (f, s, slope)):
        return out

    if c > f > s and slope > 0:
        out.ema_dir = 1
    elif c < f < s and slope < 0:
        out.ema_dir = -1
    out.adx = float(adx(df, cfg.adx_period).iat[-1])
    out.structure = swing_structure(df, cfg.swing_strength)
    out.ok = True

    trending = not math.isnan(out.adx) and out.adx >= cfg.adx_min
    if out.ema_dir and trending and out.structure != -out.ema_dir:
        out.direction = out.ema_dir
    return out


class TrendEngine:
    def __init__(self, connector, cfg: TrendConfig, symbol: str):
        self.connector = connector
        self.cfg = cfg
        self.symbol = symbol

    def evaluate(self) -> TrendSnapshot:
        snap = TrendSnapshot()
        if not self.cfg.enabled:
            snap.notes.append("trend engine disabled")
            return snap
        by_tf: Dict[str, TimeframeTrend] = {}
        for tf, weight in zip(self.cfg.timeframes, self.cfg.weights):
            try:
                df = self.connector.get_rates(self.symbol, tf, self.cfg.bars)
            except Exception as exc:  # noqa: BLE001
                log.error("Trend bars %s fetch failed: %s", tf, exc)
                df = None
            trend = classify_trend(df, tf, self.cfg)
            by_tf[tf] = trend
            snap.frames.append(trend)
            snap.score += trend.direction * weight
            if not trend.ok:
                snap.notes.append(f"{tf} trend data insufficient")
        if self.cfg.filter_timeframe in by_tf:
            snap.filter_dir = by_tf[self.cfg.filter_timeframe].direction
        snap.notes.append(f"trend {snap.summary()}")
        return snap
