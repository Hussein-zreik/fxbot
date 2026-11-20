"""Plain data types shared by the risk engine, strategies and the orchestrator."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple


class InvalidIntent(ValueError):
    """An order intent that must never exist (e.g. no stop-loss)."""


@dataclass(frozen=True)
class SymbolInfo:
    """Contract data, refreshed from the broker every session (never hardcoded)."""
    name: str                   # broker's name, e.g. XAUUSD.a
    canonical: str              # rulebook name, e.g. XAUUSD
    point: float
    digits: int
    tick_size: float
    tick_value: float           # account currency, per lot, per tick_size
    contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level: int = 0        # points
    freeze_level: int = 0       # points


@dataclass(frozen=True)
class OrderIntent:
    """What a strategy wants. A stop-loss is mandatory and checked at construction."""
    intent_id: str
    strategy: str
    symbol: str                 # canonical
    side: str                   # BUY | SELL
    entry: float                # ask for BUY, bid for SELL
    sl: float
    tp: Optional[float]
    bid: float
    ask: float
    requested_risk_pct: Optional[float] = None

    def __post_init__(self):
        def bad(msg: str):
            raise InvalidIntent(msg)

        if self.side not in ("BUY", "SELL"):
            bad(f"side must be BUY or SELL, got {self.side!r}")
        for name in ("entry", "sl", "bid", "ask"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool) \
                    or not math.isfinite(value) or value <= 0:
                bad(f"{name} must be a positive number (a stop-loss is mandatory), got {value!r}")
        buy = self.side == "BUY"
        # The spread must be inside the risk distance (sizing contract): longs enter at
        # the ask, shorts at the bid.
        if abs(self.entry - (self.ask if buy else self.bid)) > 1e-9:
            bad("entry must be the ask for BUY / the bid for SELL")
        if self.bid > self.ask:
            bad("bid is above ask")
        if (buy and self.sl >= self.entry) or (not buy and self.sl <= self.entry):
            bad("stop-loss is on the wrong side of the entry")
        if self.tp is not None:
            if not math.isfinite(self.tp) or self.tp <= 0 \
                    or (buy and self.tp <= self.entry) or (not buy and self.tp >= self.entry):
                bad("take-profit is invalid or on the wrong side of the entry")


@dataclass(frozen=True)
class PositionRisk:
    ticket: int
    symbol: str                 # canonical
    risk_usd: Optional[float]   # initial stop risk; None = unknown (unprotected/orphan)
    notional: float


@dataclass
class PortfolioState:
    """Everything the engine needs, gathered by the caller. None means unknown."""
    now_utc: datetime
    server_utc_offset_s: Optional[int]
    equity: float
    day_start_equity: float
    week_start_equity: float
    open_positions: List[PositionRisk]
    trades_today: int
    losing_trades_today: int
    consecutive_losses: int
    risk_halved_by_streak: bool
    day_stopped: bool
    ladder_level: int                       # 0..3
    risk_cap_pct: Optional[float]           # post-L2 cap (0.25) until changed by a human
    broker_connected: bool
    clock_drift_ms: Optional[float]
    kill_flag: bool
    halted: bool
    halt_reason: str
    mode: str                               # paper | live
    orders_enabled: bool                    # False = dry-run (nothing is sent)
    account_login: int
    expected_login: Optional[int]
    account_is_demo: bool
    live_gate_ok: bool
    news_blackout: bool
    news_reason: str
    calendar_fresh: bool
    spread_points: float
    spread_hour_median_points: Optional[float]
    margin_used: float
    margin_per_lot: Optional[float]
    independent_mid: Optional[float]
    atr_1m: Optional[float]
    recent_order_sizes: List[float]
    symbol: SymbolInfo


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason_code: str
    detail: str = ""
    adjusted_size: Optional[float] = None
    risk_usd: float = 0.0
    risk_pct: float = 0.0
    halved: bool = False
    checks: Tuple[str, ...] = field(default_factory=tuple)
