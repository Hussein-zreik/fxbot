"""End-to-end loop iterations against the fake connector (no MT5 needed)."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import numpy as np

from goldbot.config import AppConfig
from goldbot.macro_engine import MacroDataEngine
from goldbot.mt5_connector import OrderResult, Tick
from goldbot.news_filter import NewsFilterEngine
from goldbot.orchestrator import BotOrchestrator
from goldbot.mt5_connector import PositionInfo
from goldbot.risk.livegate import GateResult
from goldbot.risk.suite import build_suite
from goldbot.risk.types import RiskDecision
from goldbot.risk_manager import RiskManager
from goldbot.state import StateStore
from tests.conftest import FakeConnector, long_position, make_bars

NOW = datetime(2026, 9, 23, 13, 0, 30, tzinfo=timezone.utc)  # Wed 13:00 UTC


def bullish_market():
    """Gold/silver break up on M15, EURUSD breaks up (USD down), gold near VWAP on M5."""
    rng = np.random.default_rng(3)
    n15 = 80
    shocks = rng.normal(0, 0.0008, n15)
    gold15 = 2600 * np.exp(np.cumsum(shocks))
    silver15 = 30 * np.exp(np.cumsum(shocks * 1.2 + rng.normal(0, 0.0002, n15)))
    eur15 = np.full(n15, 1.10)
    gold15[-1] *= 1.02
    silver15[-1] *= 1.03
    eur15[-1] *= 1.006
    start15 = NOW - timedelta(minutes=15 * n15)

    # M5: flat around 2650 all day, last close just above VWAP.
    n5 = 150
    gold5 = np.full(n5, 2650.0)
    gold5[-1] = 2651.0
    start5 = NOW.replace(hour=0, minute=0, second=0) + timedelta(minutes=5)

    return {
        ("XAUUSD", "M15"): make_bars(gold15, start15, 15),
        ("XAGUSD", "M15"): make_bars(silver15, start15, 15, spread=0.005),
        ("EURUSD", "M15"): make_bars(eur15, start15, 15, spread=0.0001),
        ("XAUUSD", "M5"): make_bars(gold5, start5, 5, spread=2.0),
        ("XAUUSD", "M1"): make_bars(np.full(120, 2651.0), NOW - timedelta(minutes=120), 1,
                                    spread=0.4),
    }


def yield_drop(_ticker):
    import pandas as pd
    idx = pd.date_range(NOW - timedelta(hours=6), NOW, freq="5min", tz="UTC")
    vals = np.full(len(idx), 4.25)
    vals[-3:] -= 0.03  # -3 bp -> bullish gold
    return pd.Series(vals, index=idx)


def trend_bars(drift: float, minutes: int):
    i = np.arange(400)
    closes = 2000 + drift * i + 20 * np.sin(i / 4.0)
    return make_bars(closes, NOW - timedelta(minutes=minutes * 400), minutes)


def build(tmp_path, account, calendar=None, now=NOW, extra_rates=None, independent=2651.0,
          mode="paper", live_gate=None, conn_cls=FakeConnector, dry_run=True):
    cfg = AppConfig(state_file=str(tmp_path / "state.json"),
                    journal_file=str(tmp_path / "journal.csv"))
    cfg.news.cache_file = str(tmp_path / "cal.json")
    cfg.risk.db_file = str(tmp_path / "risk.db")
    cfg.risk.kill_flag_file = str(tmp_path / "ops" / "KILL")
    cfg.risk.changelog_file = str(tmp_path / "CHANGELOG.md")
    conn = conn_cls({**bullish_market(), **(extra_rates or {})},
                    Tick(2651.0, 2651.3, 1), account, dry_run=dry_run)
    clock = lambda: now  # noqa: E731
    state = StateStore(cfg.state_file)
    macro = MacroDataEngine(cfg.strategy, fetcher=yield_drop, clock=clock)
    macro.refresh()

    class Resp:
        def raise_for_status(self): pass
        def json(self): return calendar or []

    news = NewsFilterEngine(cfg.news, http_get=lambda *a, **k: Resp(), clock=clock)
    risk = RiskManager(cfg.risk)
    suite = build_suite(cfg)
    feed = (lambda symbol: independent) if independent is not None else None
    bot = BotOrchestrator(cfg, conn, macro, news, risk, state, suite, clock=clock,
                          mode=mode, live_gate=live_gate, independent_feed=feed)
    bot.setup()
    return bot, conn


def test_full_bullish_stack_places_buy(tmp_path, account):
    bot, conn = build(tmp_path, account)
    bot.step()
    opens = [s for s in conn.sent if s[0] == "open"]
    assert len(opens) == 1 and opens[0][1] == "BUY"
    _, _, volume, sl, tp = opens[0]
    assert sl < 2651.3 < tp and volume > 0
    assert volume * (2651.3 - sl) * 100 <= 25.0001            # <= 0.25% of $10,000
    journal = (tmp_path / "journal.csv").read_text()
    assert "BUY" in journal and ",70," in journal  # 25+20+20+5, trend unknown

    bot.step()  # same closed bar -> no duplicate order
    assert len([s for s in conn.sent if s[0] == "open"]) == 1


def test_news_blackout_blocks_entry(tmp_path, account):
    cpi = [{"title": "CPI m/m", "country": "USD", "impact": "High",
            "date": (NOW + timedelta(minutes=20)).isoformat()}]
    bot, conn = build(tmp_path, account, calendar=cpi)
    bot.step()
    assert not [s for s in conn.sent if s[0] == "open"]
    assert "news blackout" in (tmp_path / "journal.csv").read_text()


def test_pre_news_moves_winner_to_breakeven(tmp_path, account):
    cpi = [{"title": "NFP", "country": "USD", "impact": "High",
            "date": (NOW + timedelta(minutes=20)).isoformat()}]
    bot, conn = build(tmp_path, account, calendar=cpi)
    conn.open_positions = [long_position(price_open=2640.0, sl=2630.0)]
    bot.step()
    mods = [s for s in conn.sent if s[0] == "modify"]
    assert mods and abs(mods[0][2] - 2640.10) < 1e-9  # entry + 10 points
    bot.step()  # handled once per news window
    assert len([s for s in conn.sent if s[0] == "modify"]) == 1


def test_pre_news_halves_loser(tmp_path, account):
    cpi = [{"title": "FOMC", "country": "USD", "impact": "High",
            "date": (NOW + timedelta(minutes=5)).isoformat()}]
    bot, conn = build(tmp_path, account, calendar=cpi)
    conn.open_positions = [long_position(price_open=2660.0, sl=2650.0, volume=0.20)]
    bot.step()
    assert ("close", 1, 0.10) in conn.sent


def test_a_fast_equity_drop_trips_the_kill_switch_and_flattens(tmp_path, account):
    bot, conn = build(tmp_path, account)
    conn.open_positions = [long_position()]
    bot.step()                                      # records equity 10,000
    conn.acc = replace(account, equity=9_650.0)     # -3.5% inside the same 15 minutes
    bot._last_bar = None
    conn.sent.clear()
    bot.step()
    assert ("close", 1, 0.10) in conn.sent
    assert not [s for s in conn.sent if s[0] == "open"]
    assert bot.suite.kill.is_killed() and "PNL_DROP" in bot.suite.kill.reasons()
    assert bot.status()["mode"] == "KILLED"


def test_a_slow_two_percent_loss_blocks_entries_but_leaves_positions_alone(tmp_path, account):
    bot, conn = build(tmp_path, account)
    conn.open_positions = [long_position()]
    bot.step()
    later = NOW + timedelta(minutes=20)
    bot._clock = lambda: later
    conn.acc = replace(account, equity=9_790.0)     # -2.1%, spread over 20 minutes
    conn.sent.clear()
    bot.step()
    assert not [s for s in conn.sent if s[0] == "close"]          # CLAUDE.md: stop entries only
    status = bot.status()
    assert status["halted"] and "DAILY_LOSS" in status["halt_reason"]
    conn.open_positions = []
    bot.cfg.execution.cooldown_minutes = 0          # isolate the loss limit from the cooldown
    bot._last_bar = None
    bot.step()
    assert not [s for s in conn.sent if s[0] == "open"]
    assert "REJECTED DAILY_LOSS" in (tmp_path / "journal.csv").read_text()


def test_outside_session_skips(tmp_path, account):
    late = NOW.replace(hour=22)
    bot, conn = build(tmp_path, account, now=late)
    bot.step()
    assert not [s for s in conn.sent if s[0] == "open"]


def test_uptrend_adds_trend_points(tmp_path, account):
    up = {("XAUUSD", "H4"): trend_bars(1.5, 240), ("XAUUSD", "H1"): trend_bars(1.5, 60)}
    bot, conn = build(tmp_path, account, extra_rates=up)
    bot.step()
    assert [s for s in conn.sent if s[0] == "open"][0][1] == "BUY"
    journal = (tmp_path / "journal.csv").read_text()
    assert ",90," in journal and "H4 UP H1 UP" in journal   # 70 + 20 trend


def test_h4_downtrend_blocks_buy(tmp_path, account):
    mixed = {("XAUUSD", "H4"): trend_bars(-1.5, 240), ("XAUUSD", "H1"): trend_bars(1.5, 60)}
    bot, conn = build(tmp_path, account, extra_rates=mixed)
    bot.step()
    assert not [s for s in conn.sent if s[0] == "open"]
    assert "against the higher-timeframe trend" in (tmp_path / "journal.csv").read_text()


# --------------------------------------------------------------------------- #
# Dashboard commands, AI shock pause and trend-reversal exits
# --------------------------------------------------------------------------- #
def opens(conn):
    return [s for s in conn.sent if s[0] == "open"]


def test_dashboard_pause_blocks_and_resume_allows(tmp_path, account):
    bot, conn = build(tmp_path, account)
    bot.submit_command("pause")
    bot.step()
    assert not opens(conn) and bot.status()["mode"] == "PAUSED"
    assert "paused from dashboard" in (tmp_path / "journal.csv").read_text()

    bot.submit_command("resume")
    bot._last_bar = None
    bot.step()
    assert len(opens(conn)) == 1 and bot.status()["mode"] == "DRY-RUN"


def test_dashboard_close_all_closes_and_pauses(tmp_path, account):
    bot, conn = build(tmp_path, account)
    conn.open_positions = [long_position(ticket=7)]
    bot.submit_command("close_all")
    bot.step()
    assert ("close", 7, 0.10) in conn.sent
    assert bot.paused
    assert any("Closed 1 position" in e["text"] for e in bot.status()["events"])


def test_status_is_json_safe(tmp_path, account):
    import json
    bot, conn = build(tmp_path, account)
    conn.open_positions = [long_position()]
    bot.step()
    status = bot.status()
    json.dumps(status, allow_nan=False)          # NaN would break the phone page
    assert status["account"]["equity"] == 10_000.0
    assert status["last_bar"]["parts"]["yield"] == 25
    assert status["positions"][0]["side"] == "BUY"


class ShockAI:
    active = True

    def __init__(self, shock=True):
        from goldbot.sentiment_engine import SentimentSnapshot
        self.snap = SentimentSnapshot(shock_active=shock,
                                      shock_headline="Emergency Fed meeting called",
                                      shock_until=NOW + timedelta(minutes=40))

    def snapshot(self):
        return self.snap

    def start(self):
        pass

    def stop(self):
        pass


def test_ai_shock_pauses_entries_and_protects_trades(tmp_path, account):
    bot, conn = build(tmp_path, account)
    bot.sentiment = ShockAI()
    conn.open_positions = [long_position(price_open=2640.0, sl=2630.0)]
    bot.cfg.execution.max_open_positions = 2
    bot.step()
    assert not opens(conn)
    assert "AI shock pause" in (tmp_path / "journal.csv").read_text()
    assert [s for s in conn.sent if s[0] == "modify"]       # winner -> breakeven
    bot._last_bar = None
    bot.step()                                              # protected only once
    assert len([s for s in conn.sent if s[0] == "modify"]) == 1


def test_h1_trend_reversal_closes_trade(tmp_path, account):
    rates = {("XAUUSD", "H4"): trend_bars(1.5, 240), ("XAUUSD", "H1"): trend_bars(-1.5, 60)}
    bot, conn = build(tmp_path, account, extra_rates=rates)
    conn.open_positions = [long_position(ticket=9)]
    bot.step()
    assert ("close", 9, 0.10) in conn.sent
    assert any("Trend exit" in e["text"] for e in bot.status()["events"])


def test_ranging_h1_does_not_close_trade(tmp_path, account):
    bot, conn = build(tmp_path, account)            # no H1 data -> RANGE
    conn.open_positions = [long_position(ticket=9)]
    bot.step()
    assert not [s for s in conn.sent if s[0] == "close"]


# --------------------------------------------------------------------------- #
# Telegram alerts and performance
# --------------------------------------------------------------------------- #
class Recorder:
    active = True

    def __init__(self):
        self.msgs = []

    def send(self, text, category):
        self.msgs.append((category, text))
        return True

    def start(self):
        pass

    def stop(self):
        pass


def closed_deals(pid, profit, minutes_ago=30, now=NOW):
    from goldbot.mt5_connector import DealInfo
    t = now - timedelta(minutes=minutes_ago)
    return [DealInfo(pid * 10, pid, t - timedelta(minutes=45), "BUY", "IN", 0.1, 2600,
                     -0.7, "BOT", 20260926, "XAUUSD", "goldbot +70"),
            DealInfo(pid * 10 + 1, pid, t, "SELL", "OUT", 0.1, 2610, profit + 0.7, "TP",
                     0, "XAUUSD", "")]


def test_trade_alerts_open_and_close(tmp_path, account):
    bot, conn = build(tmp_path, account)
    bot.notifier = rec = Recorder()
    bot.step()
    assert any(c == "trade" and "BUY" in t and "score +70" in t for c, t in rec.msgs)

    conn.open_positions = [long_position(ticket=5)]
    bot.step()
    conn.open_positions = []
    conn.deals = closed_deals(5, 88.0)
    bot.step()
    assert any(c == "trade" and "Closed BUY 0.1 #5: +88.00 USD (take profit" in t
               for c, t in rec.msgs)


def test_risk_and_shock_alerts_sent_once(tmp_path, account):
    bot, conn = build(tmp_path, account)
    bot.notifier = rec = Recorder()
    bot.sentiment = ShockAI()
    bot.step()
    bot._last_bar = None
    bot.step()
    shocks = [t for c, t in rec.msgs if c == "shock" and "AI SHOCK" in t]
    assert len(shocks) == 1 and "Emergency Fed meeting" in shocks[0]

    conn.acc = replace(account, equity=9_790.0)
    bot.step()
    assert any(c == "risk" and "DAILY_LOSS" in t for c, t in rec.msgs)


def test_connection_lost_and_restored_alerts(tmp_path, account):
    bot, conn = build(tmp_path, account)
    bot.notifier = rec = Recorder()
    conn.ensure_connected = lambda: False
    bot.step()
    bot.step()
    conn.ensure_connected = lambda: True
    bot.step()
    system = [t for c, t in rec.msgs if c == "system"]
    assert len([t for t in system if "Lost connection" in t]) == 1
    assert any("restored" in t for t in system)


def test_daily_and_weekly_summary(tmp_path, account):
    friday = datetime(2026, 9, 25, 21, 10, tzinfo=timezone.utc)
    bot, conn = build(tmp_path, account, now=friday)
    bot.notifier = rec = Recorder()
    conn.deals = closed_deals(1, 120.0, minutes_ago=120, now=friday)
    bot.step()
    bot.step()                                        # only once per day
    summaries = [t for c, t in rec.msgs if c == "summary"]
    assert len(summaries) == 2
    assert "Daily summary 2026-09-25" in summaries[0] and "+120.00 USD" in summaries[0]
    assert "Weekly summary" in summaries[1]


def test_performance_endpoint_data(tmp_path, account):
    import json
    bot, conn = build(tmp_path, account)
    conn.deals = closed_deals(1, 50.0) + closed_deals(2, -20.0, minutes_ago=10)
    bot.step()
    perf = bot.performance()
    json.dumps(perf, allow_nan=False)
    week = perf["periods"]["7d"]
    assert perf["ready"] and week["trades"] == 2 and week["net"] == 30.0
    assert perf["recent"][0]["position_id"] == 2      # newest first


def test_entry_breakdown_saved_for_attribution(tmp_path, account):
    from goldbot.mt5_connector import OrderResult
    bot, conn = build(tmp_path, account)
    conn.open_market = lambda *a, **k: OrderResult(ok=True, ticket=777)
    bot.step()
    meta = bot.state.get("trade_meta")["777"]
    assert meta["total"] == 70 and meta["parts"]["yield"] == 25


# --------------------------------------------------------------------------- #
# Prime directives at whole-bot level (CLAUDE.md section 0)
# --------------------------------------------------------------------------- #
class FillConn(FakeConnector):
    """A fake broker that really opens a position, so post-fill checks can run."""
    drop_stop = False
    slip = 0.0

    def open_market(self, symbol, side, volume, sl, tp, comment):
        self.sent.append(("open", side, volume, sl, tp))
        self.open_positions.append(PositionInfo(
            ticket=900, symbol=symbol, side=side, volume=volume,
            price_open=2651.3 + self.slip, sl=0.0 if self.drop_stop else sl, tp=tp,
            profit=0.0, magic=20260926))
        return OrderResult(ok=True, retcode=10009, ticket=900, price=2651.3, dry_run=False)


def journal(tmp_path):
    return (tmp_path / "journal.csv").read_text()


def test_the_engine_vetoes_everything(tmp_path, account):
    bot, conn = build(tmp_path, account)
    bot.suite.engine.approve = lambda intent, state: RiskDecision(False, "TEST_VETO", "no")
    bot.step()
    assert not opens(conn)
    assert "REJECTED TEST_VETO" in journal(tmp_path)


def test_size_comes_from_the_engine_never_from_the_strategy(tmp_path, account):
    bot, conn = build(tmp_path, account)
    bot.step()
    assert opens(conn)[0][2] == 0.04                  # $25 / ~$611 per lot, floored
    decision = bot.suite.store.decisions(1)[0]
    assert decision["approved"] == 1 and decision["size"] == 0.04
    assert decision["strategy"] == "gold_macro"


def test_without_an_independent_feed_every_entry_is_rejected(tmp_path, account):
    bot, conn = build(tmp_path, account, independent=None)
    bot.step()
    assert not opens(conn) and "REJECTED NO_INDEPENDENT_FEED" in journal(tmp_path)


def test_a_dry_run_can_never_trip_the_kill_switch(tmp_path, account):
    bot, conn = build(tmp_path, account)
    for _ in range(6):                                # six dry-run "orders" (retcode 0 each)
        bot._last_bar = None
        bot.step()
    assert len(opens(conn)) == 6 and not bot.suite.kill.is_killed()


def test_kill_flag_flattens_persists_and_needs_a_signed_reset(tmp_path, account):
    bot, conn = build(tmp_path, account)
    conn.open_positions = [long_position()]
    flag = tmp_path / "ops" / "KILL"
    flag.parent.mkdir()
    flag.write_text("")
    bot.step()
    assert ("close", 1, 0.10) in conn.sent and bot.status()["mode"] == "KILLED"
    flag.unlink()
    bot.step()
    assert bot.suite.kill.is_killed()                 # removing the file is not a reset
    conn.sent.clear()
    bot.suite.kill.reset("Flag was a test", "tester", NOW)
    conn.open_positions = []
    bot.step()
    assert not bot.suite.kill.is_killed() and bot.status()["mode"] == "DRY-RUN"
    assert "KILL RESET" in (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")


def test_ladder_l2_blocks_entries_and_l3_kills(tmp_path, account):
    bot, conn = build(tmp_path, account)
    bot.step()                                        # high-water mark 10,000
    bot._clock = lambda: NOW + timedelta(minutes=30)
    conn.acc = replace(account, equity=9_250.0)       # -7.5%
    bot._last_bar = None
    bot.step()
    risk = bot.status()["risk"]
    assert risk["ladder_level"] == 2 and "LADDER_L2" in risk["blocked"]
    conn.open_positions = [long_position()]
    bot._clock = lambda: NOW + timedelta(hours=2)
    conn.acc = replace(account, equity=8_990.0)       # -10.1%
    bot.step()
    assert bot.suite.kill.is_killed() and "LADDER_L3" in bot.suite.kill.reasons()
    assert ("close", 1, 0.10) in conn.sent


def test_a_good_fill_is_booked_and_the_stop_is_verified(tmp_path, account):
    bot, conn = build(tmp_path, account, conn_cls=FillConn, dry_run=False)
    bot.step()
    assert len(opens(conn)) == 1 and not [s for s in conn.sent if s[0] == "close"]
    day = bot._day_key(NOW)
    assert bot.suite.ledger.trades_today(day) == 1
    assert 0 < bot.suite.ledger.risk_for(900) <= 25.0001


def test_a_position_without_a_stop_is_closed_immediately(tmp_path, account):
    bot, conn = build(tmp_path, account, conn_cls=FillConn, dry_run=False)
    conn.drop_stop = True
    bot.step()
    assert ("close", 900, opens(conn)[0][2]) in conn.sent
    assert any("NO STOP-LOSS" in e["text"] for e in bot.status()["events"])


def test_a_fill_over_ten_percent_above_planned_risk_is_closed(tmp_path, account):
    bot, conn = build(tmp_path, account, conn_cls=FillConn, dry_run=False)
    conn.slip = 3.0                                   # filled $3 worse than planned
    bot.step()
    assert any(s[0] == "close" and s[1] == 900 for s in conn.sent)
    assert any("more than 10% above" in e["text"] for e in bot.status()["events"])


def test_a_closed_trade_is_booked_with_its_r_multiple(tmp_path, account):
    bot, conn = build(tmp_path, account, conn_cls=FillConn, dry_run=False)
    bot.step()
    risk = bot.suite.ledger.risk_for(900)
    conn.open_positions = []
    conn.deals = closed_deals(900, -risk)             # lost exactly 1R
    bot.step()
    assert bot.suite.ledger.streak().consecutive_losses == 1
    assert bot.suite.ledger.losing_today(bot._day_key(NOW)) == 1
    assert any("-1.00R" in e["text"] for e in bot.status()["events"])


def test_unbooked_closes_wait_for_the_broker_history(tmp_path, account):
    bot, conn = build(tmp_path, account, conn_cls=FillConn, dry_run=False)
    bot.step()
    conn.open_positions = []                          # closed, but no deals in history yet
    bot.step()
    assert bot.suite.ledger.streak().consecutive_losses == 0 and bot._pending_close
    conn.deals = closed_deals(900, -20.0)
    bot.step()
    assert not bot._pending_close and bot.suite.ledger.streak().consecutive_losses == 1


def test_live_gate_failure_locks_the_bot_out_of_sending_orders(tmp_path, account):
    gate = lambda login: GateResult(False, ["TYPED: type exactly 'LIVE 1'"])  # noqa: E731
    bot, conn = build(tmp_path, account, mode="live", live_gate=gate, dry_run=False)
    assert conn.dry_run is True and bot.live_gate_ok is False


def test_live_on_a_real_account_is_refused_while_the_stage_is_demo(tmp_path, account):
    real = replace(account, is_demo=False)
    bot, conn = build(tmp_path, real, mode="live", dry_run=False,
                      live_gate=lambda login: GateResult(True))
    assert bot.live_gate_ok and conn.dry_run is False
    bot.step()
    assert not opens(conn) and "REJECTED STAGE_MISMATCH" in journal(tmp_path)


def test_paper_mode_refuses_a_real_account(tmp_path, account):
    real = replace(account, is_demo=False)
    bot, conn = build(tmp_path, real, mode="paper", dry_run=False)
    bot.step()
    assert not opens(conn) and "REJECTED MODE_MISMATCH" in journal(tmp_path)


def test_status_publishes_the_risk_state(tmp_path, account):
    import json
    bot, conn = build(tmp_path, account)
    bot.step()
    risk = bot.status()["risk"]
    json.dumps(bot.status(), allow_nan=False)
    assert risk["stage"] == "demo" and risk["per_trade_pct"] == 0.25
    assert risk["last_decision"]["approved"] is True and risk["mode"] == "paper"
