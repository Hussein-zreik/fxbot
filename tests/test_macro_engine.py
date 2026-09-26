from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from goldbot.config import StrategyConfig
from goldbot.macro_engine import MacroDataEngine

NOW = datetime(2026, 9, 23, 14, 0, tzinfo=timezone.utc)


def yield_series(last_move_bp: float, level: float = 4.25, scale: float = 1.0):
    idx = pd.date_range(NOW - timedelta(hours=10), NOW, freq="5min", tz="UTC")
    values = np.full(len(idx), level)
    values[-3:] += last_move_bp / 100.0  # move happens inside the last 15 min
    return pd.Series(values * scale, index=idx)


def engine(series=None, error=None, now=NOW):
    def fetch(_ticker):
        if error:
            raise error
        return series
    return MacroDataEngine(StrategyConfig(), fetcher=fetch, clock=lambda: now)


def test_yield_spike_is_bearish_gold():
    eng = engine(yield_series(+2.5))
    eng.refresh()
    snap = eng.snapshot()
    assert round(snap.delta_bp, 6) == 2.5
    assert snap.score == -25


def test_yield_drop_is_bullish_gold():
    eng = engine(yield_series(-3.0))
    eng.refresh()
    assert eng.snapshot().score == 25


def test_small_move_scores_zero():
    eng = engine(yield_series(1.0))
    eng.refresh()
    assert eng.snapshot().score == 0


def test_legacy_x10_quote_normalised():
    eng = engine(yield_series(+2.5, scale=10.0))
    eng.refresh()
    snap = eng.snapshot()
    assert abs(snap.value - 4.275) < 1e-9
    assert snap.score == -25


def test_stale_data_scores_zero():
    eng = engine(yield_series(+5.0), now=NOW + timedelta(hours=2))
    eng.refresh()
    snap = eng.snapshot()
    assert snap.stale and snap.score == 0


def test_fetch_error_is_contained():
    eng = engine(error=ConnectionError("proxy 403"))
    eng.refresh()
    snap = eng.snapshot()
    assert snap.score == 0 and "403" in snap.error
