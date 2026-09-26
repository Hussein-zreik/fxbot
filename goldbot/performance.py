"""Trade performance: rebuild closed trades from MT5 deals and score them.

A *trade* is one MT5 position opened by the bot (entry deal carries the bot's
magic number). Partial closes (e.g. the pre-news 50 % trim) are folded into
the same trade, so its P/L is the true net result including commission and
swap.

Signal attribution answers "which signals worked?": for every scored block
(yields, dollar, silver, trend, AI news, VWAP) the trades are split into
those where the block AGREED with the trade direction and those where it did
not (neutral or against). A block whose "agreed" trades clearly beat its
"not agreed" trades is earning its weight. Small samples are noisy, so the
page shows the trade count next to every number.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional

BLOCKS = ("yield", "dollar", "silver", "trend", "ai_news", "vwap")
_SCORE_RE = re.compile(r"([+-]\d+)\s*$")


@dataclass
class ClosedTrade:
    position_id: int
    side: str
    volume: float
    open_time: datetime
    close_time: datetime
    open_price: float
    close_price: float
    profit: float
    exit_reason: str
    score: Optional[int] = None
    parts: Dict[str, int] = field(default_factory=dict)

    @property
    def hold_minutes(self) -> float:
        return (self.close_time - self.open_time).total_seconds() / 60

    def to_json(self) -> dict:
        d = asdict(self)
        d["open_time"] = self.open_time.isoformat()
        d["close_time"] = self.close_time.isoformat()
        d["profit"] = round(self.profit, 2)
        return d


def build_trades(deals: Iterable, magic: int,
                 meta: Optional[Dict[str, dict]] = None) -> List[ClosedTrade]:
    """Group deals by position and keep fully closed bot positions."""
    meta = meta or {}
    by_pos = defaultdict(list)
    for d in deals:
        by_pos[d.position_id].append(d)

    trades = []
    for pid, group in by_pos.items():
        group.sort(key=lambda d: d.time)
        entries = [d for d in group if d.entry == "IN"]
        exits = [d for d in group if d.entry in ("OUT", "OUT_BY", "INOUT")]
        if not entries or not exits or entries[0].magic != magic:
            continue
        opened = sum(d.volume for d in entries)
        closed = sum(d.volume for d in exits)
        if closed + 1e-9 < opened:
            continue  # still (partly) open
        first = entries[0]
        m = meta.get(str(pid), {})
        score = m.get("total")
        if score is None:
            found = _SCORE_RE.search(first.comment or "")
            score = int(found.group(1)) if found else None
        trades.append(ClosedTrade(
            position_id=pid, side=first.side, volume=opened,
            open_time=first.time, close_time=exits[-1].time,
            open_price=first.price,
            close_price=sum(d.price * d.volume for d in exits) / closed,
            profit=sum(d.profit for d in group),
            exit_reason=exits[-1].reason, score=score,
            parts=dict(m.get("parts", {})),
        ))
    trades.sort(key=lambda t: t.close_time)
    return trades


def _bucket(trades: List[ClosedTrade]) -> dict:
    n = len(trades)
    wins = sum(1 for t in trades if t.profit > 0)
    net = sum(t.profit for t in trades)
    return {"n": n, "win_rate": round(wins / n * 100, 1) if n else None,
            "net": round(net, 2), "avg": round(net / n, 2) if n else None}


def compute_stats(trades: List[ClosedTrade], now: datetime,
                  days: Optional[int] = None) -> dict:
    if days is not None:
        cutoff = now - timedelta(days=days)
        trades = [t for t in trades if t.close_time >= cutoff]
    wins = [t.profit for t in trades if t.profit > 0]
    losses = [t.profit for t in trades if t.profit <= 0]
    gross_win, gross_loss = sum(wins), -sum(losses)

    equity, peak, max_dd, curve = 0.0, 0.0, 0.0, []
    for t in trades:
        equity += t.profit
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        curve.append({"t": t.close_time.isoformat(), "v": round(equity, 2)})

    signals = {}
    for block in BLOCKS:
        agreed, other = [], []
        for t in trades:
            if block not in t.parts:
                continue
            direction = 1 if t.side == "BUY" else -1
            (agreed if t.parts[block] * direction > 0 else other).append(t)
        signals[block] = {"agreed": _bucket(agreed), "not_agreed": _bucket(other)}

    by_side = {side: _bucket([t for t in trades if t.side == side])
               for side in ("BUY", "SELL")}
    exits = defaultdict(int)
    for t in trades:
        exits[t.exit_reason] += 1

    n = len(trades)
    return {
        "days": days,
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / n * 100, 1) if n else None,
        "net": round(sum(t.profit for t in trades), 2),
        "gross_win": round(gross_win, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": (round(gross_win / gross_loss, 2) if gross_loss > 0
                          else (None if not wins else math.inf)),
        "avg_win": round(gross_win / len(wins), 2) if wins else None,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else None,
        "expectancy": round(sum(t.profit for t in trades) / n, 2) if n else None,
        "max_drawdown": round(max_dd, 2),
        "best": round(max((t.profit for t in trades), default=0.0), 2),
        "worst": round(min((t.profit for t in trades), default=0.0), 2),
        "avg_hold_min": round(sum(t.hold_minutes for t in trades) / n, 1) if n else None,
        "by_side": by_side,
        "exits": dict(exits),
        "signals": signals,
        "curve": curve,
    }


def json_safe(obj):
    """Replace inf/NaN (not valid JSON) with None, recursively."""
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [json_safe(v) for v in obj]
    return obj


def summary_text(stats: dict, title: str, currency: str) -> str:
    """Plain-text summary for Telegram."""
    if not stats["trades"]:
        return f"{title}\nNo closed trades."
    pf = stats["profit_factor"]
    pf_txt = "∞" if pf == math.inf else ("n/a" if pf is None else f"{pf:.2f}")
    lines = [
        title,
        f"Net P/L: {stats['net']:+.2f} {currency}",
        f"Trades: {stats['trades']} ({stats['wins']}W / {stats['losses']}L, "
        f"win rate {stats['win_rate']:.0f}%)",
        f"Profit factor: {pf_txt} · Max drawdown: {stats['max_drawdown']:.2f}",
        f"Best {stats['best']:+.2f} · Worst {stats['worst']:+.2f}",
    ]
    ranked = sorted(
        ((b, s["agreed"]) for b, s in stats["signals"].items() if s["agreed"]["n"]),
        key=lambda x: x[1]["net"], reverse=True)
    if ranked:
        best_b, best_s = ranked[0]
        lines.append(f"Best signal: {best_b} ({best_s['n']} trades, "
                     f"{best_s['win_rate']:.0f}% win, {best_s['net']:+.2f})")
    return "\n".join(lines)
