"""Daily and weekly equity baselines (reset at 00:00 BROKER SERVER time).

Persisted, so a restart in the middle of the day cannot hand the bot a fresh
loss allowance. The week starts on Monday (ISO week).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..clock import server_time
from .store import RiskStore


@dataclass(frozen=True)
class Periods:
    day_key: str
    week_key: str
    day_start_equity: float
    week_start_equity: float


class PeriodTracker:
    def __init__(self, store: RiskStore):
        self.store = store

    def update(self, equity: float, now_utc: datetime, offset_s: int) -> Periods:
        local = server_time(now_utc, offset_s)
        day_key = local.date().isoformat()
        iso = local.isocalendar()
        week_key = f"{iso[0]}-W{iso[1]:02d}"
        if self.store.get("day_key") != day_key:
            self.store.set("day_key", day_key)
            self.store.set("day_start_equity", equity)
        if self.store.get("week_key") != week_key:
            self.store.set("week_key", week_key)
            self.store.set("week_start_equity", equity)
        return Periods(day_key, week_key, float(self.store.get("day_start_equity", equity)),
                       float(self.store.get("week_start_equity", equity)))
