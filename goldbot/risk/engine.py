"""RiskEngine.approve(): the single gate every order must pass (prime directive 2).

Checks run in the order of CLAUDE.md section 6; the first failure rejects the
order with a reason code. The engine also computes the size (through
``sizing.size_position``, the only place lots are computed), so a strategy never
sizes its own orders.

Check 12 (portfolio volatility / ES / gap scenarios) is Phase 2 and not applied
to a single sleeve.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional

from ..clock import beirut_time, in_any_window, server_time
from .rules import Limits
from .sizing import Sized, effective_leverage, notional, size_position, risk_distance
from .store import RiskStore
from .types import OrderIntent, PortfolioState, RiskDecision

log = logging.getLogger(__name__)

EPS = 1e-9
MAX_CLOCK_DRIFT_MS = 250.0
FAT_FINGER_MIN_ORDERS = 5


@dataclass
class MonitorStatus:
    """Continuous (monitor-loop) view: why new entries are currently blocked."""
    entries_blocked: bool
    reasons: List[str] = field(default_factory=list)
    daily_loss_pct: float = 0.0
    weekly_loss_pct: float = 0.0


def _loss_pct(start: float, now: float) -> float:
    return (start - now) / start * 100 if start > 0 else 0.0


class RiskEngine:
    def __init__(self, limits: Limits, store: Optional[RiskStore] = None):
        self.limits, self.store = limits, store

    # ------------------------------------------------------------------ continuous
    def monitor(self, *, equity: float, day_start_equity: float, week_start_equity: float,
                losing_trades_today: int, consecutive_losses: int, day_stopped: bool,
                ladder_level: int, kill_flag: bool = False, halted: bool = False
                ) -> MonitorStatus:
        """Why new entries are blocked right now (monitor loop; no intent needed)."""
        lim = self.limits
        daily = _loss_pct(day_start_equity, equity)
        weekly = _loss_pct(week_start_equity, equity)
        reasons = []
        if kill_flag:
            reasons.append("KILL_FLAG")
        if halted:
            reasons.append("HALTED")
        if daily + EPS >= lim.daily_loss_limit_pct:
            reasons.append("DAILY_LOSS")
        if weekly + EPS >= lim.weekly_loss_limit_pct:
            reasons.append("WEEKLY_LOSS")
        if losing_trades_today >= lim.daily_max_losing_trades:
            reasons.append("DAILY_LOSING_TRADES")
        if day_stopped or consecutive_losses >= lim.consecutive_losses_stop_day:
            reasons.append("STREAK_STOP")
        if ladder_level >= 3:
            reasons.append("LADDER_L3")
        elif ladder_level == 2:
            reasons.append("LADDER_L2")
        return MonitorStatus(bool(reasons), reasons, daily, weekly)

    # ------------------------------------------------------------------ pre-trade
    def approve(self, intent: OrderIntent, st: PortfolioState) -> RiskDecision:
        decision = self._approve(intent, st)
        if self.store is not None:
            try:
                self.store.record_decision(st.now_utc, intent.intent_id, intent.strategy,
                                           intent.symbol, intent.side, decision.approved,
                                           decision.reason_code, decision.detail,
                                           decision.adjusted_size)
            except Exception:  # noqa: BLE001 - logging must never block or break a decision
                log.exception("Could not record the risk decision")
        return decision

    def _approve(self, intent: OrderIntent, st: PortfolioState) -> RiskDecision:
        lim, passed = self.limits, []

        def reject(code: str, detail: str = "") -> RiskDecision:
            return RiskDecision(False, code, detail, checks=tuple(passed))

        sym = st.symbol

        # 1. kill flag, halts, connection, clock
        if st.kill_flag:
            return reject("KILL_FLAG", "the ops/KILL flag is set")
        if st.halted:
            return reject("HALTED", st.halt_reason or "trading is halted")
        if not st.broker_connected:
            return reject("BROKER_DISCONNECTED", "no broker connection")
        if st.clock_drift_ms is None or abs(st.clock_drift_ms) >= MAX_CLOCK_DRIFT_MS:
            return reject("CLOCK_DRIFT", f"clock drift {st.clock_drift_ms} ms "
                                         f"(limit {MAX_CLOCK_DRIFT_MS:.0f})")
        if intent.symbol != sym.canonical:
            return reject("SYMBOL_MISMATCH", f"{intent.symbol} vs {sym.canonical}")
        passed.append("safety_state")

        # 2. mode and account (only when orders are really sent)
        if st.orders_enabled:
            if st.mode not in ("paper", "live"):
                return reject("MODE_INVALID", f"mode {st.mode!r}")
            if st.expected_login is not None and st.account_login != st.expected_login:
                return reject("ACCOUNT_MISMATCH",
                              f"logged in as {st.account_login}, config expects "
                              f"{st.expected_login}")
            if st.mode == "paper" and not st.account_is_demo:
                return reject("MODE_MISMATCH", "paper mode on a REAL account")
            if st.mode == "live":
                if st.account_is_demo:
                    return reject("MODE_MISMATCH", "live mode on a demo account")
                if not st.live_gate_ok:
                    return reject("LIVE_GATE", "the four-part live gate is not satisfied")
                if lim.stage == "demo":
                    return reject("STAGE_MISMATCH", "real account but stage is still 'demo'")
        passed.append("mode_account")

        # 3. news, no-entry windows, stale calendar
        if st.news_blackout:
            return reject("NEWS_BLACKOUT", st.news_reason or "news blackout")
        if st.server_utc_offset_s is None:
            return reject("SERVER_TIME_UNKNOWN", "broker server time offset not known")
        local = server_time(st.now_utc, st.server_utc_offset_s)
        window = in_any_window(local, lim.filters.no_entry_windows_server)
        if window:
            return reject("NO_ENTRY_WINDOW", f"server time {local:%a %H:%M} is inside {window}")
        if not st.calendar_fresh:
            stale = in_any_window(beirut_time(st.now_utc),
                                  lim.filters.calendar_fail_closed_windows_beirut)
            if stale:
                return reject("CALENDAR_STALE_WINDOW",
                              f"calendar unavailable and Beirut time is inside {stale}")
        passed.append("time_filters")

        # 4. spread
        cap = lim.filters.spread_max_points.get(intent.symbol)
        if cap is None:
            return reject("SPREAD_LIMIT_UNKNOWN", f"no spread cap configured for {intent.symbol}")
        if st.spread_hour_median_points is None:
            return reject("SPREAD_BASELINE_MISSING", "no hour-of-day median spread available")
        median_cap = lim.filters.spread_max_mult_of_hour_median * st.spread_hour_median_points
        if st.spread_points > median_cap + EPS:
            return reject("SPREAD_MEDIAN", f"spread {st.spread_points:.0f} pts > "
                                           f"{median_cap:.0f} (x median)")
        if st.spread_points > cap + EPS:
            return reject("SPREAD_MAX", f"spread {st.spread_points:.0f} pts > {cap:.0f}")
        passed.append("spread")

        # 5. stop-loss placement
        buy = intent.side == "BUY"
        closing_price = intent.bid if buy else intent.ask
        distance = abs(closing_price - intent.sl)
        if (buy and intent.sl >= closing_price) or (not buy and intent.sl <= closing_price):
            return reject("SL_SIDE", "stop-loss is on the wrong side of the market")
        if distance + EPS < sym.stops_level * sym.point:
            return reject("STOPS_LEVEL", f"stop {distance:.2f} closer than the broker minimum "
                                         f"{sym.stops_level * sym.point:.2f}")
        if distance + EPS < sym.freeze_level * sym.point:
            return reject("FREEZE_LEVEL", f"stop {distance:.2f} inside the freeze level "
                                          f"{sym.freeze_level * sym.point:.2f}")
        passed.append("stop_placement")

        # 6. reward : risk
        if intent.tp is None:
            return reject("RR_NO_TARGET", "no take-profit, cannot compute reward:risk")
        risk_dist = float(risk_distance(intent.side, intent.bid, intent.ask, intent.sl))
        rr = abs(intent.tp - intent.entry) / risk_dist
        if rr + EPS < lim.filters.min_reward_risk:
            return reject("RR_TOO_LOW", f"reward:risk {rr:.2f} < {lim.filters.min_reward_risk}")
        passed.append("reward_risk")

        # 7. per-trade risk -> size
        allowed = lim.per_trade_cap()
        if st.risk_cap_pct is not None:
            allowed = min(allowed, st.risk_cap_pct)
        halved = st.ladder_level >= 1 or st.risk_halved_by_streak
        if halved:
            allowed *= 0.5
        risk_pct = allowed
        if intent.requested_risk_pct is not None:
            if intent.requested_risk_pct > allowed + EPS:
                return reject("RISK_OVER_STAGE", f"requested {intent.requested_risk_pct}% > "
                                                 f"allowed {allowed}%")
            risk_pct = intent.requested_risk_pct
        risk_budget = st.equity * risk_pct / 100
        sized = size_position(risk_usd=risk_budget, entry=intent.entry, sl=intent.sl,
                              tick_size=sym.tick_size, tick_value=sym.tick_value,
                              volume_min=sym.volume_min, volume_step=sym.volume_step,
                              volume_max=sym.volume_max)
        if not isinstance(sized, Sized):
            return reject("SIZE_SKIP", sized.reason)
        lots = float(sized.lots)
        risk_usd = float(sized.risk_usd)
        passed.append("per_trade_risk")

        # 8. open risk, positions, clusters, trade count
        if any(p.risk_usd is None for p in st.open_positions):
            return reject("UNPROTECTED_POSITION", "an open position has no known stop risk")
        if len(st.open_positions) >= lim.max_open_positions:
            return reject("MAX_POSITIONS", f"{len(st.open_positions)} open "
                                           f"(max {lim.max_open_positions})")
        if any(p.symbol == intent.symbol for p in st.open_positions):
            return reject("MAX_POSITIONS", f"already holding {intent.symbol} (max 1 per symbol)")
        cluster = lim.cluster_of(intent.symbol)
        if any(lim.cluster_of(p.symbol) == cluster for p in st.open_positions):
            return reject("CLUSTER_SLOT", f"a correlated position ({cluster}) already uses "
                                          "this risk slot")
        open_risk = sum(p.risk_usd for p in st.open_positions) + risk_usd
        if open_risk > st.equity * lim.max_open_risk_pct / 100 + EPS:
            return reject("OPEN_RISK", f"open risk ${open_risk:.2f} > "
                                       f"{lim.max_open_risk_pct}% of equity")
        if st.trades_today >= lim.max_trades_per_day:
            return reject("MAX_TRADES_DAY", f"{st.trades_today} trades today "
                                            f"(max {lim.max_trades_per_day})")
        passed.append("exposure")

        # 9. leverage and margin
        new_notional = notional(sized.lots, sym.contract_size, intent.entry)
        total = Decimal(str(sum(p.notional for p in st.open_positions))) + new_notional
        leverage = effective_leverage(total, st.equity)
        if leverage > Decimal(str(lim.max_effective_leverage)):
            return reject("LEVERAGE", f"effective leverage {float(leverage):.2f}x > "
                                      f"{lim.max_effective_leverage}x")
        if st.margin_per_lot is None:
            return reject("MARGIN_UNKNOWN", "broker margin for this order is unknown")
        margin_pct = (st.margin_used + lots * st.margin_per_lot) / st.equity * 100
        if margin_pct > lim.max_margin_usage_pct + EPS:
            return reject("MARGIN", f"margin after trade {margin_pct:.1f}% > "
                                    f"{lim.max_margin_usage_pct}%")
        passed.append("leverage_margin")

        # 10. loss limits and ladder
        daily = _loss_pct(st.day_start_equity, st.equity)
        if daily + EPS >= lim.daily_loss_limit_pct:
            return reject("DAILY_LOSS", f"day P/L -{daily:.2f}% "
                                        f"(limit {lim.daily_loss_limit_pct}%)")
        if st.losing_trades_today >= lim.daily_max_losing_trades:
            return reject("DAILY_LOSING_TRADES", f"{st.losing_trades_today} losing trades today")
        weekly = _loss_pct(st.week_start_equity, st.equity)
        if weekly + EPS >= lim.weekly_loss_limit_pct:
            return reject("WEEKLY_LOSS", f"week P/L -{weekly:.2f}% "
                                         f"(limit {lim.weekly_loss_limit_pct}%)")
        if st.day_stopped or st.consecutive_losses >= lim.consecutive_losses_stop_day:
            return reject("STREAK_STOP", f"{st.consecutive_losses} consecutive losses: no more "
                                         "entries today")
        if st.ladder_level >= 3:
            return reject("LADDER_L3", "drawdown ladder L3: kill, manual reset required")
        if st.ladder_level == 2:
            return reject("LADDER_L2", "drawdown ladder L2: entries stopped pending review")
        passed.append("loss_limits")

        # 11. price sanity vs an independent feed, fat-finger size
        if st.independent_mid is None:
            return reject("NO_INDEPENDENT_FEED",
                          "no independent price feed to sanity-check against")
        if st.atr_1m is None or st.atr_1m <= 0:
            return reject("PRICE_SANITY", "1-minute ATR unavailable")
        band = lim.filters.price_sanity_atr1m_mult * st.atr_1m
        if abs(intent.entry - st.independent_mid) > band + EPS:
            gap = abs(intent.entry - st.independent_mid)
            return reject("PRICE_SANITY", f"price {intent.entry} is {gap:.2f} from the "
                                          f"independent mid (limit {band:.2f})")
        if len(st.recent_order_sizes) >= FAT_FINGER_MIN_ORDERS:
            median = statistics.median(st.recent_order_sizes)
            if lots > lim.filters.fat_finger_size_mult * median + EPS:
                return reject("FAT_FINGER", f"size {lots} > {lim.filters.fat_finger_size_mult}x "
                                            f"the recent median {median}")
        passed.append("price_sanity")

        return RiskDecision(True, "OK", f"risk ${risk_usd:.2f} ({risk_pct}%), RR {rr:.2f}",
                            adjusted_size=lots, risk_usd=risk_usd, risk_pct=risk_pct,
                            halved=halved, checks=tuple(passed))
