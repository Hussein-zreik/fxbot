"""D. Technical trigger engine: session VWAP + ATR on the execution chart.

A VWAP *retest* in the direction of the macro bias means:

* long:  VWAP <= close <= VWAP + k * ATR  (pulled back to VWAP, holding above)
* short: VWAP - k * ATR <= close <= VWAP  (rallied to VWAP, holding below)

with ``k = vwap_retest_atr_mult`` (default 1.0).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd

from .config import StrategyConfig
from .indicators import atr, session_vwap

log = logging.getLogger(__name__)

# One full UTC day of M5 bars plus ATR warm-up.
_BARS_NEEDED = 400


@dataclass
class TechnicalSnapshot:
    bar_time: Optional[datetime] = None
    close: float = float("nan")
    vwap: float = float("nan")
    atr: float = float("nan")
    retest_long: bool = False
    retest_short: bool = False
    ok: bool = False
    notes: list = field(default_factory=list)

    def vwap_score(self, weight: int) -> int:
        if self.retest_long:
            return weight
        if self.retest_short:
            return -weight
        return 0


def evaluate_technicals(df: pd.DataFrame, cfg: StrategyConfig) -> TechnicalSnapshot:
    snap = TechnicalSnapshot()
    if df is None or len(df) < cfg.atr_period + 2:
        snap.notes.append("not enough execution bars")
        return snap
    atr_series = atr(df, cfg.atr_period)
    vwap_series = session_vwap(df)
    snap.bar_time = df["time"].iat[-1].to_pydatetime()
    snap.close = float(df["close"].iat[-1])
    snap.atr = float(atr_series.iat[-1])
    snap.vwap = float(vwap_series.iat[-1])
    if math.isnan(snap.atr) or math.isnan(snap.vwap) or snap.atr <= 0:
        snap.notes.append("ATR/VWAP not ready")
        return snap

    band = cfg.vwap_retest_atr_mult * snap.atr
    snap.retest_long = snap.vwap <= snap.close <= snap.vwap + band
    snap.retest_short = snap.vwap - band <= snap.close <= snap.vwap
    snap.ok = True
    return snap


class TechnicalEngine:
    def __init__(self, connector, cfg: StrategyConfig, symbol: str):
        self.connector = connector
        self.cfg = cfg
        self.symbol = symbol

    def evaluate(self) -> TechnicalSnapshot:
        try:
            df = self.connector.get_rates(self.symbol, self.cfg.execution_timeframe,
                                          _BARS_NEEDED)
        except Exception as exc:  # noqa: BLE001
            log.error("Execution bars fetch failed: %s", exc)
            return TechnicalSnapshot(notes=[f"data error: {exc}"])
        return evaluate_technicals(df, self.cfg)
