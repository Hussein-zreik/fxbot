"""Time conversions (CLAUDE.md section 2: UTC internally, convert only here).

Internal timestamps are timezone-aware UTC. Broker *server* time is a fixed
offset that the connector detects; Asia/Beirut follows Lebanon's own DST
rules (they differ from the US/EU dates), so it uses the tz database.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

BEIRUT = ZoneInfo("Asia/Beirut")
_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_HHMM = re.compile(r"^(\d{1,2}):(\d{2})$")


def server_time(utc: datetime, offset_s: int) -> datetime:
    """Broker wall-clock time as a NAIVE datetime (it is not UTC and not a tz)."""
    if utc.tzinfo is None:
        raise ValueError("server_time needs a timezone-aware UTC datetime")
    return (utc.astimezone(timezone.utc) + timedelta(seconds=offset_s)).replace(tzinfo=None)


def beirut_time(utc: datetime) -> datetime:
    if utc.tzinfo is None:
        raise ValueError("beirut_time needs a timezone-aware UTC datetime")
    return utc.astimezone(BEIRUT)


def _minutes(hhmm: str) -> int:
    match = _HHMM.match(hhmm.strip())
    if not match:
        raise ValueError(f"bad time '{hhmm}' (expected HH:MM)")
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"bad time '{hhmm}'")
    return hour * 60 + minute


def parse_window(spec: str) -> Tuple[int, int, bool]:
    """Parse "HH:MM-HH:MM" (daily) or "Fri 20:00-Sun 23:59" (weekly).

    Returns (start_minute, end_minute, weekly). Minutes are minute-of-day for
    daily windows and minute-of-week (Monday 00:00 = 0) for weekly ones.
    """
    if "-" not in spec:
        raise ValueError(f"bad window '{spec}'")
    left, right = (part.strip() for part in spec.split("-", 1))
    weekly = " " in left
    if not weekly:
        if " " in right:
            raise ValueError(f"bad window '{spec}'")
        return _minutes(left), _minutes(right), False
    try:
        (d1, t1), (d2, t2) = left.split(None, 1), right.split(None, 1)
        day1, day2 = _DAYS.index(d1[:3].lower()), _DAYS.index(d2[:3].lower())
    except (ValueError, IndexError):
        raise ValueError(f"bad window '{spec}'") from None
    return day1 * 1440 + _minutes(t1), day2 * 1440 + _minutes(t2), True


def in_any_window(local: datetime, specs: List[str]) -> Optional[str]:
    """Return the first window containing ``local`` (naive or aware wall time)."""
    minute_of_day = local.hour * 60 + local.minute
    minute_of_week = local.weekday() * 1440 + minute_of_day
    for spec in specs:
        start, end, weekly = parse_window(spec)
        now = minute_of_week if weekly else minute_of_day
        inside = start <= now <= end if start <= end else (now >= start or now <= end)
        if inside:
            return spec
    return None
