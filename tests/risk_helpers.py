"""Builders for risk-engine tests: a state/intent that passes every check."""

from dataclasses import replace
from datetime import datetime, timezone

from goldbot.risk.rules import load_limits
from goldbot.risk.types import OrderIntent, PortfolioState, SymbolInfo

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)      # Wed 15:00 server / Beirut


def gold(stops=10, freeze=5, **kw) -> SymbolInfo:
    base = dict(name="XAUUSD", canonical="XAUUSD", point=0.01, digits=2, tick_size=0.01,
                tick_value=1.0, contract_size=100.0, volume_min=0.01, volume_max=100.0,
                volume_step=0.01, stops_level=stops, freeze_level=freeze)
    base.update(kw)
    return SymbolInfo(**base)


def good_state(**over) -> PortfolioState:
    base = dict(
        now_utc=NOW, server_utc_offset_s=3 * 3600, equity=10_000.0,
        day_start_equity=10_000.0, week_start_equity=10_000.0, open_positions=[],
        trades_today=0, losing_trades_today=0, consecutive_losses=0,
        risk_halved_by_streak=False, day_stopped=False, ladder_level=0, risk_cap_pct=None,
        broker_connected=True, clock_drift_ms=50.0, kill_flag=False, halted=False,
        halt_reason="", mode="paper", orders_enabled=True, account_login=123,
        expected_login=123, account_is_demo=True, live_gate_ok=False,
        news_blackout=False, news_reason="", calendar_fresh=True, spread_points=30.0,
        spread_hour_median_points=20.0, margin_used=0.0, margin_per_lot=4000.0,
        independent_mid=4000.15, atr_1m=0.8, recent_order_sizes=[], symbol=gold())
    base.update(over)
    return PortfolioState(**base)


def good_intent(**over) -> OrderIntent:
    base = dict(intent_id="i-1", strategy="gold_macro", symbol="XAUUSD", side="BUY",
                entry=4000.30, sl=3988.30, tp=4024.30, bid=4000.00, ask=4000.30)
    base.update(over)
    return OrderIntent(**base)


def limits(**tight):
    from goldbot.risk.rules import tighten
    return tighten(load_limits(), tight) if tight else load_limits()


__all__ = ["good_state", "good_intent", "gold", "limits", "NOW", "replace"]
