"""A. US Treasury yield engine (10Y yield via yfinance ``^TNX``).

Fetching runs on a background thread so a slow or failing HTTP call never
blocks the trading loop; the loop only ever reads the latest snapshot.
"""

from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import pandas as pd

from .config import StrategyConfig
from .indicators import zscore_last

log = logging.getLogger(__name__)


@dataclass
class YieldSnapshot:
    value: float = float("nan")          # yield in percent, e.g. 4.25
    delta_bp: float = float("nan")       # change over the window, basis points
    zscore_1h: float = float("nan")      # z-score of the 1h change
    as_of: Optional[datetime] = None     # timestamp of the latest yield print
    fetched_at: Optional[datetime] = None
    error: str = ""
    stale: bool = True
    score: int = 0
    notes: list = field(default_factory=list)


def fetch_yield_series_yfinance(ticker: str) -> pd.Series:
    """Intraday 5-minute yield closes, indexed by UTC timestamp."""
    import yfinance as yf  # imported lazily: heavy, and optional in tests

    hist = yf.Ticker(ticker).history(period="5d", interval="5m", auto_adjust=False)
    if hist is None or hist.empty:
        raise RuntimeError(f"yfinance returned no data for {ticker}")
    series = hist["Close"].dropna()
    series.index = series.index.tz_convert("UTC")
    return series


class MacroDataEngine:
    """Keeps a fresh :class:`YieldSnapshot` and scores it for gold."""

    def __init__(self, cfg: StrategyConfig,
                 fetcher: Callable[[str], pd.Series] = fetch_yield_series_yfinance,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.cfg = cfg
        self._fetcher = fetcher
        self._clock = clock
        self._lock = threading.Lock()
        self._snapshot = YieldSnapshot(error="not fetched yet")
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ----------------------------------------------------------------- #
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="macro-yield",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.refresh()
            self._stop.wait(self.cfg.yield_refresh_seconds)

    # ----------------------------------------------------------------- #
    def refresh(self) -> None:
        """Fetch and recompute once. Safe to call directly (used by tests)."""
        now = self._clock()
        try:
            series = self._fetcher(self.cfg.yield_ticker)
            snap = self._compute(series, now)
        except Exception as exc:  # noqa: BLE001 - network/parse errors of any kind
            log.warning("Yield fetch failed: %s", exc)
            with self._lock:
                prev = self._snapshot
            snap = YieldSnapshot(value=prev.value, delta_bp=prev.delta_bp,
                                 zscore_1h=prev.zscore_1h, as_of=prev.as_of,
                                 fetched_at=prev.fetched_at, error=str(exc))
        with self._lock:
            self._snapshot = snap

    def _compute(self, series: pd.Series, now: datetime) -> YieldSnapshot:
        series = series.sort_index()
        # Yahoo historically quoted ^TNX as yield x 10 (42.5 = 4.25 %).
        if series.median() > 20:
            series = series / 10.0

        last_ts = series.index[-1]
        value = float(series.iat[-1])
        window = timedelta(minutes=self.cfg.yield_window_minutes)
        prior = series.loc[:last_ts - window]
        delta_bp = (value - float(prior.iat[-1])) * 100.0 if len(prior) else float("nan")

        hourly_change = series - series.shift(freq="60min").reindex(series.index,
                                                                    method="ffill")
        zscore = zscore_last(hourly_change, self.cfg.yield_zscore_lookback)

        return YieldSnapshot(value=value, delta_bp=delta_bp, zscore_1h=zscore,
                             as_of=last_ts.to_pydatetime(), fetched_at=now)

    # ----------------------------------------------------------------- #
    def snapshot(self) -> YieldSnapshot:
        """Latest snapshot with staleness and score evaluated at call time."""
        with self._lock:
            base = self._snapshot
        snap = replace(base, notes=[])
        now = self._clock()
        max_age = timedelta(minutes=self.cfg.yield_max_staleness_minutes)
        snap.stale = snap.as_of is None or now - snap.as_of > max_age
        snap.score = self._score(snap)
        return snap

    def _score(self, snap: YieldSnapshot) -> int:
        if snap.stale:
            snap.notes.append("yield data stale/unavailable")
            return 0
        if math.isnan(snap.delta_bp):
            snap.notes.append("yield delta unavailable")
            return 0
        if (self.cfg.yield_min_abs_zscore > 0
                and (math.isnan(snap.zscore_1h)
                     or abs(snap.zscore_1h) < self.cfg.yield_min_abs_zscore)):
            snap.notes.append("yield z-score below gate")
            return 0
        trigger = self.cfg.yield_trigger_bp
        if snap.delta_bp >= trigger:
            snap.notes.append(f"yield spike +{snap.delta_bp:.1f}bp -> bearish gold")
            return -self.cfg.weight_yield
        if snap.delta_bp <= -trigger:
            snap.notes.append(f"yield drop {snap.delta_bp:.1f}bp -> bullish gold")
            return self.cfg.weight_yield
        return 0
