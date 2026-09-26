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
from collections import deque
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .config import AppConfig
from .correlation_engine import CorrelationEngine, CorrelationSnapshot
from .macro_engine import MacroDataEngine, YieldSnapshot
from .mt5_connector import BUY, PositionInfo
from .news_filter import NewsFilterEngine, NewsState
from .notifier import TelegramNotifier
from .performance import build_trades, compute_stats, json_safe, summary_text
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
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 sentiment: Optional[SentimentEngine] = None,
                 notifier: Optional[TelegramNotifier] = None):
        self.cfg = cfg
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
        log.info("Trading %s | silver %s | USD proxy %s | dry_run=%s",
                 self.gold, silver, dxy or f"1/{eurusd}", self.connector.dry_run)

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
        if not self.connector.ensure_connected():
            log.error("MT5 unavailable; will retry next loop")
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
            return
        ctx["account"] = acc
        self._currency = acc.currency

        magic = self.cfg.execution.magic
        positions = self.connector.positions(self.gold, magic)
        ctx["positions"] = positions
        self._track_exits(positions)

        guard = self.risk.update_daily(acc)
        ctx["guard"] = guard
        if guard.just_triggered:
            self._event(f"🛑 DAILY LOSS LIMIT: {guard.reason}. Closing all bot trades.", "risk")
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

        if guard.halted:
            self._flatten(guard.reason)
            return

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
                     guard.daily_loss_pct)

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

        plan, why = self.risk.plan_trade(sig.direction, tick, tech.atr, acc, spec)
        if plan is None:
            return f"{sig.direction} skipped: {why}"

        comment = f"{self.cfg.execution.order_comment} {sig.total:+d}"
        result = self.connector.open_market(self.gold, plan.side, plan.volume,
                                            plan.sl, plan.tp, comment)
        desc = (f"{plan.side} {plan.volume} @ {plan.entry} SL {plan.sl} TP {plan.tp} "
                f"risk {plan.risk_amount:.2f} {acc.currency}")
        if result.ok:
            log.info("ORDER %s%s", "[DRY-RUN] " if result.dry_run else "", desc)
            if not result.dry_run and result.ticket:
                self._remember_trade(result.ticket, sig)
            self._event(("DRY-RUN " if result.dry_run else "✅ Opened ") + desc +
                        f" (score {sig.total:+d})", "trade")
            return ("DRY-RUN " if result.dry_run else "OPENED ") + desc
        log.error("ORDER FAILED %s -> retcode %s %s", desc, result.retcode, result.message)
        self._event(f"❌ Order FAILED: {desc} ({result.retcode} {result.message})", "trade")
        return f"FAILED {desc}: {result.retcode} {result.message}"

    # ----------------------------------------------------------------- #
    # Position management
    # ----------------------------------------------------------------- #
    def _track_exits(self, positions: List[PositionInfo]) -> None:
        tickets = {p.ticket for p in positions}
        if self._prev_tickets is not None and self._prev_tickets - tickets:
            closed = self._prev_tickets - tickets
            log.info("Position(s) closed: %s - cooldown %d min", sorted(closed),
                     self.cfg.execution.cooldown_minutes)
            for ticket in sorted(closed):
                self._event(self._close_message(ticket), "trade")
            self.state.set("last_exit_time", self._clock().isoformat())
            self._perf_at = None  # refresh stats now
        self._prev_tickets = tickets

    def _remember_trade(self, ticket: int, sig: Signal) -> None:
        """Store the entry score breakdown so performance can attribute it."""
        meta = self.state.get("trade_meta", {})
        meta[str(ticket)] = {"total": sig.total, "parts": {
            "yield": sig.yield_score, "dollar": sig.dxy_score, "silver": sig.silver_score,
            "trend": sig.trend_score, "ai_news": sig.news_score, "vwap": sig.vwap_score}}
        if len(meta) > 1000:  # keep the state file small
            meta = dict(list(meta.items())[-1000:])
        self.state.set("trade_meta", meta)

    def _close_message(self, ticket: int) -> str:
        deals = self.connector.position_deals(ticket)
        trades = build_trades(deals, self.cfg.execution.magic)
        if not trades:
            return f"Position #{ticket} closed"
        t = trades[0]
        icon = "🟢" if t.profit > 0 else "🔴"
        why = {"TP": "take profit", "SL": "stop loss", "BOT": "closed by bot",
               "MANUAL": "closed manually", "STOP_OUT": "STOP OUT"}.get(t.exit_reason,
                                                                        t.exit_reason)
        return (f"{icon} Closed {t.side} {t.volume} #{ticket}: {t.profit:+.2f} "
                f"{self._currency} ({why}, {t.hold_minutes:.0f} min)")

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
        acc, guard = ctx.get("account"), ctx.get("guard")
        ns: Optional[NewsState] = ctx.get("news")
        ai: Optional[SentimentSnapshot] = ctx.get("ai")
        dash = self.cfg.dashboard

        if not ctx.get("connected"):
            mode = "DISCONNECTED"
        elif guard is not None and guard.halted:
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
            "halted": bool(guard and guard.halted),
            "halt_reason": guard.reason if guard else "",
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
                "day_pl_pct": _num(-guard.daily_loss_pct * 100) if guard else None,
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
