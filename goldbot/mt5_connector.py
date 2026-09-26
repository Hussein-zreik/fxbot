"""MetaTrader 5 gateway: connection, market data, account state and orders.

This is the only module that imports ``MetaTrader5``. Everything else talks to
MT5 through the small, typed interface below, which keeps the strategy code
testable on machines without the (Windows-only) terminal.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import pandas as pd

from .config import ExecutionConfig, MT5Config

try:  # The MetaTrader5 wheel only exists for Windows.
    import MetaTrader5 as mt5
except ImportError:  # pragma: no cover - exercised only off-Windows
    mt5 = None

log = logging.getLogger(__name__)

BUY, SELL = "BUY", "SELL"

# Raw MT5 values (stable across terminal builds; some are not exported
# as named constants by the Python package).
_SYMBOL_FILLING_FOK = 1
_SYMBOL_FILLING_IOC = 2
_ACCOUNT_TRADE_MODE_DEMO = 0
_ACCOUNT_MARGIN_MODE_HEDGING = 2
_RETCODE_PLACED = 10008
_RETCODE_DONE = 10009
_RETCODE_DONE_PARTIAL = 10010
_RETRYABLE_RETCODES = {10004, 10020, 10021}  # requote, price changed, off quotes


@dataclass
class SymbolSpec:
    name: str
    point: float
    digits: int
    tick_size: float
    tick_value: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level: int
    filling_type: int


@dataclass
class Tick:
    bid: float
    ask: float
    time_msc: int


@dataclass
class AccountSnapshot:
    login: int
    server: str
    currency: str
    balance: float
    equity: float
    margin_free: float
    is_demo: bool
    hedging: bool


@dataclass
class PositionInfo:
    ticket: int
    symbol: str
    side: str
    volume: float
    price_open: float
    sl: float
    tp: float
    profit: float
    magic: int


@dataclass
class DealInfo:
    """One executed deal from the account history (times in UTC)."""
    ticket: int
    position_id: int
    time: datetime
    side: str            # BUY / SELL (the deal's own direction)
    entry: str           # IN / OUT / INOUT / OUT_BY
    volume: float
    price: float
    profit: float        # net: profit + commission + swap + fee
    reason: str          # SL / TP / STOP_OUT / BOT / MANUAL
    magic: int
    symbol: str
    comment: str


_DEAL_ENTRY = {0: "IN", 1: "OUT", 2: "INOUT", 3: "OUT_BY"}
_DEAL_REASON = {0: "MANUAL", 1: "MANUAL", 2: "MANUAL", 3: "BOT", 4: "SL", 5: "TP",
                6: "STOP_OUT"}


@dataclass
class OrderResult:
    ok: bool
    retcode: int = 0
    ticket: int = 0
    price: float = 0.0
    message: str = ""
    dry_run: bool = False


class MT5Connector:
    """Owns the MT5 session and every call that reaches the terminal."""

    def __init__(self, cfg: MT5Config, exec_cfg: ExecutionConfig):
        if mt5 is None:
            raise RuntimeError(
                "The MetaTrader5 package is not installed. It only runs on "
                "Windows next to an MT5 desktop terminal: pip install MetaTrader5"
            )
        self.cfg = cfg
        self.exec_cfg = exec_cfg
        self.dry_run = exec_cfg.dry_run
        self._connected = False
        self._spec_cache: Dict[str, SymbolSpec] = {}
        self._last_tick_msc: Dict[str, int] = {}
        self._last_tick_change: Dict[str, float] = {}
        self._utc_offset_s: Optional[int] = (
            int(exec_cfg.server_utc_offset_hours * 3600)
            if exec_cfg.server_utc_offset_hours is not None else None
        )
        self._timeframes = {
            "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5,
            "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30,
            "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
            "D1": mt5.TIMEFRAME_D1,
        }

    # ------------------------------------------------------------------ #
    # Connection lifecycle
    # ------------------------------------------------------------------ #
    def connect(self) -> bool:
        """Initialize the terminal session, retrying with exponential backoff."""
        kwargs = {"timeout": self.cfg.timeout_ms}
        if self.cfg.login:
            kwargs.update(login=int(self.cfg.login), password=self.cfg.password,
                          server=self.cfg.server)

        for attempt in range(1, self.cfg.reconnect_attempts + 1):
            try:
                ok = (mt5.initialize(self.cfg.terminal_path, **kwargs)
                      if self.cfg.terminal_path else mt5.initialize(**kwargs))
            except Exception as exc:  # noqa: BLE001 - terminal IPC can raise anything
                ok = False
                log.error("mt5.initialize raised: %s", exc)
            if ok:
                self._connected = True
                self._apply_account_safety()
                return True
            delay = self.cfg.reconnect_base_delay_s * 2 ** (attempt - 1)
            log.warning("MT5 init failed (attempt %d/%d): %s - retry in %.0fs",
                        attempt, self.cfg.reconnect_attempts, mt5.last_error(), delay)
            mt5.shutdown()
            time.sleep(delay)
        self._connected = False
        return False

    def _apply_account_safety(self) -> None:
        acc = self.account()
        if acc is None:
            return
        mode = "DEMO" if acc.is_demo else "LIVE"
        log.info("Connected: login=%s server=%s currency=%s balance=%.2f "
                 "account=%s margin_mode=%s", acc.login, acc.server, acc.currency,
                 acc.balance, mode, "hedging" if acc.hedging else "netting")
        if not acc.is_demo and not self.exec_cfg.allow_live_trading:
            log.critical("LIVE account detected but execution.allow_live_trading "
                         "is false - forcing DRY-RUN. No orders will be sent.")
            self.dry_run = True
        if not acc.hedging and self.exec_cfg.max_open_positions > 1:
            log.warning("Netting account: extra orders would merge into one "
                        "position - limiting max_open_positions to 1.")
            self.exec_cfg.max_open_positions = 1
        if self.dry_run:
            log.warning("DRY-RUN mode: signals and orders are logged only.")

    def ensure_connected(self) -> bool:
        """Health check; reconnect if the terminal or broker link dropped."""
        try:
            info = mt5.terminal_info()
            if info is not None and info.connected:
                if not info.trade_allowed and not self.dry_run:
                    log.warning("'Algo Trading' is disabled in the MT5 terminal; "
                                "orders will be rejected until it is enabled.")
                self._connected = True
                return True
        except Exception as exc:  # noqa: BLE001
            log.error("terminal_info failed: %s", exc)
        log.warning("MT5 connection lost - reconnecting")
        mt5.shutdown()
        self._spec_cache.clear()
        return self.connect()

    def shutdown(self) -> None:
        try:
            mt5.shutdown()
        finally:
            self._connected = False

    # ------------------------------------------------------------------ #
    # Symbols and market data
    # ------------------------------------------------------------------ #
    def resolve_symbol(self, base: str) -> Optional[str]:
        """Map a base name to the broker's name (XAUUSD -> XAUUSD.a/XAUUSDm)."""
        if not base:
            return None
        info = mt5.symbol_info(base)
        name = base if info is not None else None
        if name is None:
            candidates = [s.name for s in (mt5.symbols_get() or [])
                          if base.upper() in s.name.upper()]
            # Prefer names that start with the base, then the shortest one.
            candidates.sort(key=lambda n: (not n.upper().startswith(base.upper()),
                                           len(n)))
            name = candidates[0] if candidates else None
        if name is None:
            log.error("Symbol %s not found at this broker", base)
            return None
        if not mt5.symbol_select(name, True):
            log.error("Could not add %s to Market Watch: %s", name, mt5.last_error())
            return None
        if name != base:
            log.info("Resolved symbol %s -> %s", base, name)
        return name

    def symbol_spec(self, symbol: str) -> Optional[SymbolSpec]:
        if symbol in self._spec_cache:
            return self._spec_cache[symbol]
        info = mt5.symbol_info(symbol)
        if info is None:
            log.error("symbol_info(%s) failed: %s", symbol, mt5.last_error())
            return None
        if info.filling_mode & _SYMBOL_FILLING_FOK:
            filling = mt5.ORDER_FILLING_FOK
        elif info.filling_mode & _SYMBOL_FILLING_IOC:
            filling = mt5.ORDER_FILLING_IOC
        else:
            filling = mt5.ORDER_FILLING_RETURN
        spec = SymbolSpec(
            name=symbol, point=info.point, digits=info.digits,
            tick_size=info.trade_tick_size or info.point,
            tick_value=info.trade_tick_value,
            volume_min=info.volume_min, volume_max=info.volume_max,
            volume_step=info.volume_step, stops_level=info.trade_stops_level,
            filling_type=filling,
        )
        self._spec_cache[symbol] = spec
        return spec

    def get_tick(self, symbol: str) -> Optional[Tick]:
        tick = mt5.symbol_info_tick(symbol)
        if tick is None or tick.bid <= 0 or tick.ask <= 0:
            return None
        now = time.time()
        prev = self._last_tick_msc.get(symbol)
        if prev != tick.time_msc:
            self._last_tick_msc[symbol] = tick.time_msc
            # Only a tick that changed while we were watching is known to be
            # fresh; the first one seen may be hours old (e.g. weekend close).
            if prev is not None:
                self._last_tick_change[symbol] = now
                if self._utc_offset_s is None:
                    self._detect_utc_offset(tick.time, now)
        return Tick(bid=tick.bid, ask=tick.ask, time_msc=tick.time_msc)

    def _detect_utc_offset(self, server_ts: int, now: float) -> None:
        """Server timestamps are broker-local; derive the offset from a fresh tick."""
        offset = int(round((server_ts - now) / 1800.0) * 1800)
        if abs(offset) <= 14 * 3600:
            self._utc_offset_s = offset
            log.info("Detected broker server time offset: UTC%+.1fh", offset / 3600)

    def market_is_live(self, symbol: str) -> bool:
        """True if the symbol ticked within ``market_stale_seconds``."""
        self.get_tick(symbol)
        last = self._last_tick_change.get(symbol)
        return last is not None and time.time() - last <= self.exec_cfg.market_stale_seconds

    @property
    def utc_offset_seconds(self) -> int:
        return self._utc_offset_s or 0

    @property
    def utc_offset_known(self) -> bool:
        return self._utc_offset_s is not None

    def get_rates(self, symbol: str, timeframe: str, count: int) -> Optional[pd.DataFrame]:
        """Last ``count`` CLOSED bars as a DataFrame with UTC timestamps."""
        tf = self._timeframes.get(timeframe)
        if tf is None:
            raise ValueError(f"Unsupported timeframe {timeframe}")
        try:
            rates = mt5.copy_rates_from_pos(symbol, tf, 1, count)  # pos 1 = skip forming bar
        except Exception as exc:  # noqa: BLE001
            log.error("copy_rates_from_pos(%s) raised: %s", symbol, exc)
            return None
        if rates is None or len(rates) == 0:
            log.warning("No %s bars for %s: %s", timeframe, symbol, mt5.last_error())
            return None
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"] - self.utc_offset_seconds,
                                    unit="s", utc=True)
        df["volume"] = (df["real_volume"] if df["real_volume"].sum() > 0
                        else df["tick_volume"]).astype(float)
        return df[["time", "open", "high", "low", "close", "volume"]]

    # ------------------------------------------------------------------ #
    # Account and positions
    # ------------------------------------------------------------------ #
    def account(self) -> Optional[AccountSnapshot]:
        info = mt5.account_info()
        if info is None:
            log.error("account_info failed: %s", mt5.last_error())
            return None
        return AccountSnapshot(
            login=info.login, server=info.server, currency=info.currency,
            balance=info.balance, equity=info.equity, margin_free=info.margin_free,
            is_demo=info.trade_mode == _ACCOUNT_TRADE_MODE_DEMO,
            hedging=info.margin_mode == _ACCOUNT_MARGIN_MODE_HEDGING,
        )

    def positions(self, symbol: Optional[str] = None,
                  magic: Optional[int] = None) -> List[PositionInfo]:
        raw = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        if raw is None:
            return []
        out = []
        for p in raw:
            if magic is not None and p.magic != magic:
                continue
            out.append(PositionInfo(
                ticket=p.ticket, symbol=p.symbol,
                side=BUY if p.type == mt5.POSITION_TYPE_BUY else SELL,
                volume=p.volume, price_open=p.price_open, sl=p.sl, tp=p.tp,
                profit=p.profit, magic=p.magic,
            ))
        return out

    def calc_loss_per_lot(self, symbol: str, side: str, entry: float,
                          sl: float) -> Optional[float]:
        """Account-currency loss of 1.0 lot from entry to SL (uses broker math)."""
        order_type = mt5.ORDER_TYPE_BUY if side == BUY else mt5.ORDER_TYPE_SELL
        try:
            pnl = mt5.order_calc_profit(order_type, symbol, 1.0, entry, sl)
        except Exception as exc:  # noqa: BLE001
            log.warning("order_calc_profit raised: %s", exc)
            pnl = None
        return abs(pnl) if pnl else None

    def calc_margin(self, symbol: str, side: str, volume: float,
                    price: float) -> Optional[float]:
        order_type = mt5.ORDER_TYPE_BUY if side == BUY else mt5.ORDER_TYPE_SELL
        try:
            return mt5.order_calc_margin(order_type, symbol, volume, price)
        except Exception as exc:  # noqa: BLE001
            log.warning("order_calc_margin raised: %s", exc)
            return None

    # ------------------------------------------------------------------ #
    # Account history
    # ------------------------------------------------------------------ #
    def _to_deals(self, raw) -> List[DealInfo]:
        out = []
        for d in raw or []:
            if d.type not in (mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL):
                continue  # balance, credit, commission rows etc.
            out.append(DealInfo(
                ticket=d.ticket, position_id=d.position_id,
                time=datetime.fromtimestamp(d.time - self.utc_offset_seconds, timezone.utc),
                side=BUY if d.type == mt5.DEAL_TYPE_BUY else SELL,
                entry=_DEAL_ENTRY.get(d.entry, str(d.entry)),
                volume=d.volume, price=d.price,
                profit=d.profit + d.commission + d.swap + getattr(d, "fee", 0.0),
                reason=_DEAL_REASON.get(d.reason, "OTHER"),
                magic=d.magic, symbol=d.symbol, comment=d.comment,
            ))
        return out

    def deal_history(self, since: datetime) -> List[DealInfo]:
        """All trade deals since ``since`` (UTC); padded for broker time zones."""
        start = since - timedelta(days=1)
        end = datetime.now(timezone.utc) + timedelta(days=2)
        try:
            raw = mt5.history_deals_get(start, end)
        except Exception as exc:  # noqa: BLE001
            log.warning("history_deals_get failed: %s", exc)
            return []
        return [d for d in self._to_deals(raw) if d.time >= since]

    def position_deals(self, position_id: int) -> List[DealInfo]:
        try:
            raw = mt5.history_deals_get(position=position_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("history_deals_get(position) failed: %s", exc)
            return []
        return self._to_deals(raw)

    # ------------------------------------------------------------------ #
    # Trading (every method honours dry-run)
    # ------------------------------------------------------------------ #
    def _send(self, request: dict, symbol: str, side: Optional[str]) -> OrderResult:
        if self.dry_run:
            log.info("[DRY-RUN] order_send %s", request)
            return OrderResult(ok=True, message="dry-run", dry_run=True)

        last = OrderResult(ok=False, message="not sent")
        for attempt in range(1, self.exec_cfg.order_retries + 1):
            if side is not None:  # refresh market price for DEAL requests
                tick = self.get_tick(symbol)
                if tick is None:
                    return OrderResult(ok=False, message="no tick")
                request["price"] = tick.ask if side == BUY else tick.bid
            try:
                res = mt5.order_send(request)
            except Exception as exc:  # noqa: BLE001
                log.error("order_send raised: %s", exc)
                res = None
            if res is None:
                last = OrderResult(ok=False, message=str(mt5.last_error()))
            else:
                last = OrderResult(
                    ok=res.retcode in (_RETCODE_DONE, _RETCODE_PLACED, _RETCODE_DONE_PARTIAL),
                    retcode=res.retcode, ticket=res.order, price=res.price,
                    message=res.comment,
                )
                if last.ok or res.retcode not in _RETRYABLE_RETCODES:
                    break
            log.warning("order_send attempt %d failed: retcode=%s %s",
                        attempt, last.retcode, last.message)
            time.sleep(0.5)
        return last

    def open_market(self, symbol: str, side: str, volume: float, sl: float,
                    tp: float, comment: str) -> OrderResult:
        spec = self.symbol_spec(symbol)
        if spec is None:
            return OrderResult(ok=False, message="no symbol spec")
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(volume),
            "type": mt5.ORDER_TYPE_BUY if side == BUY else mt5.ORDER_TYPE_SELL,
            "price": 0.0,
            "sl": round(sl, spec.digits),
            "tp": round(tp, spec.digits),
            "deviation": self.exec_cfg.deviation_points,
            "magic": self.exec_cfg.magic,
            "comment": comment[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": spec.filling_type,
        }
        return self._send(request, symbol, side)

    def modify_sltp(self, pos: PositionInfo, sl: float, tp: float) -> OrderResult:
        spec = self.symbol_spec(pos.symbol)
        digits = spec.digits if spec else 5
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": pos.symbol,
            "position": pos.ticket,
            "sl": round(sl, digits),
            "tp": round(tp, digits),
            "magic": self.exec_cfg.magic,
        }
        return self._send(request, pos.symbol, None)

    def close_position(self, pos: PositionInfo,
                       volume: Optional[float] = None) -> OrderResult:
        spec = self.symbol_spec(pos.symbol)
        if spec is None:
            return OrderResult(ok=False, message="no symbol spec")
        close_side = SELL if pos.side == BUY else BUY
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "volume": float(volume or pos.volume),
            "type": mt5.ORDER_TYPE_SELL if close_side == SELL else mt5.ORDER_TYPE_BUY,
            "position": pos.ticket,
            "price": 0.0,
            "deviation": self.exec_cfg.deviation_points,
            "magic": self.exec_cfg.magic,
            "comment": "goldbot close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": spec.filling_type,
        }
        return self._send(request, pos.symbol, close_side)

    @staticmethod
    def utc_now() -> datetime:
        return datetime.now(timezone.utc)
