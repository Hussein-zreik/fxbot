"""Pure, side-effect-free indicator functions.

All functions take pandas objects with columns ``time`` (tz-aware UTC),
``open``, ``high``, ``low``, ``close``, ``volume`` and operate on CLOSED bars
only; callers are responsible for excluding the still-forming bar.
"""

from __future__ import annotations

import math
from typing import Tuple

import numpy as np
import pandas as pd


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's Average True Range."""
    prev_close = df["close"].shift(1)
    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1.0 / period, adjust=False,
                          min_periods=period).mean()


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """VWAP anchored at 00:00 UTC each day.

    CFD brokers usually report zero real volume, so ``volume`` is expected to
    be tick volume in that case. If a whole session has zero volume, the VWAP
    falls back to an equal-weighted average of typical price.
    """
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    volume = df["volume"].astype(float)
    day = df["time"].dt.floor("D")

    cum_pv = (typical * volume).groupby(day).cumsum()
    cum_v = volume.groupby(day).cumsum()
    equal_weight = typical.groupby(day).cumsum() / typical.groupby(day).cumcount().add(1)

    vwap = cum_pv / cum_v.replace(0.0, np.nan)
    return vwap.fillna(equal_weight)


def donchian(df: pd.DataFrame, period: int = 20) -> Tuple[pd.Series, pd.Series]:
    """Donchian channel of the PREVIOUS ``period`` bars (excludes current)."""
    upper = df["high"].shift(1).rolling(period, min_periods=period).max()
    lower = df["low"].shift(1).rolling(period, min_periods=period).min()
    return upper, lower


def breakout_direction(df: pd.DataFrame, period: int = 20,
                       lookback: int = 3) -> int:
    """Most recent Donchian breakout among the last ``lookback`` closed bars.

    Returns +1 for a close above the prior ``period``-bar high, -1 for a close
    below the prior low, 0 if neither happened recently.
    """
    if len(df) < period + 1:
        return 0
    upper, lower = donchian(df, period)
    close = df["close"]
    for i in range(len(df) - 1, max(len(df) - 1 - lookback, -1), -1):
        if not math.isnan(upper.iat[i]) and close.iat[i] > upper.iat[i]:
            return 1
        if not math.isnan(lower.iat[i]) and close.iat[i] < lower.iat[i]:
            return -1
    return 0


def rolling_return_correlation(a: pd.Series, b: pd.Series,
                               window: int = 30) -> float:
    """Pearson R of the last ``window`` log returns of two aligned series.

    Returns are used instead of raw prices: price-level correlation between
    two trending assets is spuriously high and says little about co-movement.
    """
    ra = np.log(a).diff()
    rb = np.log(b).diff()
    both = pd.concat([ra, rb], axis=1).dropna().tail(window)
    if len(both) < max(10, window // 2):
        return float("nan")
    if both.iloc[:, 0].std() == 0 or both.iloc[:, 1].std() == 0:
        return float("nan")
    return float(both.iloc[:, 0].corr(both.iloc[:, 1]))


def rate_of_change_pct(series: pd.Series, bars: int) -> float:
    """Percentage change between the last value and ``bars`` bars earlier."""
    if len(series) <= bars:
        return float("nan")
    prev = series.iat[-1 - bars]
    if prev == 0:
        return float("nan")
    return float((series.iat[-1] / prev - 1.0) * 100.0)


def zscore_last(series: pd.Series, lookback: int) -> float:
    """Z-score of the last observation vs. the preceding ``lookback`` values."""
    clean = series.dropna()
    if len(clean) < max(10, lookback // 3) + 1:
        return float("nan")
    history = clean.iloc[-lookback - 1:-1]
    std = history.std()
    if not std or math.isnan(std):
        return float("nan")
    return float((clean.iat[-1] - history.mean()) / std)


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average (NaN until ``period`` values exist)."""
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's Average Directional Index: trend STRENGTH, 0-100, no direction."""
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    alpha = 1.0 / period
    atr_w = atr(df, period)
    plus_di = 100 * plus_dm.ewm(alpha=alpha, adjust=False).mean() / atr_w
    minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False).mean() / atr_w
    denom = (plus_di + minus_di).replace(0.0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / denom
    return dx.ewm(alpha=alpha, adjust=False, min_periods=period).mean()


def _distinct_pivots(pivots: pd.Series, strength: int) -> pd.Series:
    """Merge adjacent pivot bars (equal highs/lows) into a single swing point."""
    keep, last = [], None
    for pos, idx in enumerate(pivots.index):
        if last is None or idx - last > strength:
            keep.append(pos)
        last = idx
    return pivots.iloc[keep]


def swing_structure(df: pd.DataFrame, strength: int = 3) -> int:
    """Market structure from the last two confirmed swing highs and lows.

    A swing high is a bar whose high is the highest of the ``strength`` bars
    on each side (so it is only confirmed ``strength`` bars later).
    Returns +1 for higher highs AND higher lows, -1 for lower highs AND lower
    lows, 0 otherwise (range or transition).
    """
    window = 2 * strength + 1
    if len(df) < window * 3:
        return 0
    roll_max = df["high"].rolling(window, center=True).max()
    roll_min = df["low"].rolling(window, center=True).min()
    highs = _distinct_pivots(df["high"][df["high"] == roll_max].dropna(), strength)
    lows = _distinct_pivots(df["low"][df["low"] == roll_min].dropna(), strength)
    if len(highs) < 2 or len(lows) < 2:
        return 0
    hh = highs.iat[-1] > highs.iat[-2]
    hl = lows.iat[-1] > lows.iat[-2]
    lh = highs.iat[-1] < highs.iat[-2]
    ll = lows.iat[-1] < lows.iat[-2]
    if hh and hl:
        return 1
    if lh and ll:
        return -1
    return 0
