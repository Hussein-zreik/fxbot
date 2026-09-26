"""End-to-end loop iterations against the fake connector (no MT5 needed)."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import numpy as np

from goldbot.config import AppConfig
from goldbot.macro_engine import MacroDataEngine
from goldbot.mt5_connector import Tick
from goldbot.news_filter import NewsFilterEngine
from goldbot.orchestrator import BotOrchestrator
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


def build(tmp_path, account, calendar=None, now=NOW, extra_rates=None):
    cfg = AppConfig(state_file=str(tmp_path / "state.json"),
                    journal_file=str(tmp_path / "journal.csv"))
    cfg.news.cache_file = str(tmp_path / "cal.json")
    conn = FakeConnector({**bullish_market(), **(extra_rates or {})},
                         Tick(2651.0, 2651.3, 1), account)
    clock = lambda: now  # noqa: E731
    state = StateStore(cfg.state_file)
    macro = MacroDataEngine(cfg.strategy, fetcher=yield_drop, clock=clock)
    macro.refresh()

    class Resp:
        def raise_for_status(self): pass
        def json(self): return calendar or []

    news = NewsFilterEngine(cfg.news, http_get=lambda *a, **k: Resp(), clock=clock)
    risk = RiskManager(cfg.risk, conn, state, clock=clock)
    bot = BotOrchestrator(cfg, conn, macro, news, risk, state, clock=clock)
    bot.setup()
    return bot, conn


def test_full_bullish_stack_places_buy(tmp_path, account):
    bot, conn = build(tmp_path, account)
    bot.step()
    opens = [s for s in conn.sent if s[0] == "open"]
    assert len(opens) == 1 and opens[0][1] == "BUY"
    _, _, volume, sl, tp = opens[0]
    assert sl < 2651.3 < tp and volume > 0
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


def test_guardrail_flattens_and_halts(tmp_path, account):
    bot, conn = build(tmp_path, account)
    conn.open_positions = [long_position()]
    bot.step()  # records day start balance 10k
    conn.acc = replace(account, equity=9_650.0)
    bot._last_bar = None
    conn.sent.clear()
    bot.step()
    assert ("close", 1, 0.10) in conn.sent
    assert not [s for s in conn.sent if s[0] == "open"]


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
