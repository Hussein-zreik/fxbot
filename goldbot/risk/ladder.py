"""Drawdown ladder from the equity high-water mark (CLAUDE.md section 6).

  L1 (5 %)   risk x 0.5; clears again when the drawdown recovers
  L2 (7.5 %) stop entries; LATCHED until a human reviews and calls reset()
  L3 (10 %)  kill + flatten; LATCHED until a human resets it

reset() needs a written note, appends it to CHANGELOG.md, rebases the high-water
mark to current equity (otherwise the ladder would re-trigger immediately) and
caps risk at 0.25 % until a human changes it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .changelog import append_entry
from .rules import Limits
from .store import RiskStore

EPS = 1e-9
RESUME_CAP_PCT = 0.25


@dataclass(frozen=True)
class LadderStatus:
    level: int
    dd_pct: float
    hwm: float
    changed: bool


class Ladder:
    def __init__(self, limits: Limits, store: RiskStore, changelog: str = "CHANGELOG.md"):
        self.limits, self.store, self.changelog = limits, store, changelog

    def _dynamic_level(self, dd_pct: float) -> int:
        steps = self.limits.drawdown_ladder_from_hwm
        for level, step in ((3, steps.L3), (2, steps.L2), (1, steps.L1)):
            if dd_pct + EPS >= step.dd_pct:
                return level
        return 0

    def update(self, equity: float, now: datetime) -> LadderStatus:
        hwm = max(float(self.store.get("hwm", equity)), equity)
        self.store.set("hwm", hwm)
        self.store.set("last_equity", equity)
        dd = (hwm - equity) / hwm * 100 if hwm > 0 else 0.0
        dynamic = self._dynamic_level(dd)
        latched = int(self.store.get("ladder_latched", 0))
        if dynamic >= 2 and dynamic > latched:
            latched = dynamic
            self.store.set("ladder_latched", latched)
        level = max(latched, dynamic)
        changed = level != int(self.store.get("ladder_level", 0))
        self.store.set("ladder_level", level)
        return LadderStatus(level, dd, hwm, changed)

    def risk_cap_pct(self) -> Optional[float]:
        return self.store.get("risk_cap_pct")

    def reset(self, note: str, author: str, now: datetime) -> None:
        if not note or not note.strip():
            raise ValueError("a signed review note is required to reset the ladder")
        old_hwm = float(self.store.get("hwm", 0.0))
        equity = float(self.store.get("last_equity", old_hwm))
        level = int(self.store.get("ladder_level", 0))
        self.store.set("ladder_latched", 0)
        self.store.set("ladder_level", 0)
        self.store.set("hwm", equity)
        self.store.set("risk_cap_pct", RESUME_CAP_PCT)
        append_entry(self.changelog, "LADDER RESET", author, note.strip(),
                     f"Cleared level {level}; high-water mark rebased {old_hwm:.2f} -> "
                     f"{equity:.2f}; risk capped at {RESUME_CAP_PCT}% until changed by a human",
                     now)
