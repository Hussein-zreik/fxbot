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


def build(tmp_path, account, calendar=None, now=NOW):
    cfg = AppConfig(state_file=str(tmp_path / "state.json"),
                    journal_file=str(tmp_path / "journal.csv"))
    cfg.news.cache_file = str(tmp_path / "cal.json")
    conn = FakeConnector(bullish_market(), Tick(2651.0, 2651.3, 1), account)
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
    assert "BUY" in journal and ",100," in journal

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
