"""Sizing contract (CLAUDE.md section 1) with the course answer-key fixtures."""

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from goldbot.risk.sizing import (Sized, Skip, actual_risk_usd, effective_leverage,
                                 floor_to_step, overshoot_ok, risk_distance,
                                 size_position)

EQUITY, RISK = 10_000, 100          # $10,000 at 1% -> $100

# name: (entry, sl, tick_size, tick_value, vol_min, vol_step, expected lots)
FIXTURES = {
    "EURUSD 25 pips": (1.10000, 1.09750, 0.00001, 1.0, 0.01, 0.01, "0.40"),
    "USDJPY @150, 30 pips": (150.00, 149.70, 0.001, 0.6666666667, 0.01, 0.01, "0.50"),
    "EURGBP 20 pips @ GBPUSD 1.30": (0.85000, 0.84800, 0.00001, 1.30, 0.01, 0.01, "0.38"),
    "XAUUSD $12": (4000.00, 3988.00, 0.01, 1.0, 0.01, 0.01, "0.08"),
    "XAUUSD $7.50": (4000.00, 3992.50, 0.01, 1.0, 0.01, 0.01, "0.13"),
    "MES 15 pts": (5000.00, 4985.00, 0.25, 1.25, 1, 1, "1"),
}


@pytest.mark.parametrize("name", FIXTURES)
def test_course_fixtures(name):
    entry, sl, tick_size, tick_value, vmin, vstep, expected = FIXTURES[name]
    out = size_position(risk_usd=RISK, entry=entry, sl=sl, tick_size=tick_size,
                        tick_value=tick_value, volume_min=vmin, volume_step=vstep)
    assert isinstance(out, Sized), out
    assert out.lots == Decimal(expected)


def test_mgc_12_dollars_is_a_skip():
    out = size_position(risk_usd=RISK, entry=4000.0, sl=3988.0, tick_size=0.10,
                        tick_value=1.0, volume_min=1, volume_step=1)
    assert isinstance(out, Skip) and "min lot" in out.reason


def test_xauusd_4_dollars_sizes_but_breaks_the_leverage_cap():
    out = size_position(risk_usd=RISK, entry=4000.0, sl=3996.0, tick_size=0.01,
                        tick_value=1.0, volume_min=0.01, volume_step=0.01)
    assert isinstance(out, Sized) and out.lots == Decimal("0.25")
    lev = effective_leverage(notional=out.lots * 100 * Decimal("4000"), equity=EQUITY)
    assert lev == Decimal("10") and lev > 5            # engine skips it (max 5x)


def test_floor_to_step_tolerance_and_never_up():
    assert floor_to_step(0.4999999998, 0.01) == Decimal("0.50")    # not 0.49
    assert floor_to_step(0.0833, 0.01) == Decimal("0.08")
    assert floor_to_step(0.099, 0.01) == Decimal("0.09")
    assert floor_to_step(1.99, 1) == Decimal("1")
    assert floor_to_step(0.12999, 0.01) == Decimal("0.12")
    with pytest.raises(ValueError):
        floor_to_step(1, 0)


def test_risk_distance_includes_spread():
    assert risk_distance("BUY", bid=2999.8, ask=3000.0, sl=2995.0) == Decimal("5.0")
    assert risk_distance("SELL", bid=2999.8, ask=3000.0, sl=3005.0) == Decimal("5.2")
    with pytest.raises(ValueError):
        risk_distance("HOLD", 1, 1, 1)


def test_volume_max_caps_the_size():
    out = size_position(risk_usd=RISK, entry=1.1, sl=1.0999, tick_size=0.00001,
                        tick_value=1.0, volume_min=0.01, volume_step=0.01, volume_max=5.0)
    assert out.lots == Decimal("5.00")


def test_actual_risk_and_overshoot_guard():
    actual = actual_risk_usd(fill=3999.0, sl=3988.0, tick_size=0.01, tick_value=1.0, lots=0.08)
    assert actual == Decimal("88.00")
    assert overshoot_ok(actual=Decimal("109"), planned=Decimal("100"))
    assert overshoot_ok(actual=Decimal("110"), planned=Decimal("100"))
    assert not overshoot_ok(actual=Decimal("110.01"), planned=Decimal("100"))


@settings(max_examples=300, deadline=None)
@given(risk=st.floats(1, 5000), dist=st.floats(0.05, 500),
       tick=st.sampled_from([0.01, 0.001, 0.00001, 0.25]),
       tick_value=st.floats(0.1, 20), step=st.sampled_from([0.01, 0.1, 1.0]))
def test_property_risk_never_exceeds_budget_and_never_rounds_up(risk, dist, tick, tick_value, step):
    out = size_position(risk_usd=risk, entry=1000.0 + dist, sl=1000.0, tick_size=tick,
                        tick_value=tick_value, volume_min=step, volume_step=step)
    if isinstance(out, Sized):
        per_lot = Decimal(str(dist)) / Decimal(str(tick)) * Decimal(str(tick_value))
        assert out.lots * per_lot <= Decimal(str(risk)) * Decimal("1.0000001")
        assert out.lots >= Decimal(str(step))
        assert (out.lots / Decimal(str(step))) % 1 == 0     # on the lot grid
