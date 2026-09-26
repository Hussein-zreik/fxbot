from datetime import datetime, timezone

import numpy as np

from goldbot.config import TrendConfig
from goldbot.indicators import adx, swing_structure
from goldbot.trend_engine import TrendEngine, classify_trend
from tests.conftest import make_bars

START = datetime(2026, 6, 1, tzinfo=timezone.utc)
CFG = TrendConfig()


def zigzag(drift: float, n: int = 400, amp: float = 20.0):
    """Trending market that still makes clear swings (pullbacks)."""
    i = np.arange(n)
    return 2000 + drift * i + amp * np.sin(i / 4.0)


def noise(seed: int = 1, n: int = 400):
    """Directionless market: random wiggles around a flat level."""
    return 2000 + np.random.default_rng(seed).normal(0, 3, n)


def test_uptrend_detected():
    df = make_bars(zigzag(+1.5), START, 60)
    t = classify_trend(df, "H1", CFG)
    assert t.ema_dir == 1 and t.structure == 1 and t.adx >= CFG.adx_min
    assert t.direction == 1


def test_downtrend_detected():
    t = classify_trend(make_bars(zigzag(-1.5), START, 60), "H1", CFG)
    assert t.direction == -1


def test_range_is_not_a_trend():
    for seed in range(5):
        t = classify_trend(make_bars(noise(seed), START, 60, spread=1.0), "H1", CFG)
        assert t.direction == 0


def test_insufficient_history():
    t = classify_trend(make_bars(zigzag(1.5, n=100), START, 60), "H4", CFG)
    assert not t.ok and t.direction == 0


def test_adx_high_in_trend_low_in_range():
    assert adx(make_bars(zigzag(1.5), START, 60)).iat[-1] > 25
    assert adx(make_bars(noise(), START, 60, spread=1.0)).iat[-1] < 20


def test_swing_structure_directions():
    assert swing_structure(make_bars(zigzag(1.5), START, 60)) == 1
    assert swing_structure(make_bars(zigzag(-1.5), START, 60)) == -1


def test_equal_highs_count_as_one_swing():
    # Two adjacent bars touching the same high must not be compared as two swings.
    df = make_bars(zigzag(1.5), START, 60)
    peak = df["high"].iloc[:-10].idxmax()
    df.loc[peak + 1, "high"] = df.loc[peak, "high"]
    assert swing_structure(df) == 1


class _Conn:
    def __init__(self, frames):
        self.frames = frames

    def get_rates(self, symbol, tf, count):
        return self.frames.get(tf)


def test_engine_scores_and_filter():
    up = make_bars(zigzag(1.5), START, 60)
    down = make_bars(zigzag(-1.5), START, 60)
    snap = TrendEngine(_Conn({"H4": up, "H1": up}), CFG, "XAUUSD").evaluate()
    assert snap.score == 20 and snap.filter_dir == 1
    mixed = TrendEngine(_Conn({"H4": down, "H1": up}), CFG, "XAUUSD").evaluate()
    assert mixed.score == 0 and mixed.filter_dir == -1
    assert mixed.summary() == "H4 DOWN H1 UP"
