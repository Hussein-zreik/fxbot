"""Trade counters and the losing-streak rules, persisted.

* ``max_trades_per_day`` counts trades OPENED on the server day.
* ``daily_max_losing_trades`` counts trades CLOSED at a loss on the server day.
* After ``halve_after`` consecutive losses risk is halved until +2R has been
  recovered (measured from the moment halving began).
* ``stop_after`` consecutive losses stop entries for the rest of that day.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .store import RiskStore

RECOVERY_R = 2.0


@dataclass(frozen=True)
class Streak:
    consecutive_losses: int
    halved: bool
    recovery_r: float

    @property
    def losses(self) -> int:
        return self.consecutive_losses


class TradeLedger:
    def __init__(self, store: RiskStore, halve_after: int, stop_after: int):
        self.store, self.halve_after, self.stop_after = store, halve_after, stop_after

    def record_open(self, day: str, ticket: int, risk_usd: float, lots: float) -> None:
        self.store.execute("INSERT OR IGNORE INTO trade_log(ticket, day_open, risk_usd, lots) "
                           "VALUES(?,?,?,?)", (ticket, day, risk_usd, lots))

    def record_close(self, day: str, ticket: int, profit: float) -> Optional[float]:
        """Book a closed trade; returns its R-multiple (None if unknown/already booked)."""
        rows = self.store.query("SELECT risk_usd, closed FROM trade_log WHERE ticket=?",
                                (ticket,))
        if not rows or rows[0]["closed"]:
            return None
        risk = rows[0]["risk_usd"] or 0.0
        r = profit / risk if risk > 0 else 0.0
        self.store.execute("UPDATE trade_log SET day_close=?, profit=?, r_multiple=?, closed=1 "
                           "WHERE ticket=?", (day, profit, r, ticket))
        self._update_streak(day, profit, r)
        return r

    def _update_streak(self, day: str, profit: float, r: float) -> None:
        s = self.streak()
        consecutive, halved, recovery = s.consecutive_losses, s.halved, s.recovery_r
        if halved:
            recovery += r
        if profit < 0:
            consecutive += 1
            if consecutive >= self.halve_after and not halved:
                halved, recovery = True, 0.0
            if consecutive >= self.stop_after:
                self.store.set("stopped_day", day)
        else:
            consecutive = 0
        if halved and recovery >= RECOVERY_R:
            halved, recovery, consecutive = False, 0.0, 0
        self.store.set("streak", {"consecutive_losses": consecutive, "halved": halved,
                                  "recovery_r": recovery})

    def streak(self) -> Streak:
        raw = self.store.get("streak", {})
        return Streak(int(raw.get("consecutive_losses", 0)), bool(raw.get("halved", False)),
                      float(raw.get("recovery_r", 0.0)))

    def day_stopped(self, day: str) -> bool:
        return self.store.get("stopped_day") == day

    def trades_today(self, day: str) -> int:
        return self.store.query("SELECT COUNT(*) AS n FROM trade_log WHERE day_open=?",
                                (day,))[0]["n"]

    def losing_today(self, day: str) -> int:
        return self.store.query("SELECT COUNT(*) AS n FROM trade_log WHERE closed=1 AND "
                                "day_close=? AND profit < 0", (day,))[0]["n"]

    def recent_sizes(self, n: int = 50) -> List[float]:
        rows = self.store.query("SELECT lots FROM trade_log ORDER BY rowid DESC LIMIT ?", (n,))
        return [r["lots"] for r in reversed(rows)]

    def risk_for(self, ticket: int) -> Optional[float]:
        rows = self.store.query("SELECT risk_usd FROM trade_log WHERE ticket=?", (ticket,))
        return rows[0]["risk_usd"] if rows else None
