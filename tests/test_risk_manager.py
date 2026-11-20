"""RiskManager only places the stop and target now; sizing/approval live in risk.*."""

from goldbot.config import RiskConfig
from goldbot.mt5_connector import Tick
from goldbot.risk_manager import RiskManager, round_price, round_volume_down
from tests.conftest import GOLD_SPEC

TICK = Tick(bid=2600.00, ask=2600.30, time_msc=1)


def test_buy_levels_use_the_ask_and_atr_multiples():
    levels, why = RiskManager(RiskConfig()).plan_levels("BUY", TICK, 4.0, GOLD_SPEC)
    assert why == "ok"
    assert (levels.entry, levels.sl, levels.tp) == (2600.30, 2594.30, 2612.30)
    assert levels.sl_distance == 6.0                       # spread is inside the risk distance


def test_sell_levels_mirror_with_the_bid():
    levels, _ = RiskManager(RiskConfig()).plan_levels("SELL", TICK, 4.0, GOLD_SPEC)
    assert (levels.entry, levels.sl, levels.tp) == (2600.00, 2606.00, 2588.00)


def test_bad_atr_is_refused():
    for atr in (0, -1, float("nan"), None):
        levels, why = RiskManager(RiskConfig()).plan_levels("BUY", TICK, atr, GOLD_SPEC)
        assert levels is None and why == "invalid ATR"


def test_rounding_helpers():
    assert round_volume_down(0.1699, GOLD_SPEC) == 0.16
    assert round_volume_down(0.4999999998, GOLD_SPEC) == 0.5
    assert round_price(2594.304, GOLD_SPEC) == 2594.30


def test_reward_to_risk_of_the_default_levels_meets_the_rulebook_minimum():
    levels, _ = RiskManager(RiskConfig()).plan_levels("BUY", TICK, 4.0, GOLD_SPEC)
    assert (levels.tp - levels.entry) / levels.sl_distance >= 1.5
