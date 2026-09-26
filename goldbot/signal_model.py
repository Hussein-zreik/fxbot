"""Multi-factor score (-100 .. +100) and the entry decision.

Default ("Balanced") weights:

    yield 25 + DXY 20 + silver 20 + trend 20 (H4 10 + H1 10) + AI news 10 + VWAP 5

VWAP points only count in the direction of the bias formed by the other
blocks. BUY needs total >= buy_threshold, SELL needs total <= sell_threshold.
Hard gates on top of the score:

* VWAP retest in the trade direction (``require_vwap_retest``)
* no gold/silver divergence against the trade (``block_on_divergence``)
* no trade against the higher-timeframe trend (``trend.filter_timeframe``)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .config import StrategyConfig
from .correlation_engine import CorrelationSnapshot
from .macro_engine import YieldSnapshot
from .mt5_connector import BUY, SELL
from .sentiment_engine import SentimentSnapshot
from .technical_engine import TechnicalSnapshot
from .trend_engine import TrendSnapshot


@dataclass
class Signal:
    total: int = 0
    macro: int = 0
    yield_score: int = 0
    dxy_score: int = 0
    silver_score: int = 0
    trend_score: int = 0
    news_score: int = 0
    vwap_score: int = 0
    direction: Optional[str] = None
    blocked_by: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)


def build_signal(ys: YieldSnapshot, cs: CorrelationSnapshot,
                 ts: TechnicalSnapshot, cfg: StrategyConfig,
                 trend: Optional[TrendSnapshot] = None,
                 news: Optional[SentimentSnapshot] = None) -> Signal:
    trend = trend or TrendSnapshot()
    news = news or SentimentSnapshot()
    sig = Signal(yield_score=ys.score, dxy_score=cs.dxy_score,
                 silver_score=cs.silver_score, trend_score=trend.score,
                 news_score=news.score)
    sig.reasons = (list(ys.notes) + list(cs.notes) + list(trend.notes)
                   + list(news.notes) + list(ts.notes))
    sig.macro = (sig.yield_score + sig.dxy_score + sig.silver_score
                 + sig.trend_score + sig.news_score)

    if sig.macro > 0 and ts.retest_long:
        sig.vwap_score = cfg.weight_vwap
    elif sig.macro < 0 and ts.retest_short:
        sig.vwap_score = -cfg.weight_vwap
    sig.total = max(-100, min(100, sig.macro + sig.vwap_score))

    if sig.total >= cfg.buy_threshold:
        candidate, retest = BUY, ts.retest_long
    elif sig.total <= cfg.sell_threshold:
        candidate, retest = SELL, ts.retest_short
    else:
        return sig

    if not ts.ok:
        sig.blocked_by.append("technicals not ready")
    if cfg.require_vwap_retest and not retest:
        sig.blocked_by.append("no VWAP retest")
    want = 1 if candidate == BUY else -1
    if cfg.block_on_divergence and cs.divergence and cs.gold_break == want:
        sig.blocked_by.append("gold/silver divergence (possible fakeout)")
    if trend.filter_dir == -want:
        sig.blocked_by.append("against the higher-timeframe trend")
    if not sig.blocked_by:
        sig.direction = candidate
    return sig
