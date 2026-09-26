"""Shared fixtures: synthetic bars and an in-memory fake of MT5Connector."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goldbot.mt5_connector import (BUY, AccountSnapshot, OrderResult,  # noqa: E402
                                   PositionInfo, SymbolSpec, Tick)


def make_bars(closes, start: datetime, minutes: int = 5, spread: float = 0.5,
              volume: float = 100.0) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    return pd.DataFrame({
        "time": pd.date_range(start, periods=len(closes), freq=f"{minutes}min", tz="UTC"),
        "open": opens,
        "high": np.maximum(opens, closes) + spread,
        "low": np.minimum(opens, closes) - spread,
        "close": closes,
        "volume": np.full(len(closes), volume),
    })


GOLD_SPEC = SymbolSpec(name="XAUUSD", point=0.01, digits=2, tick_size=0.01,
                       tick_value=1.0, volume_min=0.01, volume_max=100.0,
                       volume_step=0.01, stops_level=0, filling_type=1)


class FakeConnector:
    """Duck-typed stand-in for MT5Connector used by orchestrator tests."""

    def __init__(self, rates: Dict[tuple, pd.DataFrame], tick: Tick,
                 account: AccountSnapshot, dry_run: bool = True):
        self.rates = rates
        self.tick = tick
        self.acc = account
        self.dry_run = dry_run
        self.utc_offset_known = True
        self.live = True
        self.open_positions: List[PositionInfo] = []
        self.sent: List[tuple] = []

    def connect(self): return True
    def ensure_connected(self): return True
    def shutdown(self): pass
    def resolve_symbol(self, base): return base or None
    def symbol_spec(self, symbol): return GOLD_SPEC
    def get_tick(self, symbol): return self.tick
    def market_is_live(self, symbol): return self.live
    def account(self): return self.acc

    def get_rates(self, symbol, timeframe, count) -> Optional[pd.DataFrame]:
        df = self.rates.get((symbol, timeframe))
        return None if df is None else df.tail(count).reset_index(drop=True)

    def positions(self, symbol=None, magic=None):
        return [p for p in self.open_positions
                if (symbol is None or p.symbol == symbol)
                and (magic is None or p.magic == magic)]

    def calc_loss_per_lot(self, symbol, side, entry, sl):
        return abs(entry - sl) * 100.0  # 100 oz contract, USD account

    def calc_margin(self, symbol, side, volume, price):
        return volume * price * 100 / 100  # 1:100 leverage

    def open_market(self, symbol, side, volume, sl, tp, comment):
        self.sent.append(("open", side, volume, sl, tp))
        return OrderResult(ok=True, dry_run=self.dry_run)

    def modify_sltp(self, pos, sl, tp):
        self.sent.append(("modify", pos.ticket, sl, tp))
        return OrderResult(ok=True)

    deals: list = []

    def deal_history(self, since):
        return [d for d in self.deals if d.time >= since]

    def position_deals(self, position_id):
        return [d for d in self.deals if d.position_id == position_id]

    def close_position(self, pos, volume=None):
        self.sent.append(("close", pos.ticket, volume or pos.volume))
        return OrderResult(ok=True)


@pytest.fixture
def account():
    return AccountSnapshot(login=1, server="Demo", currency="USD", balance=10_000.0,
                           equity=10_000.0, margin_free=10_000.0, is_demo=True,
                           hedging=True)


@pytest.fixture
def t0():
    return datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc)  # a Wednesday


def long_position(ticket=1, price_open=2600.0, sl=2590.0, tp=2620.0, volume=0.10):
    return PositionInfo(ticket=ticket, symbol="XAUUSD", side=BUY, volume=volume,
                        price_open=price_open, sl=sl, tp=tp, profit=0.0,
                        magic=20260926)


__all__ = ["make_bars", "FakeConnector", "GOLD_SPEC", "long_position", "timedelta"]
