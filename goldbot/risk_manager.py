"""Position sizing, SL/TP placement and the hard daily equity guardrail.

Lot size
    lots = (equity * risk_per_trade) / loss_per_lot

``loss_per_lot`` is the account-currency loss of 1.0 lot if the stop is hit.
It comes from the broker's own ``order_calc_profit`` (handles account currency
and contract size). The fallback is the textbook formula:

    loss_per_lot = (SL distance / tick_size) * tick_value

which equals "SL points x tick value" when tick_size == point.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Tuple

from .config import RiskConfig
from .mt5_connector import BUY, AccountSnapshot, SymbolSpec, Tick
from .state import StateStore

log = logging.getLogger(__name__)


@dataclass
class TradePlan:
    side: str
    volume: float
    entry: float
    sl: float
    tp: float
    sl_distance: float
    loss_per_lot: float
    risk_amount: float


@dataclass
class GuardrailStatus:
    halted: bool
    daily_loss_pct: float
    day_start_balance: float
    reason: str = ""
    just_triggered: bool = False


def round_volume_down(volume: float, spec: SymbolSpec) -> float:
    """Round down to the broker's lot step (never round risk UP)."""
    steps = math.floor(volume / spec.volume_step + 1e-9)
    decimals = max(0, -int(math.floor(math.log10(spec.volume_step))))
    return round(steps * spec.volume_step, decimals)


def round_price(price: float, spec: SymbolSpec) -> float:
    return round(round(price / spec.tick_size) * spec.tick_size, spec.digits)


class RiskManager:
    def __init__(self, cfg: RiskConfig, connector, state: StateStore,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.cfg = cfg
        self.connector = connector
        self.state = state
        self._clock = clock

    # ----------------------------------------------------------------- #
    # Daily guardrail
    # ----------------------------------------------------------------- #
    def update_daily(self, acc: AccountSnapshot) -> GuardrailStatus:
        now = self._clock()
        today = now.date().isoformat()

        if self.state.get("day") != today:
            self.state.data["day"] = today
            self.state.data["day_start_balance"] = acc.balance
            log.info("New UTC trading day %s - starting balance %.2f", today, acc.balance)

        halted_until = self.state.get("halted_until")
        if halted_until and now >= datetime.fromisoformat(halted_until):
            log.info("Daily halt expired - trading re-enabled")
            self.state.data["halted_until"] = None
            halted_until = None
        self.state.save()

        start = float(self.state.get("day_start_balance") or acc.balance)
        loss_pct = (start - acc.equity) / start if start > 0 else 0.0
        status = GuardrailStatus(halted=bool(halted_until), daily_loss_pct=loss_pct,
                                 day_start_balance=start)
        if halted_until:
            status.reason = f"daily loss limit hit - halted until {halted_until}"
            return status

        if loss_pct >= self.cfg.max_daily_loss:
            resume = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                                       microsecond=0)
            self.state.set("halted_until", resume.isoformat())
            status.halted = True
            status.just_triggered = True
            status.reason = (f"daily loss {loss_pct:.2%} >= {self.cfg.max_daily_loss:.2%}"
                             f" - halted until {resume.isoformat()}")
            log.critical("EQUITY GUARDRAIL: %s", status.reason)
        return status

    # ----------------------------------------------------------------- #
    # Trade planning
    # ----------------------------------------------------------------- #
    def plan_trade(self, side: str, tick: Tick, atr_value: float,
                   acc: AccountSnapshot, spec: SymbolSpec
                   ) -> Tuple[Optional[TradePlan], str]:
        if not atr_value or atr_value <= 0 or math.isnan(atr_value):
            return None, "invalid ATR"

        spread_points = (tick.ask - tick.bid) / spec.point
        if spread_points > self.cfg.max_spread_points:
            return None, f"spread {spread_points:.0f} pts > max {self.cfg.max_spread_points}"

        entry = tick.ask if side == BUY else tick.bid
        sl_dist = self.cfg.sl_atr_mult * atr_value
        tp_dist = self.cfg.tp_atr_mult * atr_value
        min_dist = (spec.stops_level + spread_points) * spec.point
        if sl_dist <= min_dist:
            return None, f"SL distance {sl_dist:.2f} inside broker stop level"

        sign = 1 if side == BUY else -1
        sl = round_price(entry - sign * sl_dist, spec)
        tp = round_price(entry + sign * tp_dist, spec)
        sl_dist = abs(entry - sl)

        loss_per_lot = self.connector.calc_loss_per_lot(spec.name, side, entry, sl)
        if not loss_per_lot:
            loss_per_lot = (sl_dist / spec.tick_size) * spec.tick_value
        if loss_per_lot <= 0:
            return None, "could not compute loss per lot"

        risk_amount = acc.equity * self.cfg.risk_per_trade
        volume = min(risk_amount / loss_per_lot, spec.volume_max, self.cfg.max_lot)
        volume = round_volume_down(volume, spec)

        margin = self.connector.calc_margin(spec.name, side, volume, entry)
        budget = acc.margin_free * self.cfg.margin_usage_limit
        if margin and margin > budget and volume > 0:
            volume = round_volume_down(volume * budget / margin, spec)

        if volume < spec.volume_min:
            return None, (f"size {volume} below broker minimum {spec.volume_min} "
                          f"(1 min lot would risk {spec.volume_min * loss_per_lot:.2f} "
                          f"> {risk_amount:.2f})")

        plan = TradePlan(side=side, volume=volume, entry=entry, sl=sl, tp=tp,
                         sl_distance=sl_dist, loss_per_lot=loss_per_lot,
                         risk_amount=volume * loss_per_lot)
        return plan, "ok"
