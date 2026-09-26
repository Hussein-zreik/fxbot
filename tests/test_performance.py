import math
from datetime import datetime, timedelta, timezone

from goldbot.mt5_connector import DealInfo
from goldbot.performance import build_trades, compute_stats, json_safe, summary_text

NOW = datetime(2026, 9, 25, 20, 0, tzinfo=timezone.utc)
MAGIC = 20260926


def deal(pid, entry, side, vol, price, profit, minutes_ago, reason="BOT",
         magic=MAGIC, comment=""):
    return DealInfo(ticket=pid * 10 + len(entry), position_id=pid,
                    time=NOW - timedelta(minutes=minutes_ago), side=side, entry=entry,
                    volume=vol, price=price, profit=profit, reason=reason, magic=magic,
                    symbol="XAUUSD", comment=comment)


def trade(pid, side, profit, minutes_ago, reason="TP", comment="goldbot +70"):
    close_side = "SELL" if side == "BUY" else "BUY"
    return [deal(pid, "IN", side, 0.1, 2600, -0.7, minutes_ago + 60, comment=comment),
            deal(pid, "OUT", close_side, 0.1, 2610, profit + 0.7, minutes_ago,
                 reason=reason, magic=0)]


def test_build_trades_nets_commission_and_reads_score():
    trades = build_trades(trade(1, "BUY", 50.0, 30), MAGIC)
    t = trades[0]
    assert t.side == "BUY" and t.exit_reason == "TP" and t.score == 70
    assert math.isclose(t.profit, 50.0) and t.hold_minutes == 60


def test_partial_close_folds_into_one_trade():
    deals = [deal(2, "IN", "BUY", 0.2, 2600, 0, 120),
             deal(2, "OUT", "SELL", 0.1, 2595, -50, 90, reason="BOT"),   # news trim
             deal(2, "OUT", "SELL", 0.1, 2620, 200, 30, reason="TP")]
    t = build_trades(deals, MAGIC)[0]
    assert t.volume == 0.2 and t.profit == 150 and t.close_price == 2607.5


def test_ignores_open_and_foreign_positions():
    still_open = [deal(3, "IN", "BUY", 0.2, 2600, 0, 60),
                  deal(3, "OUT", "SELL", 0.1, 2605, 50, 30)]
    manual = trade(4, "BUY", 10, 30)
    for d in manual:
        d.magic = 999
    assert build_trades(still_open + manual, MAGIC) == []


def test_stats_and_drawdown():
    deals = (trade(1, "BUY", 100, 300) + trade(2, "BUY", -40, 200, "SL")
             + trade(3, "SELL", -60, 100, "SL") + trade(4, "SELL", 80, 10))
    s = compute_stats(build_trades(deals, MAGIC), NOW)
    assert s["trades"] == 4 and s["wins"] == 2 and s["win_rate"] == 50.0
    assert s["net"] == 80.0 and s["profit_factor"] == 1.8
    assert s["max_drawdown"] == 100.0                  # +100 -> 0
    assert s["exits"] == {"TP": 2, "SL": 2}
    assert s["by_side"]["SELL"]["net"] == 20.0
    assert [p["v"] for p in s["curve"]] == [100.0, 60.0, 0.0, 80.0]


def test_period_filter():
    deals = trade(1, "BUY", 100, 60 * 24 * 10) + trade(2, "BUY", 20, 60)
    trades = build_trades(deals, MAGIC)
    assert compute_stats(trades, NOW, 7)["trades"] == 1
    assert compute_stats(trades, NOW, None)["trades"] == 2


def test_signal_attribution():
    deals = trade(1, "BUY", 100, 300) + trade(2, "BUY", -50, 100, "SL")
    meta = {"1": {"total": 70, "parts": {"yield": 25, "trend": 20, "ai_news": 0}},
            "2": {"total": 60, "parts": {"yield": 25, "trend": -10, "ai_news": 10}}}
    sig = compute_stats(build_trades(deals, MAGIC, meta), NOW)["signals"]
    assert sig["yield"]["agreed"] == {"n": 2, "win_rate": 50.0, "net": 50.0, "avg": 25.0}
    assert sig["trend"]["agreed"]["net"] == 100 and sig["trend"]["not_agreed"]["net"] == -50
    assert sig["ai_news"]["agreed"]["n"] == 1


def test_no_losses_profit_factor_is_json_safe():
    s = compute_stats(build_trades(trade(1, "BUY", 10, 30), MAGIC), NOW)
    assert s["profit_factor"] == math.inf
    assert json_safe(s)["profit_factor"] is None


def test_summary_text():
    s = compute_stats(build_trades(trade(1, "BUY", 42.5, 30), MAGIC,
                                   {"1": {"parts": {"yield": 25}}}), NOW)
    text = summary_text(s, "Daily", "USD")
    assert "+42.50 USD" in text and "100%" in text and "Best signal: yield" in text
    assert "No closed trades" in summary_text(compute_stats([], NOW), "Daily", "USD")
