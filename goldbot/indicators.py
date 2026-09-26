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
