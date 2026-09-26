"""BotOrchestrator: the main loop that assembles signals and drives MT5.

Every ``loop_interval_seconds`` (default 10 s) it:
  1. checks the MT5 connection (reconnects if needed),
  2. enforces the daily equity guardrail (flattens and halts on breach),
  3. manages open trades ahead of high-impact news,
  4. on each newly CLOSED execution bar, scores the four signal blocks and,
     if every gate passes, sizes and sends the order.

Entries are evaluated once per closed bar, so live behaviour matches what a
bar-by-bar backtest of the same rules would do.
"""

from __future__ import annotations

import csv
import logging
import math
import threading
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, Optional

from .config import AppConfig
from .correlation_engine import CorrelationEngine, CorrelationSnapshot
from .macro_engine import MacroDataEngine, YieldSnapshot
from .mt5_connector import BUY, PositionInfo
from .news_filter import NewsFilterEngine, NewsState
from .risk_manager import RiskManager, round_volume_down
from .signal_model import Signal, build_signal
from .state import StateStore
from .technical_engine import TechnicalEngine, TechnicalSnapshot

log = logging.getLogger(__name__)

JOURNAL_FIELDS = [
    "logged_at", "bar_time", "total", "macro", "yield_pts", "dxy_pts",
    "silver_pts", "vwap_pts", "signal", "blocked_by", "action", "yield",
    "yield_delta_bp", "yield_z1h", "yield_stale", "gs_corr", "gold_break",
    "silver_break", "usd_break", "usd_roc_pct", "divergence", "close", "vwap",
    "atr", "news", "open_positions",
]


def _parse_hhmm(value: str) -> dtime:
    hh, mm = value.split(":")
    return dtime(int(hh), int(mm))


def _fmt(x: float, spec: str = ".2f") -> str:
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else format(x, spec)


class BotOrchestrator:
    def __init__(self, cfg: AppConfig, connector, macro: MacroDataEngine,
                 news: NewsFilterEngine, risk: RiskManager, state: StateStore,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.cfg = cfg
        self.connector = connector
        self.macro = macro
        self.news = news
        self.risk = risk
        self.state = state
        self._clock = clock
        self._stop = threading.Event()

        self.gold: Optional[str] = None
        self.correlation: Optional[CorrelationEngine] = None
        self.technical: Optional[TechnicalEngine] = None
        self._last_bar: Optional[datetime] = None
        self._prev_tickets: Optional[set] = None
        self._market_closed_logged = False

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
        self.news.refresh(force=False)
        log.info("Trading %s | silver %s | USD proxy %s | dry_run=%s",
                 self.gold, silver, dxy or f"1/{eurusd}", self.connector.dry_run)

    def run(self) -> None:
        self.setup()
        self.macro.start()
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
        self.connector.shutdown()
        log.info("Bot stopped. Open positions keep their broker-side SL/TP.")

    # ----------------------------------------------------------------- #
    # One loop iteration
    # ----------------------------------------------------------------- #
    def step(self) -> None:
        if not self.connector.ensure_connected():
            log.error("MT5 unavailable; will retry next loop")
            return
        acc = self.connector.account()
        if acc is None:
            return

        magic = self.cfg.execution.magic
        positions = self.connector.positions(self.gold, magic)
        self._track_exits(positions)

        guard = self.risk.update_daily(acc)
        if guard.halted:
            self._flatten(guard.reason)
            return

        self.news.refresh()
        news_state = self.news.state()
        if self.cfg.news.manage_open_positions and positions:
            self._manage_news(positions, news_state)

        if not self.connector.market_is_live(self.gold):
            if not self._market_closed_logged:
                log.info("No fresh ticks on %s (market closed or feed paused)", self.gold)
                self._market_closed_logged = True
            return
        self._market_closed_logged = False

        tech = self.technical.evaluate()
        if not tech.ok or tech.bar_time == self._last_bar:
            return
        self._last_bar = tech.bar_time

        ys = self.macro.snapshot()
        cs = self.correlation.evaluate()
        sig = build_signal(ys, cs, tech, self.cfg.strategy)
        positions = self.connector.positions(self.gold, magic)
        action = self._decide_and_execute(sig, tech, news_state, positions, acc)
        self._report(sig, ys, cs, tech, news_state, positions, action, guard.daily_loss_pct)

    # ----------------------------------------------------------------- #
    # Entry gates and execution
    # ----------------------------------------------------------------- #
    def _entry_gates(self, news_state: NewsState, positions: List[PositionInfo]) -> List[str]:
        e = self.cfg.execution
        now = self._clock()
        gates = []
        if news_state.blackout:
            gates.append(f"news blackout: {news_state.reason}")
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
                            news_state: NewsState, positions: List[PositionInfo],
                            acc) -> str:
        if sig.direction is None:
            return "blocked: " + "; ".join(sig.blocked_by) if sig.blocked_by else "no signal"

        gates = self._entry_gates(news_state, positions)
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
            return ("DRY-RUN " if result.dry_run else "OPENED ") + desc
        log.error("ORDER FAILED %s -> retcode %s %s", desc, result.retcode, result.message)
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
            self.state.set("last_exit_time", self._clock().isoformat())
        self._prev_tickets = tickets

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

    def _manage_news(self, positions: List[PositionInfo], news_state: NewsState) -> None:
        if not news_state.imminent:
            return
        ncfg = self.cfg.news
        now = self._clock()
        handled = self.state.get("news_handled", {})
        window = timedelta(minutes=ncfg.minutes_before + ncfg.minutes_after)
        event = news_state.imminent[0]

        for pos in positions:
            key = str(pos.ticket)
            if key in handled and now - datetime.fromisoformat(handled[key]) < window:
                continue  # already protected for this news cluster
            spec = self.connector.symbol_spec(pos.symbol)
            tick = self.connector.get_tick(pos.symbol)
            if spec is None or tick is None:
                continue
            action = self._protect_position(pos, spec, tick, event.title)
            if action:
                handled[key] = now.isoformat()

        # Drop entries for positions that no longer exist.
        live = {str(p.ticket) for p in positions}
        self.state.set("news_handled", {k: v for k, v in handled.items() if k in live})

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
            log.info("News '%s': #%s already protected at breakeven or better",
                     title, pos.ticket)
            return True
        if in_profit and placeable:
            res = self.connector.modify_sltp(pos, be, pos.tp)
            log.warning("News '%s': #%s SL -> breakeven %.2f (%s)", title, pos.ticket, be,
                        "ok" if res.ok else res.message)
            return res.ok

        # Losing (or breakeven cannot be placed yet): cut exposure instead.
        part = round_volume_down(pos.volume * ncfg.partial_close_fraction, spec)
        if part >= spec.volume_min and pos.volume - part >= spec.volume_min - 1e-9:
            res = self.connector.close_position(pos, part)
            log.warning("News '%s': #%s closed %.2f of %.2f lots (%s)", title,
                        pos.ticket, part, pos.volume, "ok" if res.ok else res.message)
            return res.ok
        if ncfg.close_fully_if_unsplittable:
            res = self.connector.close_position(pos)
            log.warning("News '%s': #%s volume %.2f cannot be split - closed fully (%s)",
                        title, pos.ticket, pos.volume, "ok" if res.ok else res.message)
            return res.ok
        log.warning("News '%s': #%s cannot be split; left unchanged", title, pos.ticket)
        return True

    # ----------------------------------------------------------------- #
    # Reporting
    # ----------------------------------------------------------------- #
    def _report(self, sig: Signal, ys: YieldSnapshot, cs: CorrelationSnapshot,
                tech: TechnicalSnapshot, ns: NewsState, positions, action: str,
                daily_loss: float) -> None:
        news_txt = f"BLACKOUT {ns.reason}" if ns.blackout else "clear"
        log.info(
            "BAR %s | score %+d (Y%+d D%+d S%+d V%+d) | 10Y %s d15 %sbp z %s | "
            "R %s | close %s VWAP %s ATR %s | news %s | day P/L %s | pos %d | %s",
            f"{tech.bar_time:%H:%M}Z", sig.total, sig.yield_score, sig.dxy_score,
            sig.silver_score, sig.vwap_score, _fmt(ys.value, ".3f"),
            _fmt(ys.delta_bp, "+.1f"), _fmt(ys.zscore_1h, "+.1f"),
            _fmt(cs.correlation), _fmt(tech.close), _fmt(tech.vwap), _fmt(tech.atr),
            news_txt, f"{-daily_loss:+.2%}", len(positions), action,
        )
        if sig.reasons:
            log.debug("Signal notes: %s", "; ".join(sig.reasons))
        self._journal({
            "logged_at": self._clock().isoformat(),
            "bar_time": tech.bar_time.isoformat() if tech.bar_time else "",
            "total": sig.total, "macro": sig.macro, "yield_pts": sig.yield_score,
            "dxy_pts": sig.dxy_score, "silver_pts": sig.silver_score,
            "vwap_pts": sig.vwap_score, "signal": sig.direction or "",
            "blocked_by": "; ".join(sig.blocked_by), "action": action,
            "yield": ys.value, "yield_delta_bp": ys.delta_bp, "yield_z1h": ys.zscore_1h,
            "yield_stale": ys.stale, "gs_corr": cs.correlation,
            "gold_break": cs.gold_break, "silver_break": cs.silver_break,
            "usd_break": cs.usd_break, "usd_roc_pct": cs.usd_roc_pct,
            "divergence": cs.divergence, "close": tech.close, "vwap": tech.vwap,
            "atr": tech.atr, "news": news_txt, "open_positions": len(positions),
        })

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
