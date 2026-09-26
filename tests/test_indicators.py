import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from goldbot.indicators import (atr, breakout_direction, rate_of_change_pct,
                                rolling_return_correlation, session_vwap,
                                zscore_last)
from tests.conftest import make_bars

START = datetime(2026, 9, 23, tzinfo=timezone.utc)


def test_atr_constant_range():
    df = make_bars([100.0] * 50, START, spread=1.0)  # every bar: high-low = 2
    assert math.isclose(atr(df, 14).iat[-1], 2.0, rel_tol=1e-9)
    assert math.isnan(atr(df, 14).iat[5])  # warm-up


def test_vwap_resets_at_utc_midnight():
    late = datetime(2026, 9, 23, 23, 10, tzinfo=timezone.utc)
    df = make_bars([100.0] * 10 + [200.0] * 10, late)
    vwap = session_vwap(df)
    new_day = df["time"].dt.floor("D") != df["time"].dt.floor("D").iat[0]
    first_new = new_day.idxmax()
    # First bar of the new session: VWAP equals its own typical price.
    row = df.loc[first_new]
    assert math.isclose(vwap.iat[first_new], (row.high + row.low + row.close) / 3)


def test_vwap_zero_volume_falls_back_to_equal_weight():
    df = make_bars([100.0, 102.0, 104.0], START, volume=0.0)
    typical = (df.high + df.low + df.close) / 3
    assert math.isclose(session_vwap(df).iat[-1], typical.mean())


def test_breakout_direction():
    flat = [100.0] * 25
    assert breakout_direction(make_bars(flat + [110.0], START), 20, 3) == 1
    assert breakout_direction(make_bars(flat + [90.0], START), 20, 3) == -1
    assert breakout_direction(make_bars(flat + [100.2], START), 20, 3) == 0
    # A break 5 bars ago is outside a 3-bar lookback.
    old = flat + [110.0] + [110.0] * 4
    assert breakout_direction(make_bars(old, START), 20, 3) == 0


def test_return_correlation():
    rng = np.random.default_rng(1)
    a = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.001, 60))))
    assert rolling_return_correlation(a, a, 30) > 0.999
    assert rolling_return_correlation(a, 1 / a, 30) < -0.999


def test_roc_and_zscore():
    s = pd.Series([100.0, 101.0, 102.0])
    assert math.isclose(rate_of_change_pct(s, 2), 2.0)
    z = zscore_last(pd.Series([0.0, 1.0] * 20 + [10.0]), 40)
    assert z > 5
