from dataclasses import replace
from datetime import datetime, timedelta, timezone

from goldbot.config import RiskConfig
from goldbot.mt5_connector import BUY, SELL, Tick
from goldbot.risk_manager import RiskManager, round_volume_down
from goldbot.state import StateStore
from tests.conftest import GOLD_SPEC, FakeConnector

NOW = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
TICK = Tick(bid=2600.00, ask=2600.30, time_msc=1)


def manager(tmp_path, account, now=NOW, **risk):
    conn = FakeConnector({}, TICK, account)
    clock = {"now": now}
    rm = RiskManager(RiskConfig(**risk), conn, StateStore(str(tmp_path / "s.json")),
                     clock=lambda: clock["now"])
    return rm, clock


def test_lot_size_risks_one_percent(tmp_path, account):
    rm, _ = manager(tmp_path, account)
    plan, why = rm.plan_trade(BUY, TICK, atr_value=4.0, acc=account, spec=GOLD_SPEC)
    assert why == "ok"
    # SL = 1.5 * 4 = 6.00 below ask; 1 lot loses 600 USD; 1 % of 10k = 100 -> 0.16 lots
    assert plan.sl == 2594.30 and plan.tp == 2612.30
    assert plan.volume == 0.16
    assert plan.risk_amount <= 100.0


def test_sell_levels_mirror(tmp_path, account):
    rm, _ = manager(tmp_path, account)
    plan, _ = rm.plan_trade(SELL, TICK, 4.0, account, GOLD_SPEC)
    assert plan.entry == 2600.00 and plan.sl == 2606.00 and plan.tp == 2588.00


def test_never_rounds_risk_up(tmp_path, account):
    small = replace(account, equity=500.0, balance=500.0)
    rm, _ = manager(tmp_path, small)
    plan, why = rm.plan_trade(BUY, TICK, 4.0, small, GOLD_SPEC)
    assert plan is None and "below broker minimum" in why


def test_spread_filter(tmp_path, account):
    rm, _ = manager(tmp_path, account, max_spread_points=20)
    plan, why = rm.plan_trade(BUY, TICK, 4.0, account, GOLD_SPEC)
    assert plan is None and "spread" in why


def test_round_volume_down():
    assert round_volume_down(0.1699, GOLD_SPEC) == 0.16
    assert round_volume_down(0.17, GOLD_SPEC) == 0.17


def test_daily_guardrail_triggers_persists_and_resets(tmp_path, account):
    rm, clock = manager(tmp_path, account)
    assert not rm.update_daily(account).halted

    down = replace(account, equity=9_690.0)  # -3.1 %
    status = rm.update_daily(down)
    assert status.halted and status.just_triggered

    # A restart the same day must stay halted, even if equity recovers.
    rm2 = RiskManager(RiskConfig(), rm.connector, StateStore(str(tmp_path / "s.json")),
                      clock=lambda: clock["now"])
    assert rm2.update_daily(account).halted

    # Next UTC day: new starting balance, trading resumes.
    clock["now"] = NOW + timedelta(days=1)
    rm2._clock = lambda: clock["now"]
    status = rm2.update_daily(replace(account, balance=9_690.0, equity=9_690.0))
    assert not status.halted and status.day_start_balance == 9_690.0
