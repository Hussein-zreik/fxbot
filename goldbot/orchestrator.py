"""BotOrchestrator: the main loop that assembles signals and drives MT5.

Every ``loop_interval_seconds`` (default 10 s) it:
  1. checks the MT5 connection (reconnects if needed),
  2. runs commands queued by the phone dashboard (pause / resume / close all),
  3. enforces the daily equity guardrail (flattens and halts on breach),
  4. protects open trades ahead of high-impact news and after AI-detected
     shock headlines,
  5. on each newly CLOSED execution bar, closes trades whose H1 trend has
     reversed, scores the six signal blocks and, if every gate passes,
     sizes and sends the order,
  6. publishes a status snapshot for the dashboard.

All MT5 calls happen on this loop's thread; the dashboard only reads the
published snapshot and queues commands, so the two never race.
"""

from __future__ import annotations

import csv
import logging
import math
import queue
import threading
import uuid
from collections import deque
from decimal import Decimal
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .config import AppConfig
from .correlation_engine import CorrelationEngine, CorrelationSnapshot
from .indicators import atr as atr_series
from .macro_engine import MacroDataEngine, YieldSnapshot
from .mt5_connector import BUY, PositionInfo
from .news_filter import NewsFilterEngine, NewsState
from .notifier import TelegramNotifier
from .performance import build_trades, compute_stats, json_safe, summary_text
from .risk.engine import MonitorStatus
from .risk.livegate import GateResult
from .risk.retcodes import Action, action_for
from .risk.sizing import actual_risk_usd, overshoot_ok
from .risk.suite import RiskSuite
from .risk.types import InvalidIntent, OrderIntent, PortfolioState, PositionRisk, SymbolInfo
from .risk_manager import RiskManager, round_volume_down
from .sentiment_engine import SentimentEngine, SentimentSnapshot
from .signal_model import Signal, build_signal
from .state import StateStore
from .technical_engine import TechnicalEngine, TechnicalSnapshot
from .trend_engine import TrendEngine, TrendSnapshot

log = logging.getLogger(__name__)

JOURNAL_FIELDS = [
    "logged_at", "bar_time", "total", "macro", "yield_pts", "dxy_pts",
    "silver_pts", "trend_pts", "news_pts", "vwap_pts", "signal", "blocked_by",
    "action", "yield", "yield_delta_bp", "yield_z1h", "yield_stale", "gs_corr",
    "gold_break", "silver_break", "usd_break", "usd_roc_pct", "divergence",
    "close", "vwap", "atr", "trend", "ai_sentiment", "ai_headlines", "news",
    "open_positions",
]

COMMANDS = ("pause", "resume", "close_all")
STRATEGY_ID = "gold_macro"
CANONICAL_GOLD = "XAUUSD"      # the rulebook's name for this sleeve's instrument
PENDING_CLOSE_GIVE_UP = timedelta(minutes=30)


def _parse_hhmm(value: str) -> dtime:
    hh, mm = value.split(":")
    return dtime(int(hh), int(mm))


def _fmt(x: float, spec: str = ".2f") -> str:
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else format(x, spec)


def _num(x: Any) -> Any:
    """JSON-safe number: NaN/inf become None."""
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    return x


class BotOrchestrator:
    def __init__(self, cfg: AppConfig, connector, macro: MacroDataEngine,
                 news: NewsFilterEngine, risk: RiskManager, state: StateStore,
                 suite: RiskSuite,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 sentiment: Optional[SentimentEngine] = None,
                 notifier: Optional[TelegramNotifier] = None,
                 mode: str = "paper",
                 live_gate: Optional[Callable[[int], GateResult]] = None,
                 independent_feed: Optional[Callable[[str], Optional[float]]] = None):
        self.cfg = cfg
        self.suite = suite
        self.mode = mode
        self._live_gate = live_gate
        self.live_gate_ok = False
        self.independent_feed = independent_feed
        self._periods = None
        self._ladder_level = 0
        self._blocked_reasons: set = set()
        self._pending_close: Dict[int, datetime] = {}
        self._spread_cache: Optional[tuple] = None
        self.connector = connector
        self.macro = macro
        self.news = news
        self.risk = risk
        self.state = state
        self.sentiment = sentiment
        self._clock = clock
        self._stop = threading.Event()

        self.gold: Optional[str] = None
        self.correlation: Optional[CorrelationEngine] = None
        self.technical: Optional[TechnicalEngine] = None
        self.trend: Optional[TrendEngine] = None
        self._last_bar: Optional[datetime] = None
        self._prev_tickets: Optional[set] = None
        self._market_closed_logged = False

        # Dashboard plumbing (thread-safe).
        self._commands: "queue.Queue[str]" = queue.Queue()
        self._status_lock = threading.Lock()
        self._status: Dict[str, Any] = {"state": "starting"}
        self._last_bar_report: Dict[str, Any] = {}
        self._events: deque = deque(maxlen=30)

        # Alerts and performance.
        self.notifier = notifier
        self._was_connected = True
        self._perf: Dict[str, Any] = {}
        self._perf_trades: list = []
        self._perf_at: Optional[datetime] = None
        self._currency = ""

    # ----------------------------------------------------------------- #
    # Lifecycle
    # ----------------------------------------------------------------- #
    def setup(self) -> None:
        if not self.connector.connect():
            raise RuntimeError("Could not connect to MetaTrader 5 - see log above")
        sym = self.cfg.symbols
        self.gold = self.connector.resolve_symbol(sym.gold)
        silver = self.connector.resolve_symbol(sym.silver)
        dxy = self.connector.resolve_symbol(sym.dxy) if sym.dxy else None
        eurusd = None if dxy else self.connector.resolve_symbol(sym.eurusd)
        if not self.gold or not silver or not (dxy or eurusd):
            raise RuntimeError("Required symbols missing - check the symbols section "
                               "of the config against your broker's Market Watch")
        self.correlation = CorrelationEngine(self.connector, self.cfg.strategy,
                                             self.gold, silver, eurusd, dxy)
        self.technical = TechnicalEngine(self.connector, self.cfg.strategy, self.gold)
        self.trend = TrendEngine(self.connector, self.cfg.trend, self.gold)
        self.news.refresh(force=False)
        self._evaluate_live_gate()
        log.info("Trading %s | silver %s | USD proxy %s | mode=%s | dry_run=%s | stage=%s",
                 self.gold, silver, dxy or f"1/{eurusd}", self.mode, self.connector.dry_run,
                 self.suite.limits.stage)

    def _evaluate_live_gate(self) -> None:
        """Prime directive 6: live only when all four conditions hold."""
        if self.mode != "live":
            return
        acc = self.connector.account()
        result = (self._live_gate(acc.login) if (self._live_gate and acc)
                  else GateResult(False, ["no account or gate available"]))
        self.live_gate_ok = result.ok
        if not result.ok:
            self.connector.dry_run = True                      # fail safe: send nothing
            log.critical("MODE=live but the live gate is NOT satisfied: %s. Nothing will be "
                         "sent.", "; ".join(result.missing))
            self._event("MODE=live requested but the live gate failed: "
                        + "; ".join(result.missing), "system")

    def run(self) -> None:
        self.setup()
        self.macro.start()
        if self.sentiment:
            self.sentiment.start()
        if self.notifier:
            self.notifier.start()
        self._event(f"Bot started on {self.gold} "
                    f"({'DRY-RUN' if self.connector.dry_run else 'trading'})", "system")
        interval = self.cfg.execution.loop_interval_seconds
        log.info("Main loop started (every %ss). Ctrl+C to stop.", interval)
        try:
            while not self._stop.is_set():
                try:
                    self.step()
                except Exception:  # noqa: BLE001 - never let one bad tick kill the bot
                    log.exception("Unhandled error in loop iteration")
                self._stop.wait(interval)
        except KeyboardInterrupt:
            log.info("Keyboard interrupt received")
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop.set()
        self.macro.stop()
        if self.sentiment:
            self.sentiment.stop()
        self.connector.shutdown()
        log.info("Bot stopped. Open positions keep their broker-side SL/TP.")
        if self.notifier:
            self.notifier.send("Bot stopped. Open trades keep their SL/TP.", "system")
            self.notifier.stop()

    # ----------------------------------------------------------------- #
    # Dashboard interface (called from the web server thread)
    # ----------------------------------------------------------------- #
    def submit_command(self, action: str) -> str:
        """Queue a dashboard command; it runs on the next loop (<= 10 s)."""
        if action not in COMMANDS:
            raise ValueError(f"unknown command {action!r}")
        self._commands.put(action)
        return f"'{action}' queued - applied within {self.cfg.execution.loop_interval_seconds}s"

    def status(self) -> Dict[str, Any]:
        with self._status_lock:
            return dict(self._status)

    def _event(self, text: str, category: Optional[str] = None) -> None:
        """Record an activity-feed entry and, if categorised, push an alert."""
        self._events.appendleft({"time": self._clock().isoformat(), "text": text})
        if category and self.notifier:
            self.notifier.send(text, category)

    def performance(self) -> Dict[str, Any]:
        return self._perf or {"ready": False}

    @property
    def paused(self) -> bool:
        return bool(self.state.get("paused", False))

    def _process_commands(self) -> None:
        while True:
            try:
                action = self._commands.get_nowait()
            except queue.Empty:
                return
            if action == "pause":
                self.state.set("paused", True)
                log.warning("Dashboard: new entries PAUSED")
                self._event("Paused new trades (dashboard)", "control")
            elif action == "resume":
                self.state.set("paused", False)
                log.warning("Dashboard: trading RESUMED")
                self._event("Resumed trading (dashboard)", "control")
            elif action == "close_all":
                # Pause too, or the next signal could immediately re-enter.
                self.state.set("paused", True)
                closed = self._close_bot_positions("dashboard close-all")
                self._event(f"Closed {closed} position(s) and paused (dashboard)", "control")

    # ----------------------------------------------------------------- #
    # One loop iteration
    # ----------------------------------------------------------------- #
    def step(self) -> None:
        ctx: Dict[str, Any] = {"connected": False}
        try:
            self._step(ctx)
        finally:
            self._publish(ctx)

    def _step(self, ctx: Dict[str, Any]) -> None:
        suite = self.suite
        if not self.connector.ensure_connected():
            log.error("MT5 unavailable; will retry next loop")
            suite.kill.record_equity_unavailable(self._clock())
            if self._was_connected:
                self._event("⚠ Lost connection to MT5 - retrying", "system")
                self._was_connected = False
            return
        if not self._was_connected:
            self._event("MT5 connection restored", "system")
            self._was_connected = True
        ctx["connected"] = True
        self._process_commands()

        acc = self.connector.account()
        if acc is None:
            suite.kill.record_equity_unavailable(self._clock())
            return
        ctx["account"] = acc
        self._currency = acc.currency
        now = self._clock()

        magic = self.cfg.execution.magic
        positions = self.connector.positions(self.gold, magic)
        ctx["positions"] = positions
        self._track_exits(positions)

        # --- continuous risk: baselines, ladder, kill switch (every loop)
        if self.connector.utc_offset_known:
            self._periods = suite.periods.update(acc.equity, now,
                                                 self.connector.utc_offset_seconds)
        ladder = suite.ladder.update(acc.equity, now)
        self._ladder_level = ladder.level
        if ladder.changed:
            self._event(f"Drawdown ladder is now L{ladder.level} "
                        f"(-{ladder.dd_pct:.1f}% from the equity high of {ladder.hwm:,.2f})",
                        "risk")
        suite.kill.record_equity(now, acc.equity)
        reason = suite.kill.check(now, ladder.level, bool(positions))
        if reason and not suite.kill.is_killed():
            suite.kill.trip(reason, now)
            self._event(f"🛑 KILL SWITCH ({reason}): flattening all bot trades and halting. "
                        "Reset needs a written note: python -m goldbot.riskctl reset-kill",
                        "risk")
        if suite.kill.is_killed():
            ctx["killed"] = True
            self._flatten("kill switch: " + ", ".join(suite.kill.reasons()))
            return

        day = self._day_key(now)
        streak = suite.ledger.streak()
        mon = suite.engine.monitor(
            equity=acc.equity,
            day_start_equity=self._periods.day_start_equity if self._periods else acc.equity,
            week_start_equity=self._periods.week_start_equity if self._periods else acc.equity,
            losing_trades_today=suite.ledger.losing_today(day),
            consecutive_losses=streak.consecutive_losses,
            day_stopped=suite.ledger.day_stopped(day), ladder_level=ladder.level,
            halted=bool(suite.store.active_halts()))
        ctx["monitor"] = mon
        self._alert_block_changes(mon)

        self.news.refresh()
        news_state = self.news.state()
        ai = self.sentiment.snapshot() if self.sentiment else SentimentSnapshot()
        ctx["news"], ctx["ai"] = news_state, ai
        if ai.shock_active and self.state.get("shock_alerted") != ai.shock_headline:
            self.state.set("shock_alerted", ai.shock_headline)
            until = ai.shock_until.strftime("%H:%M") if ai.shock_until else "?"
            self._event(f"⚡ AI SHOCK - new trades paused until {until} UTC: "
                        f"{ai.shock_headline}", "shock")

        self._refresh_performance()
        self._maybe_send_summaries(acc)

        if positions:
            if self.cfg.news.manage_open_positions and news_state.imminent:
                window = timedelta(minutes=self.cfg.news.minutes_before
                                   + self.cfg.news.minutes_after)
                self._protect_all(positions, f"News {news_state.imminent[0].title}",
                                  "news_handled", window)
            if ai.shock_active and self.cfg.ai_news.shock_protect_positions:
                window = timedelta(minutes=self.cfg.ai_news.shock_pause_minutes)
                self._protect_all(positions, f"AI shock {ai.shock_headline[:60]}",
                                  "shock_handled", window)

        if not self.connector.market_is_live(self.gold):
            ctx["market_live"] = False
            if not self._market_closed_logged:
                log.info("No fresh ticks on %s (market closed or feed paused)", self.gold)
                self._market_closed_logged = True
            return
        ctx["market_live"] = True
        self._market_closed_logged = False

        tech = self.technical.evaluate()
        if not tech.ok or tech.bar_time == self._last_bar:
            return
        self._last_bar = tech.bar_time

        ys = self.macro.snapshot()
        cs = self.correlation.evaluate()
        trend = self.trend.evaluate()
        if positions and self.cfg.trend.exit_on_reversal:
            self._trend_exits(positions, trend)
        positions = self.connector.positions(self.gold, magic)
        ctx["positions"] = positions

        sig = build_signal(ys, cs, tech, self.cfg.strategy, trend, ai)
        action = self._decide_and_execute(sig, tech, news_state, ai, positions, acc)
        self._report(sig, ys, cs, tech, trend, ai, news_state, positions, action,
                     mon.daily_loss_pct / 100)

    def _day_key(self, now: datetime) -> str:
        return self._periods.day_key if self._periods else now.date().isoformat()

    def _alert_block_changes(self, mon: MonitorStatus) -> None:
        current = set(mon.reasons)
        newly = current - self._blocked_reasons
        if newly:
            self._event(f"⛔ New entries blocked: {', '.join(sorted(newly))} "
                        f"(day {-mon.daily_loss_pct:+.2f}%, week {-mon.weekly_loss_pct:+.2f}%)",
                        "risk")
        elif self._blocked_reasons and not current:
            self._event("New entries allowed again", "risk")
        self._blocked_reasons = current

    # ----------------------------------------------------------------- #
    # Entry gates and execution
    # ----------------------------------------------------------------- #
    def _entry_gates(self, news_state: NewsState, ai: SentimentSnapshot,
                     positions: List[PositionInfo]) -> List[str]:
        e = self.cfg.execution
        now = self._clock()
        gates = []
        if self.paused:
            gates.append("paused from dashboard")
        if news_state.blackout:
            gates.append(f"news blackout: {news_state.reason}")
        if ai.shock_active:
            gates.append(f"AI shock pause: {ai.shock_headline[:60]}")
        if len(positions) >= e.max_open_positions:
            gates.append(f"max open positions ({e.max_open_positions})")
        last_exit = self.state.get("last_exit_time")
        cooldown = timedelta(minutes=e.cooldown_minutes)
        if last_exit and now - datetime.fromisoformat(last_exit) < cooldown:
            gates.append("post-exit cooldown")
        if now.weekday() not in e.trade_weekdays:
            gates.append("non-trading weekday")
        if e.session_filter_enabled and not self._in_session(now):
            gates.append(f"outside session {e.session_start_utc}-{e.session_end_utc} UTC")
        if not self.connector.utc_offset_known:
            gates.append("broker UTC offset not yet detected")
        return gates

    def _in_session(self, now: datetime) -> bool:
        start = _parse_hhmm(self.cfg.execution.session_start_utc)
        end = _parse_hhmm(self.cfg.execution.session_end_utc)
        t = now.time()
        return start <= t < end if start < end else (t >= start or t < end)

    # -- risk-engine inputs ------------------------------------------------ #
    def _symbol_info(self, spec) -> SymbolInfo:
        return SymbolInfo(
            name=spec.name, canonical=CANONICAL_GOLD, point=spec.point, digits=spec.digits,
            tick_size=spec.tick_size, tick_value=spec.tick_value,
            contract_size=spec.contract_size, volume_min=spec.volume_min,
            volume_max=min(spec.volume_max, self.cfg.risk.max_lot),
            volume_step=spec.volume_step, stops_level=spec.stops_level,
            freeze_level=spec.freeze_level)

    def _position_risks(self, positions: List[PositionInfo], spec) -> List[PositionRisk]:
        out = []
        for p in positions:
            risk = self.suite.ledger.risk_for(p.ticket)
            if risk is None and p.sl:
                risk = float(actual_risk_usd(fill=p.price_open, sl=p.sl,
                                             tick_size=spec.tick_size,
                                             tick_value=spec.tick_value, lots=p.volume))
            out.append(PositionRisk(p.ticket, CANONICAL_GOLD, risk,
                                    p.volume * spec.contract_size * p.price_open))
        return out

    def _spread_baseline(self, now: datetime) -> Optional[float]:
        cached = self._spread_cache
        if cached and cached[0] == now.hour and now - cached[1] < cached[3]:
            return cached[2]
        value = self.connector.hourly_spread_median(self.gold, now.hour)
        ttl = timedelta(minutes=30) if value is not None else timedelta(minutes=5)
        self._spread_cache = (now.hour, now, value, ttl)
        return value

    def _atr_1m(self) -> Optional[float]:
        df = self.connector.get_rates(self.gold, "M1", 120)
        if df is None or len(df) < 20:
            return None
        value = float(atr_series(df, 14).iat[-1])
        return value if value == value else None

    def _build_state(self, *, acc, positions, news_state, tick, spec, now, side) -> PortfolioState:
        suite, day = self.suite, self._day_key(now)
        streak = suite.ledger.streak()
        halts = suite.store.active_halts()
        periods = self._periods
        known = self.connector.utc_offset_known
        return PortfolioState(
            now_utc=now,
            server_utc_offset_s=self.connector.utc_offset_seconds if known else None,
            equity=acc.equity,
            day_start_equity=periods.day_start_equity if periods else acc.equity,
            week_start_equity=periods.week_start_equity if periods else acc.equity,
            open_positions=self._position_risks(positions, spec),
            trades_today=suite.ledger.trades_today(day),
            losing_trades_today=suite.ledger.losing_today(day),
            consecutive_losses=streak.consecutive_losses,
            risk_halved_by_streak=streak.halved,
            day_stopped=suite.ledger.day_stopped(day),
            ladder_level=self._ladder_level,
            risk_cap_pct=suite.ladder.risk_cap_pct(),
            broker_connected=True,
            clock_drift_ms=self.connector.clock_drift_ms(),
            kill_flag=suite.kill.flag_path.exists(),
            halted=bool(halts),
            halt_reason="; ".join(f"{h['kind']}: {h['reason']}" for h in halts),
            mode=self.mode, orders_enabled=not self.connector.dry_run,
            account_login=acc.login, expected_login=self.cfg.mt5.login or None,
            account_is_demo=acc.is_demo, live_gate_ok=self.live_gate_ok,
            news_blackout=news_state.blackout, news_reason=news_state.reason,
            calendar_fresh=self.news.calendar_is_valid(),
            spread_points=(tick.ask - tick.bid) / spec.point,
            spread_hour_median_points=self._spread_baseline(now),
            margin_used=max(0.0, acc.equity - acc.margin_free),
            margin_per_lot=self.connector.calc_margin(
                self.gold, side, 1.0, tick.ask if side == "BUY" else tick.bid),
            independent_mid=(self.independent_feed(CANONICAL_GOLD)
                             if self.independent_feed else None),
            atr_1m=self._atr_1m(),
            recent_order_sizes=suite.ledger.recent_sizes(),
            symbol=self._symbol_info(spec))

    # -- decision and execution -------------------------------------------- #
    def _decide_and_execute(self, sig: Signal, tech: TechnicalSnapshot,
                            news_state: NewsState, ai: SentimentSnapshot,
                            positions: List[PositionInfo], acc) -> str:
        if sig.direction is None:
            return "blocked: " + "; ".join(sig.blocked_by) if sig.blocked_by else "no signal"

        gates = self._entry_gates(news_state, ai, positions)
        if gates:
            return f"{sig.direction} skipped: " + "; ".join(gates)

        spec = self.connector.symbol_spec(self.gold)
        tick = self.connector.get_tick(self.gold)
        if spec is None or tick is None:
            return f"{sig.direction} skipped: no symbol spec/tick"
        levels, why = self.risk.plan_levels(sig.direction, tick, tech.atr, spec)
        if levels is None:
            return f"{sig.direction} skipped: {why}"
        try:
            intent = OrderIntent(intent_id=uuid.uuid4().hex[:16], strategy=STRATEGY_ID,
                                 symbol=CANONICAL_GOLD, side=levels.side, entry=levels.entry,
                                 sl=levels.sl, tp=levels.tp, bid=tick.bid, ask=tick.ask)
        except InvalidIntent as exc:
            return f"{sig.direction} skipped: invalid order intent ({exc})"

        now = self._clock()
        state = self._build_state(acc=acc, positions=positions, news_state=news_state,
                                  tick=tick, spec=spec, now=now, side=levels.side)
        decision = self.suite.engine.approve(intent, state)      # prime directive 2
        if not decision.approved:
            log.info("RISK REJECT %s: %s", decision.reason_code, decision.detail)
            return f"{sig.direction} REJECTED {decision.reason_code}: {decision.detail}"
        lots = decision.adjusted_size

        comment = f"{STRATEGY_ID} {sig.total:+d}"
        before = {p.ticket for p in self.connector.positions(self.gold,
                                                             self.cfg.execution.magic)}
        if not self.connector.dry_run:
            self.suite.kill.record_order_sent(now)
        result = self.connector.open_market(self.gold, levels.side, lots, levels.sl,
                                            levels.tp, comment)
        desc = (f"{levels.side} {lots} @ {levels.entry} SL {levels.sl} TP {levels.tp} "
                f"risk {decision.risk_usd:.2f} {acc.currency} ({decision.risk_pct:g}%)")
        if not result.dry_run:
            self.suite.kill.record_order_result(now, result.ok, result.retcode)
            self._react_to_retcode(result, now)
        if result.ok:
            log.info("ORDER %s%s", "[DRY-RUN] " if result.dry_run else "", desc)
            if not result.dry_run:
                self._after_fill(sig, decision, spec, before, result, now)
            self._event(("DRY-RUN " if result.dry_run else "✅ Opened ") + desc +
                        f" (score {sig.total:+d})", "trade")
            return ("DRY-RUN " if result.dry_run else "OPENED ") + desc
        log.error("ORDER FAILED %s -> retcode %s %s", desc, result.retcode, result.message)
        self._event(f"❌ Order FAILED: {desc} ({result.retcode} {result.message})", "trade")
        return f"FAILED {desc}: {result.retcode} {result.message}"

    def _react_to_retcode(self, result, now: datetime) -> None:
        action = action_for(result.retcode)
        if action is Action.HALT:
            self.suite.store.add_halt("HALT", f"retcode {result.retcode}: {result.message}",
                                      now)
            self._event(f"🛑 TRADING HALTED by broker code {result.retcode} "
                        f"({result.message}). Clear it with: python -m goldbot.riskctl "
                        "clear-halt", "risk")
        elif action is Action.UNKNOWN:
            log.error("Unknown MT5 retcode %s (%s)", result.retcode, result.message)

    def _after_fill(self, sig: Signal, decision, spec, before: set, result,
                    now: datetime) -> None:
        """Book the trade, then verify the stop is attached and the risk is as planned."""
        fresh = [p for p in self.connector.positions(self.gold, self.cfg.execution.magic)
                 if p.ticket not in before]
        pos = fresh[0] if fresh else None
        ticket = pos.ticket if pos else result.ticket
        if self._prev_tickets is not None:       # so a very fast close is still noticed
            self._prev_tickets.add(ticket)
        self.suite.ledger.record_open(self._day_key(now), ticket, decision.risk_usd,
                                      decision.adjusted_size)
        self._remember_trade(ticket, sig)
        if pos is None:
            self._event("⚠ Order accepted but the new position was not found to verify its "
                        "stop-loss. Check MT5 now.", "risk")
            return
        problem = ""
        if not pos.sl:
            problem = "NO STOP-LOSS attached"
        else:
            actual = actual_risk_usd(fill=pos.price_open, sl=pos.sl, tick_size=spec.tick_size,
                                     tick_value=spec.tick_value, lots=pos.volume)
            if not overshoot_ok(actual=actual, planned=Decimal(str(decision.risk_usd))):
                problem = (f"actual risk ${float(actual):.2f} is more than 10% above the "
                           f"planned ${decision.risk_usd:.2f}")
        if problem:
            res = self.connector.close_position(pos)
            self._event(f"🛑 Closed #{pos.ticket} immediately: {problem} "
                        f"({'ok' if res.ok else res.message})", "risk")

    # ----------------------------------------------------------------- #
    # Position management
    # ----------------------------------------------------------------- #
    def _track_exits(self, positions: List[PositionInfo]) -> None:
        now = self._clock()
        tickets = {p.ticket for p in positions}
        if self._prev_tickets is not None and self._prev_tickets - tickets:
            closed = self._prev_tickets - tickets
            log.info("Position(s) closed: %s - cooldown %d min", sorted(closed),
                     self.cfg.execution.cooldown_minutes)
            for ticket in closed:
                self._pending_close[ticket] = now
            self.state.set("last_exit_time", now.isoformat())
            self._perf_at = None  # refresh stats now
        self._prev_tickets = tickets
        self._book_closed(now)

    def _book_closed(self, now: datetime) -> None:
        """Book each closed position in the ledger once MT5 history has its deals."""
        for ticket, since in list(self._pending_close.items()):
            trades = build_trades(self.connector.position_deals(ticket),
                                  self.cfg.execution.magic)
            if not trades:
                if now - since > PENDING_CLOSE_GIVE_UP:
                    del self._pending_close[ticket]
                    self._event(f"⚠ Could not find the deals of closed position #{ticket}; "
                                "its result was not booked", "risk")
                continue
            del self._pending_close[ticket]
            t = trades[0]
            r = self.suite.ledger.record_close(self._day_key(now), ticket, t.profit)
            self._event(self._close_text(ticket, t, r), "trade")

    def _remember_trade(self, ticket: int, sig: Signal) -> None:
        """Store the entry score breakdown so performance can attribute it."""
        meta = self.state.get("trade_meta", {})
        meta[str(ticket)] = {"total": sig.total, "parts": {
            "yield": sig.yield_score, "dollar": sig.dxy_score, "silver": sig.silver_score,
            "trend": sig.trend_score, "ai_news": sig.news_score, "vwap": sig.vwap_score}}
        if len(meta) > 1000:  # keep the state file small
            meta = dict(list(meta.items())[-1000:])
        self.state.set("trade_meta", meta)

    def _close_text(self, ticket: int, t, r: Optional[float]) -> str:
        icon = "🟢" if t.profit > 0 else "🔴"
        why = {"TP": "take profit", "SL": "stop loss", "BOT": "closed by bot",
               "MANUAL": "closed manually", "STOP_OUT": "STOP OUT"}.get(t.exit_reason,
                                                                        t.exit_reason)
        rtxt = f", {r:+.2f}R" if r is not None else ""
        return (f"{icon} Closed {t.side} {t.volume} #{ticket}: {t.profit:+.2f} "
                f"{self._currency} ({why}{rtxt}, {t.hold_minutes:.0f} min)")

    # ----------------------------------------------------------------- #
    # Performance and summaries
    # ----------------------------------------------------------------- #
    def _refresh_performance(self) -> None:
        pcfg = self.cfg.performance
        now = self._clock()
        if self._perf_at and now - self._perf_at < timedelta(minutes=pcfg.refresh_minutes):
            return
        self._perf_at = now
        try:
            deals = self.connector.deal_history(now - timedelta(days=pcfg.lookback_days))
            trades = build_trades(deals, self.cfg.execution.magic,
                                  self.state.get("trade_meta", {}))
        except Exception:  # noqa: BLE001 - stats must never break trading
            log.exception("Performance refresh failed")
            return
        self._perf_trades = trades
        self._perf = json_safe({
            "ready": True,
            "updated": now.isoformat(),
            "currency": self._currency,
            "dry_run": self.connector.dry_run,
            "periods": {"7d": compute_stats(trades, now, 7),
                        "30d": compute_stats(trades, now, 30),
                        "all": compute_stats(trades, now, None)},
            "recent": [t.to_json() for t in trades[-pcfg.recent_trades:][::-1]],
        })

    def _maybe_send_summaries(self, acc) -> None:
        acfg = self.cfg.alerts
        if not self.notifier or not acfg.daily_summary_utc:
            return
        now = self._clock()
        if now.weekday() > 4 or now.time() < _parse_hhmm(acfg.daily_summary_utc):
            return
        today = now.date().isoformat()
        if self.state.get("last_daily_summary") == today:
            return
        self.state.set("last_daily_summary", today)
        self._perf_at = None
        self._refresh_performance()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        todays = [t for t in self._perf_trades if t.close_time >= start]
        stats = compute_stats(todays, now, None)
        text = summary_text(stats, f"📊 Daily summary {today}", acc.currency)
        text += f"\nEquity: {acc.equity:,.2f} {acc.currency}"
        self._event(text, "summary")
        if now.weekday() == acfg.weekly_summary_weekday:
            week = compute_stats(self._perf_trades, now, 7)
            self._event(summary_text(week, "📈 Weekly summary (last 7 days)", acc.currency),
                        "summary")

    def _close_bot_positions(self, reason: str) -> int:
        closed = 0
        for pos in self.connector.positions(magic=self.cfg.execution.magic):
            res = self.connector.close_position(pos)
            log.warning("%s: close %s #%s %s %s -> %s", reason, pos.symbol, pos.ticket,
                        pos.side, pos.volume, "ok" if res.ok else res.message)
            closed += int(res.ok)
        return closed

    def _flatten(self, reason: str) -> None:
        if self.cfg.risk.close_all_account_positions_on_halt:
            targets = self.connector.positions()
        else:
            targets = self.connector.positions(magic=self.cfg.execution.magic)
        for pos in targets:
            res = self.connector.close_position(pos)
            log.warning("Guardrail close %s #%s %s %s -> %s", pos.symbol, pos.ticket,
                        pos.side, pos.volume, "ok" if res.ok else res.message)
        if targets:
            log.warning("Trading halted: %s", reason)
            self._event(f"Guardrail closed {len(targets)} position(s): {reason}", "risk")

    def _trend_exits(self, positions: List[PositionInfo], trend: TrendSnapshot) -> None:
        """Close trades whose exit-timeframe trend has flipped against them."""
        tf = self.cfg.trend.exit_timeframe
        frame = next((f for f in trend.frames if f.timeframe == tf), None)
        if frame is None or frame.direction == 0:
            return  # ranging or unknown is not a reversal
        for pos in positions:
            side = 1 if pos.side == BUY else -1
            if frame.direction == -side:
                res = self.connector.close_position(pos)
                msg = (f"Trend exit: {tf} turned {'DOWN' if side > 0 else 'UP'} - "
                       f"closed {pos.side} #{pos.ticket}")
                log.warning("%s (%s)", msg, "ok" if res.ok else res.message)
                self._event(msg, "trade")

    def _protect_all(self, positions: List[PositionInfo], title: str, state_key: str,
                     window: timedelta) -> None:
        """Breakeven or trim each open trade once per event window."""
        now = self._clock()
        handled = self.state.get(state_key, {})
        for pos in positions:
            key = str(pos.ticket)
            if key in handled and now - datetime.fromisoformat(handled[key]) < window:
                continue  # already protected for this event
            spec = self.connector.symbol_spec(pos.symbol)
            tick = self.connector.get_tick(pos.symbol)
            if spec is None or tick is None:
                continue
            if self._protect_position(pos, spec, tick, title):
                handled[key] = now.isoformat()
                self._event(f"🛡 {title}: protected #{pos.ticket}",
                            "shock" if state_key == "shock_handled" else "news")
        live = {str(p.ticket) for p in positions}
        self.state.set(state_key, {k: v for k, v in handled.items() if k in live})

    def _protect_position(self, pos: PositionInfo, spec, tick, title: str) -> bool:
        ncfg = self.cfg.news
        buffer = ncfg.breakeven_buffer_points * spec.point
        min_gap = spec.stops_level * spec.point
        if pos.side == BUY:
            be = pos.price_open + buffer
            in_profit = tick.bid > be
            already_protected = pos.sl != 0 and pos.sl >= be
            placeable = be < tick.bid - min_gap
        else:
            be = pos.price_open - buffer
            in_profit = tick.ask < be
            already_protected = pos.sl != 0 and pos.sl <= be
            placeable = be > tick.ask + min_gap

        if in_profit and already_protected:
            log.info("%s: #%s already protected at breakeven or better", title, pos.ticket)
            return True
        if in_profit and placeable:
            res = self.connector.modify_sltp(pos, be, pos.tp)
            log.warning("%s: #%s SL -> breakeven %.2f (%s)", title, pos.ticket, be,
                        "ok" if res.ok else res.message)
            return res.ok

        # Losing (or breakeven cannot be placed yet): cut exposure instead.
        part = round_volume_down(pos.volume * ncfg.partial_close_fraction, spec)
        if part >= spec.volume_min and pos.volume - part >= spec.volume_min - 1e-9:
            res = self.connector.close_position(pos, part)
            log.warning("%s: #%s closed %.2f of %.2f lots (%s)", title,
                        pos.ticket, part, pos.volume, "ok" if res.ok else res.message)
            return res.ok
        if ncfg.close_fully_if_unsplittable:
            res = self.connector.close_position(pos)
            log.warning("%s: #%s volume %.2f cannot be split - closed fully (%s)",
                        title, pos.ticket, pos.volume, "ok" if res.ok else res.message)
            return res.ok
        log.warning("%s: #%s cannot be split; left unchanged", title, pos.ticket)
        return True

    # ----------------------------------------------------------------- #
    # Reporting
    # ----------------------------------------------------------------- #
    def _report(self, sig: Signal, ys: YieldSnapshot, cs: CorrelationSnapshot,
                tech: TechnicalSnapshot, trend: TrendSnapshot, ai: SentimentSnapshot,
                ns: NewsState, positions, action: str, daily_loss: float) -> None:
        news_txt = f"BLACKOUT {ns.reason}" if ns.blackout else "clear"
        log.info(
            "BAR %s | score %+d (Y%+d D%+d S%+d T%+d N%+d V%+d) | 10Y %s d15 %sbp | "
            "R %s | trend %s | AI %s (%d) | close %s VWAP %s ATR %s | news %s | "
            "day P/L %s | pos %d | %s",
            f"{tech.bar_time:%H:%M}Z", sig.total, sig.yield_score, sig.dxy_score,
            sig.silver_score, sig.trend_score, sig.news_score, sig.vwap_score,
            _fmt(ys.value, ".3f"), _fmt(ys.delta_bp, "+.1f"), _fmt(cs.correlation),
            trend.summary(), _fmt(ai.sentiment, "+.2f"), ai.relevant_count,
            _fmt(tech.close), _fmt(tech.vwap), _fmt(tech.atr), news_txt,
            f"{-daily_loss:+.2%}", len(positions), action,
        )
        for line in ai.top:
            log.debug("AI top headline: %s", line)
        if sig.reasons:
            log.debug("Signal notes: %s", "; ".join(sig.reasons))

        self._last_bar_report = {
            "bar_time": tech.bar_time.isoformat() if tech.bar_time else None,
            "total": sig.total,
            "parts": {"yield": sig.yield_score, "dollar": sig.dxy_score,
                      "silver": sig.silver_score, "trend": sig.trend_score,
                      "ai_news": sig.news_score, "vwap": sig.vwap_score},
            "signal": sig.direction, "blocked_by": sig.blocked_by, "action": action,
            "reasons": sig.reasons[:12],
            "trend": [{"tf": f.timeframe, "dir": f.direction, "adx": _num(f.adx)}
                      for f in trend.frames],
            "yield": _num(ys.value), "yield_delta_bp": _num(ys.delta_bp),
            "yield_stale": ys.stale, "gs_corr": _num(cs.correlation),
            "close": _num(tech.close), "vwap": _num(tech.vwap), "atr": _num(tech.atr),
        }
        self._journal({
            "logged_at": self._clock().isoformat(),
            "bar_time": tech.bar_time.isoformat() if tech.bar_time else "",
            "total": sig.total, "macro": sig.macro, "yield_pts": sig.yield_score,
            "dxy_pts": sig.dxy_score, "silver_pts": sig.silver_score,
            "trend_pts": sig.trend_score, "news_pts": sig.news_score,
            "vwap_pts": sig.vwap_score, "signal": sig.direction or "",
            "blocked_by": "; ".join(sig.blocked_by), "action": action,
            "yield": ys.value, "yield_delta_bp": ys.delta_bp, "yield_z1h": ys.zscore_1h,
            "yield_stale": ys.stale, "gs_corr": cs.correlation,
            "gold_break": cs.gold_break, "silver_break": cs.silver_break,
            "usd_break": cs.usd_break, "usd_roc_pct": cs.usd_roc_pct,
            "divergence": cs.divergence, "close": tech.close, "vwap": tech.vwap,
            "atr": tech.atr, "trend": trend.summary(), "ai_sentiment": ai.sentiment,
            "ai_headlines": " || ".join(ai.top), "news": news_txt,
            "open_positions": len(positions),
        })

    def _publish(self, ctx: Dict[str, Any]) -> None:
        """Build the JSON-safe snapshot the dashboard serves."""
        acc, mon = ctx.get("account"), ctx.get("monitor")
        ns: Optional[NewsState] = ctx.get("news")
        ai: Optional[SentimentSnapshot] = ctx.get("ai")
        dash = self.cfg.dashboard

        if not ctx.get("connected"):
            mode = "DISCONNECTED"
        elif ctx.get("killed"):
            mode = "KILLED"
        elif mon is not None and mon.entries_blocked:
            mode = "HALTED"
        elif self.paused:
            mode = "PAUSED"
        elif self.connector.dry_run:
            mode = "DRY-RUN"
        else:
            mode = "LIVE"

        status: Dict[str, Any] = {
            "updated": self._clock().isoformat(),
            "mode": mode,
            "symbol": self.gold,
            "connected": bool(ctx.get("connected")),
            "market_live": ctx.get("market_live"),
            "dry_run": self.connector.dry_run,
            "paused": self.paused,
            "halted": bool(ctx.get("killed") or (mon and mon.entries_blocked)),
            "halt_reason": (", ".join(self.suite.kill.reasons())
                            if ctx.get("killed") else ", ".join(mon.reasons) if mon else ""),
            "risk": self._risk_status(mon, ctx.get("killed", False)),
            "thresholds": {"buy": self.cfg.strategy.buy_threshold,
                           "sell": self.cfg.strategy.sell_threshold},
            "weights": {"yield": self.cfg.strategy.weight_yield,
                        "dollar": self.cfg.strategy.weight_dxy,
                        "silver": self.cfg.strategy.weight_silver,
                        "trend": sum(self.cfg.trend.weights),
                        "ai_news": self.cfg.ai_news.weight,
                        "vwap": self.cfg.strategy.weight_vwap},
            "controls": {"enabled": dash.allow_controls,
                         "close_all": dash.allow_close_all},
            "account": None,
            "positions": [
                {"ticket": p.ticket, "side": p.side, "volume": p.volume,
                 "open": p.price_open, "sl": p.sl, "tp": p.tp, "profit": _num(p.profit)}
                for p in ctx.get("positions", [])
            ],
            "news": None,
            "ai": None,
            "last_bar": self._last_bar_report or None,
            "events": list(self._events),
        }
        if acc is not None:
            status["account"] = {
                "balance": acc.balance, "equity": acc.equity, "currency": acc.currency,
                "demo": acc.is_demo,
                "day_pl_pct": _num(-mon.daily_loss_pct) if mon else None,
            }
        if ns is not None:
            nxt = ns.next_event
            status["news"] = {
                "blackout": ns.blackout, "reason": ns.reason,
                "next_event": (f"{nxt.currency} {nxt.title}" if nxt else None),
                "next_event_time": nxt.time.isoformat() if nxt else None,
            }
        if ai is not None:
            status["ai"] = {
                "active": bool(self.sentiment and self.sentiment.active),
                "sentiment": _num(ai.sentiment), "count": ai.relevant_count,
                "score": ai.score, "top": ai.top, "error": ai.error,
                "shock_active": ai.shock_active, "shock_headline": ai.shock_headline,
                "shock_until": ai.shock_until.isoformat() if ai.shock_until else None,
            }
        with self._status_lock:
            self._status = status

    def _risk_status(self, mon: Optional[MonitorStatus], killed: bool) -> Dict[str, Any]:
        last = self.suite.store.decisions(1)
        return {
            "stage": self.suite.limits.stage, "mode": self.mode,
            "ladder_level": self._ladder_level, "killed": killed,
            "kill_reasons": self.suite.kill.reasons(),
            "blocked": list(mon.reasons) if mon else [],
            "daily_loss_pct": _num(mon.daily_loss_pct) if mon else None,
            "weekly_loss_pct": _num(mon.weekly_loss_pct) if mon else None,
            "per_trade_pct": self.suite.limits.per_trade_cap(),
            "live_gate_ok": self.live_gate_ok,
            "last_decision": ({"time": last[0]["ts"], "approved": bool(last[0]["approved"]),
                               "reason": last[0]["reason_code"], "detail": last[0]["detail"]}
                              if last else None),
        }

    def _journal(self, row: dict) -> None:
        path = Path(self.cfg.journal_file)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            new = not path.exists()
            with open(path, "a", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=JOURNAL_FIELDS)
                if new:
                    writer.writeheader()
                writer.writerow(row)
        except OSError as exc:
            log.warning("Journal write failed: %s", exc)
