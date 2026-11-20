from datetime import datetime, timezone

import pytest

from goldbot.clock import (beirut_time, in_any_window, parse_window, server_time)

UTC = timezone.utc


def test_server_time_applies_the_broker_offset():
    assert server_time(datetime(2026, 9, 23, 22, 0, tzinfo=UTC), 3 * 3600) == \
        datetime(2026, 9, 24, 1, 0)
    with pytest.raises(ValueError):
        server_time(datetime(2026, 9, 23, 22, 0), 0)          # naive input rejected


def test_beirut_follows_lebanon_dst_not_a_fixed_offset():
    winter = beirut_time(datetime(2026, 1, 15, 12, 0, tzinfo=UTC))
    summer = beirut_time(datetime(2026, 7, 15, 12, 0, tzinfo=UTC))
    assert winter.utcoffset().total_seconds() == 2 * 3600
    assert summer.utcoffset().total_seconds() == 3 * 3600


def test_daily_window_wrapping_midnight():
    windows = ["23:45-00:30"]
    for hhmm, inside in [("23:44", False), ("23:45", True), ("00:00", True),
                         ("00:30", True), ("00:31", False), ("12:00", False)]:
        h, m = map(int, hhmm.split(":"))
        assert bool(in_any_window(datetime(2026, 9, 23, h, m), windows)) is inside, hhmm


def test_weekly_window_friday_evening_to_sunday_night():
    windows = ["Fri 20:00-Sun 23:59"]
    # 2026-09-25 is a Friday
    cases = [(datetime(2026, 9, 25, 19, 59), False), (datetime(2026, 9, 25, 20, 0), True),
             (datetime(2026, 9, 26, 3, 0), True), (datetime(2026, 9, 27, 23, 59), True),
             (datetime(2026, 9, 28, 0, 0), False), (datetime(2026, 9, 24, 21, 0), False)]
    for dt, inside in cases:
        assert bool(in_any_window(dt, windows)) is inside, dt


def test_returns_the_matching_window_text():
    assert in_any_window(datetime(2026, 9, 23, 15, 30), ["20:45-22:00", "15:15-16:45"]) \
        == "15:15-16:45"
    assert in_any_window(datetime(2026, 9, 23, 10, 0), []) is None


@pytest.mark.parametrize("bad", ["", "25:00-26:00", "12:00", "Xyz 10:00-11:00", "10:00-11:99"])
def test_bad_windows_raise(bad):
    with pytest.raises(ValueError):
        parse_window(bad)
