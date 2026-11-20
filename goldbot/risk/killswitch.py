"""Kill switch (CLAUDE.md section 6).

Triggers: the ``ops/KILL`` file, ladder L3, 3 consecutive order errors, more than
50 orders in 5 minutes, an equity drop above 3 % within 15 minutes, a
reconciliation mismatch lasting 5 minutes, equity data missing for 5 minutes with
positions open, and more than 3 unknown retcodes in 10 minutes.

A trip is persisted. Resetting needs a written note, the flag file removed, and
writes a signed entry to CHANGELOG.md.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Deque, Optional, Tuple

from .changelog import append_entry
from .retcodes import is_known
from .store import RiskStore

MAX_ORDER_ERRORS = 3
MAX_ORDERS = 50
ORDER_WINDOW = timedelta(minutes=5)
PNL_WINDOW = timedelta(minutes=15)
PNL_DROP_PCT = 3.0
RECON_LIMIT = timedelta(minutes=5)
EQUITY_GAP_LIMIT = timedelta(minutes=5)
UNKNOWN_WINDOW = timedelta(minutes=10)
MAX_UNKNOWN = 3


class KillSwitch:
    def __init__(self, store: RiskStore, flag_path: str = "ops/KILL",
                 changelog: str = "CHANGELOG.md"):
        self.store, self.flag_path, self.changelog = store, Path(flag_path), changelog
        self._consecutive_errors = 0
        self._sent: Deque[datetime] = deque()
        self._unknown: Deque[datetime] = deque()
        self._equity: Deque[Tuple[datetime, float]] = deque()
        self._recon_since: Optional[datetime] = None
        self._equity_gap_since: Optional[datetime] = None
        self._reset_seen = self.store.get("kill_reset_count", 0)

    def _clear_counters(self) -> None:
        self._consecutive_errors = 0
        self._sent.clear()
        self._unknown.clear()
        self._equity.clear()
        self._recon_since = self._equity_gap_since = None

    # ------------------------------------------------------------- inputs
    def record_order_sent(self, now: datetime) -> None:
        self._sent.append(now)

    def record_order_result(self, now: datetime, ok: bool, retcode: int) -> None:
        self._consecutive_errors = 0 if ok else self._consecutive_errors + 1
        if not is_known(retcode):
            self._unknown.append(now)

    def record_equity(self, now: datetime, equity: float) -> None:
        self._equity_gap_since = None
        self._equity.append((now, equity))
        while self._equity and now - self._equity[0][0] > PNL_WINDOW:
            self._equity.popleft()

    def record_equity_unavailable(self, now: datetime) -> None:
        if self._equity_gap_since is None:
            self._equity_gap_since = now

    def record_reconciliation(self, now: datetime, mismatch: bool) -> None:
        if not mismatch:
            self._recon_since = None
        elif self._recon_since is None:
            self._recon_since = now

    # ------------------------------------------------------------- evaluation
    def check(self, now: datetime, ladder_level: int, has_positions: bool) -> Optional[str]:
        """Return the reason to kill, or None."""
        resets = self.store.get("kill_reset_count", 0)
        if resets != self._reset_seen:           # a human reset it from another process
            self._reset_seen = resets
            self._clear_counters()
        if self.flag_path.exists():
            return "KILL_FLAG"
        if ladder_level >= 3:
            return "LADDER_L3"
        if self._consecutive_errors >= MAX_ORDER_ERRORS:
            return "ORDER_ERRORS"
        while self._sent and now - self._sent[0] > ORDER_WINDOW:
            self._sent.popleft()
        if len(self._sent) > MAX_ORDERS:
            return "ORDER_RATE"
        if self._equity:
            peak = max(e for _, e in self._equity)
            latest = self._equity[-1][1]
            if peak > 0 and (peak - latest) / peak * 100 > PNL_DROP_PCT:
                return "PNL_DROP"
        if self._recon_since and now - self._recon_since >= RECON_LIMIT:
            return "RECONCILIATION"
        if has_positions and self._equity_gap_since and \
                now - self._equity_gap_since > EQUITY_GAP_LIMIT:
            return "EQUITY_UNAVAILABLE"
        while self._unknown and now - self._unknown[0] > UNKNOWN_WINDOW:
            self._unknown.popleft()
        if len(self._unknown) > MAX_UNKNOWN:
            return "UNKNOWN_RETCODES"
        return None

    # ------------------------------------------------------------- state
    def trip(self, reason: str, now: datetime) -> None:
        if not self.is_killed():
            self.store.add_halt("KILL", reason, now)

    def is_killed(self) -> bool:
        return bool(self.store.active_halts("KILL"))

    def reasons(self) -> list:
        return [h["reason"] for h in self.store.active_halts("KILL")]

    def reset(self, note: str, author: str, now: datetime) -> None:
        if not note or not note.strip():
            raise ValueError("a signed review note is required to reset the kill switch")
        if self.flag_path.exists():
            raise RuntimeError(f"remove the flag file {self.flag_path} first")
        why = ", ".join(self.reasons()) or "n/a"
        self.store.clear_halts("KILL", note.strip(), now)
        self._clear_counters()
        self._reset_seen = self.store.get("kill_reset_count", 0) + 1
        self.store.set("kill_reset_count", self._reset_seen)
        append_entry(self.changelog, "KILL RESET", author, note.strip(),
                     f"Trading may resume after kill ({why}); positions were flattened by "
                     "the kill action", now)
