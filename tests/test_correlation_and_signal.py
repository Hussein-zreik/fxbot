from datetime import datetime, timezone

import numpy as np

from goldbot.config import StrategyConfig
from goldbot.correlation_engine import invert_fx_bars, score_correlation
from goldbot.macro_engine import YieldSnapshot
from goldbot.mt5_connector import BUY, SELL
from goldbot.signal_model import build_signal
from goldbot.technical_engine import TechnicalSnapshot
from tests.conftest import make_bars

START = datetime(2026, 9, 23, tzinfo=timezone.utc)
CFG = StrategyConfig()


def metals(gold_final: float, silver_final: float, usd_final: float, n: int = 80):
    rng = np.random.default_rng(7)
    shocks = rng.normal(0, 0.0008, n)
    gold = 2600 * np.exp(np.cumsum(shocks))
    silver = 30 * np.exp(np.cumsum(shocks * 1.2 + rng.normal(0, 0.0002, n)))
    usd = np.full(n, 1.0 / 1.10)
    gold[-1] = gold[-2] * gold_final
    silver[-1] = silver[-2] * silver_final
    usd[-1] = usd[-2] * usd_final
    return (make_bars(gold, START, 15, spread=0.5),
            make_bars(silver, START, 15, spread=0.005),
            make_bars(usd, START, 15, spread=0.0001))


def test_bullish_confluence_scores_silver_and_dxy():
    g, s, u = metals(1.02, 1.03, 0.995)  # metals break up, USD breaks down
    snap = score_correlation(g, s, u, CFG)
    assert snap.correlation > CFG.min_correlation
    assert snap.gold_break == 1 and snap.silver_break == 1 and snap.usd_break == -1
    assert snap.silver_score == 30 and snap.dxy_score == 30
    assert not snap.divergence


def test_divergence_zeroes_silver_block():
    g, s, u = metals(1.02, 0.999, 0.995)  # silver fails to confirm
    snap = score_correlation(g, s, u, CFG)
    assert snap.divergence and snap.silver_score == 0


def test_usd_must_move_inversely():
    g, s, u = metals(1.02, 1.03, 1.0)  # USD flat
    snap = score_correlation(g, s, u, CFG)
    assert snap.silver_score == 0 and snap.dxy_score == 0


def test_invert_fx_swaps_extremes():
    fx = make_bars([1.10, 1.12], START, 15, spread=0.01)
    inv = invert_fx_bars(fx)
    assert (inv["high"] >= inv["low"]).all()
    assert abs(inv["close"].iat[-1] - 1 / 1.12) < 1e-12


# ---------------------------------------------------------------------- #
def ys(score):
    return YieldSnapshot(score=score, stale=False)


def tech(long=False, short=False, ok=True):
    return TechnicalSnapshot(retest_long=long, retest_short=short, ok=ok, atr=3.0)


def corr(dxy=0, silver=0, divergence=False, gold_break=0):
    from goldbot.correlation_engine import CorrelationSnapshot
    return CorrelationSnapshot(dxy_score=dxy, silver_score=silver,
                               divergence=divergence, gold_break=gold_break, ok=True)


def test_full_bull_stack_is_buy_100():
    sig = build_signal(ys(30), corr(30, 30), tech(long=True), CFG)
    assert sig.total == 100 and sig.direction == BUY


def test_two_blocks_plus_vwap_reaches_threshold():
    sig = build_signal(ys(-30), corr(dxy=-30), tech(short=True), CFG)
    assert sig.total == -70 and sig.direction == SELL


def test_vwap_only_counts_with_bias():
    sig = build_signal(ys(30), corr(dxy=30), tech(short=True), CFG)
    assert sig.vwap_score == 0 and sig.total == 60
    assert sig.direction is None and "no VWAP retest" in sig.blocked_by


def test_one_block_is_not_enough():
    sig = build_signal(ys(30), corr(), tech(long=True), CFG)
    assert sig.total == 40 and sig.direction is None


def test_divergence_blocks_breakout_direction():
    sig = build_signal(ys(30), corr(dxy=30, divergence=True, gold_break=1),
                       tech(long=True), CFG)
    assert sig.direction is None
    assert any("divergence" in b for b in sig.blocked_by)
