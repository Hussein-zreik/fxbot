from datetime import datetime, timedelta, timezone

import pytest
import requests

from goldbot.config import NewsConfig
from goldbot.news_filter import NewsFilterEngine, parse_calendar

# 08:30 New York (EDT, UTC-4) = 12:30 UTC
SAMPLE = [
    {"title": "CPI m/m", "country": "USD", "date": "2026-09-23T08:30:00-04:00",
     "impact": "High", "forecast": "0.3%", "previous": "0.2%"},
    {"title": "Crude Oil Inventories", "country": "USD",
     "date": "2026-09-23T10:30:00-04:00", "impact": "Medium"},
    {"title": "ECB President Speaks", "country": "EUR",
     "date": "2026-09-23T09:00:00-04:00", "impact": "High"},
    {"title": "Bank Holiday", "country": "USD", "date": "not-a-date", "impact": "High"},
]
EVENT = datetime(2026, 9, 23, 12, 30, tzinfo=timezone.utc)


class FakeResp:
    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError(f"{self.status}")

    def json(self):
        return self.payload


def make_engine(tmp_path, now, payload=SAMPLE, status=200, **overrides):
    cfg = NewsConfig(cache_file=str(tmp_path / "cal.json"), **overrides)
    calls = []

    def http_get(url, timeout, headers):
        calls.append(url)
        return FakeResp(payload, status)

    clock = {"now": now}
    eng = NewsFilterEngine(cfg, http_get=http_get, clock=lambda: clock["now"])
    return eng, calls, clock


def test_parse_keeps_only_high_usd():
    events = parse_calendar(SAMPLE, NewsConfig())
    assert [e.title for e in events] == ["CPI m/m"]
    assert events[0].time == EVENT


@pytest.mark.parametrize("offset_min,blackout", [
    (-31, False), (-29, True), (0, True), (14, True), (16, False)])
def test_blackout_window(tmp_path, offset_min, blackout):
    eng, _, _ = make_engine(tmp_path, EVENT + timedelta(minutes=offset_min))
    eng.refresh(force=True)
    assert eng.state().blackout is blackout


def test_imminent_list_for_pre_news_management(tmp_path):
    eng, _, _ = make_engine(tmp_path, EVENT - timedelta(minutes=10))
    eng.refresh(force=True)
    assert [e.title for e in eng.state().imminent] == ["CPI m/m"]


def test_fail_closed_without_calendar(tmp_path):
    eng, _, _ = make_engine(tmp_path, EVENT, status=503)
    eng.refresh(force=True)
    state = eng.state()
    assert state.blackout and "fail-closed" in state.reason


def test_fail_open_option(tmp_path):
    eng, _, _ = make_engine(tmp_path, EVENT, status=503, fail_closed=False)
    eng.refresh(force=True)
    assert not eng.state().blackout


def test_cache_survives_restart_and_rate_limit(tmp_path):
    now = EVENT - timedelta(hours=3)
    eng, calls, clock = make_engine(tmp_path, now)
    eng.refresh()
    eng.refresh()  # within refresh interval: no second download
    assert len(calls) == 1

    eng2, calls2, _ = make_engine(tmp_path, now + timedelta(minutes=5), status=503)
    eng2.refresh()  # fresh cache on disk counts as recent -> no download
    assert calls2 == []
    assert not eng2.state().blackout
    assert eng2.state().next_event.title == "CPI m/m"
