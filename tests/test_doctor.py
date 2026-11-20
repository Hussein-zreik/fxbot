from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from goldbot.config import AppConfig
from goldbot.doctor import FAIL, OK, SKIP, WARN, format_report, run_checks
from goldbot.mt5_connector import AccountSnapshot, Tick
from tests.conftest import GOLD_SPEC, FakeConnector, make_bars

NOW = datetime(2026, 9, 23, 14, 0, tzinfo=timezone.utc)


class Resp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._p = status, payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def json(self):
        return self._p


CAL = [{"title": "CPI m/m", "country": "USD", "impact": "High",
        "date": "2026-09-24T08:30:00-04:00"}]


def yields(_ticker):
    idx = pd.date_range(NOW - timedelta(hours=2), NOW - timedelta(minutes=5),
                        freq="5min", tz="UTC")
    return pd.Series(np.full(len(idx), 4.25), index=idx)


def web(url, **kwargs):
    if "models" in url:
        return Resp(payload={"data": [{"id": "llama-3.3-70b-versatile"}]})
    return Resp(payload=CAL)


ENV = {"GROQ_API_KEY": "gsk", "TELEGRAM_BOT_TOKEN": "1:T", "TELEGRAM_CHAT_ID": "9",
       "DASHBOARD_TOKEN": "x" * 24}


def cfg():
    c = AppConfig()
    c.dashboard.port = 0           # any free port
    return c


def by_name(checks):
    return {c.name: c for c in checks}


class FakeMT5(FakeConnector):
    def __init__(self, bars=400, algo=True, symbols=("XAUUSD", "XAGUSD", "EURUSD"),
                 connects=True):
        acc = AccountSnapshot(1, "Broker-Demo", "USD", 10_000, 10_000, 10_000, True, True)
        rates = {(s, tf): make_bars(np.full(bars, 100.0), NOW, 5)
                 for s in symbols for tf in ("M5", "M15", "H1", "H4")}
        super().__init__(rates, Tick(2650.0, 2650.3, 1), acc)
        self.symbols, self.algo, self.connects = symbols, algo, connects
        self.closed = False

    def connect(self):
        return self.connects

    def algo_trading_allowed(self):
        return self.algo

    def resolve_symbol(self, base):
        return base if base in self.symbols else None

    def symbol_spec(self, symbol):
        return GOLD_SPEC

    def shutdown(self):
        self.closed = True


def run(env=ENV, factory=None, **kw):
    return by_name(run_checks(cfg(), env=env, connector_factory=factory, http_get=web,
                              http_post=lambda *a, **k: Resp(), yield_fetcher=yields,
                              now=NOW, **kw))


def test_all_green_with_working_mt5():
    mt5 = FakeMT5()
    checks = run(factory=lambda: mt5)
    assert all(c.status in (OK, SKIP, WARN) for c in checks.values()), format_report(
        list(checks.values()))
    assert checks["MT5 account"].status == OK and "DEMO" in checks["MT5 account"].detail
    assert checks["Bars H4"].detail == "400/400 bars"
    assert checks["Symbol gold"].detail == "XAUUSD -> XAUUSD"
    assert checks["Groq API"].status == OK and checks["Telegram"].status == OK
    assert mt5.closed                      # connection always released


def test_missing_secrets_are_warnings_not_failures():
    checks = run(env={})
    assert checks["AI key"].status == WARN
    assert checks["Telegram keys"].status == WARN
    assert checks["Dashboard token"].status == WARN
    assert checks["Groq API"].status == SKIP and checks["Telegram"].status == SKIP
    assert checks["MetaTrader 5"].status == SKIP


def test_mt5_problems_have_plain_hints():
    checks = run(factory=lambda: FakeMT5(algo=False, symbols=("XAGUSD", "EURUSD"), bars=120))
    assert checks["Algo Trading"].status == WARN and "green" in checks["Algo Trading"].hint
    assert checks["Symbol gold"].status == FAIL and "Market Watch" in checks["Symbol gold"].hint


def test_short_history_warns():
    checks = run(factory=lambda: FakeMT5(bars=120))
    assert checks["Bars H4"].status == WARN and "scroll back" in checks["Bars H4"].hint


def test_cannot_connect_is_a_failure():
    checks = run(factory=lambda: FakeMT5(connects=False))
    assert checks["MetaTrader 5"].status == FAIL
    assert "Login to Trade Account" in checks["MetaTrader 5"].hint


def test_bad_groq_key_and_calendar_outage():
    def broken(url, **kwargs):
        if "models" in url:
            return Resp(401)
        raise ConnectionError("no internet")
    checks = by_name(run_checks(cfg(), env=ENV, http_get=broken,
                                http_post=lambda *a, **k: Resp(), yield_fetcher=yields,
                                now=NOW))
    assert checks["Groq API"].status == FAIL
    assert checks["News calendar"].status == FAIL        # fail-closed default
    assert "will not trade" in checks["News calendar"].hint


def test_stale_yield_is_only_a_warning():
    late = NOW + timedelta(hours=6)
    checks = by_name(run_checks(cfg(), env=ENV, http_get=web,
                                http_post=lambda *a, **k: Resp(),
                                yield_fetcher=yields, now=late))
    assert checks["Yield data"].status == WARN


def test_report_text_and_verdict():
    text = format_report(list(run(env={}).values()))
    assert "[WARN] AI key" in text and "-> Re-run: python -m goldbot.setup_wizard" in text
    assert "READY: nothing blocking." in text
    bad = format_report(list(run(factory=lambda: FakeMT5(connects=False)).values()))
    assert "NOT READY: 1 problem(s)" in bad
