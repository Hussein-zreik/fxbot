"""RiskEngine.approve: one test per pre-trade check (CLAUDE.md section 6)."""

from datetime import datetime, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from goldbot.risk.engine import RiskEngine
from goldbot.risk.types import InvalidIntent, OrderIntent, PositionRisk
from tests.risk_helpers import good_intent, good_state, gold, limits


def decide(state=None, intent=None, **tight):
    return RiskEngine(limits(**tight)).approve(intent or good_intent(), state or good_state())


def test_a_clean_trade_is_approved_with_a_floor_rounded_size():
    d = decide()
    assert d.approved and d.reason_code == "OK"
    assert d.adjusted_size == 0.02                       # $25 / $1200 per lot = 0.0208
    assert d.risk_pct == 0.25 and d.risk_usd == pytest.approx(24.0)
    assert not d.halved and len(d.checks) >= 10


# ---------------------------------------------------------------- intent types
def test_an_intent_without_a_valid_stop_cannot_exist():
    for bad_sl in (None, 0, -5, float("nan"), float("inf")):
        with pytest.raises(InvalidIntent):
            good_intent(sl=bad_sl)
    with pytest.raises(InvalidIntent):
        good_intent(sl=4001.0)                            # BUY stop above entry
    with pytest.raises(InvalidIntent):
        good_intent(side="SELL", entry=4000.0, sl=3990.0, tp=3980.0, bid=4000.0)
    with pytest.raises(InvalidIntent):
        good_intent(tp=3990.0)                            # target on the wrong side
    with pytest.raises(InvalidIntent):
        good_intent(side="HOLD")
    with pytest.raises(InvalidIntent):
        good_intent(entry=4000.0, sl=3988.0, tp=4024.0)    # BUY must enter at the ASK (4000.30)
    with pytest.raises(InvalidIntent):
        good_intent(bid=4001.0)                            # bid above ask
    with pytest.raises(TypeError):
        OrderIntent(intent_id="x", strategy="s", symbol="XAUUSD", side="BUY", entry=1.0,
                    tp=None, bid=1.0, ask=1.0)            # sl argument missing entirely


# ---------------------------------------------------------------- 1 safety state
@pytest.mark.parametrize("over,code", [
    (dict(kill_flag=True), "KILL_FLAG"),
    (dict(halted=True, halt_reason="manual"), "HALTED"),
    (dict(broker_connected=False), "BROKER_DISCONNECTED"),
    (dict(clock_drift_ms=250.0), "CLOCK_DRIFT"),
    (dict(clock_drift_ms=None), "CLOCK_DRIFT"),
])
def test_check1_safety_state(over, code):
    d = decide(good_state(**over))
    assert not d.approved and d.reason_code == code


def test_kill_flag_wins_over_every_other_problem():
    d = decide(good_state(kill_flag=True, broker_connected=False, news_blackout=True,
                          ladder_level=3, spread_points=999))
    assert d.reason_code == "KILL_FLAG"


# ---------------------------------------------------------------- 2 mode / account
@pytest.mark.parametrize("over,code", [
    (dict(mode="yolo"), "MODE_INVALID"),
    (dict(account_login=999), "ACCOUNT_MISMATCH"),
    (dict(mode="paper", account_is_demo=False), "MODE_MISMATCH"),
    (dict(mode="live", account_is_demo=True, live_gate_ok=True), "MODE_MISMATCH"),
    (dict(mode="live", account_is_demo=False, live_gate_ok=False), "LIVE_GATE"),
])
def test_check2_mode_and_account(over, code):
    d = decide(good_state(**over))
    assert not d.approved and d.reason_code == code


def test_live_on_a_real_account_is_refused_while_the_stage_is_still_demo():
    ok = good_state(mode="live", account_is_demo=False, live_gate_ok=True)
    assert decide(ok).reason_code == "STAGE_MISMATCH"


def test_dry_run_skips_the_account_checks_because_nothing_is_sent():
    d = decide(good_state(orders_enabled=False, mode="paper", account_is_demo=False))
    assert d.approved


# ---------------------------------------------------------------- 3 time filters
def test_check3_news_blackout():
    d = decide(good_state(news_blackout=True, news_reason="USD CPI"))
    assert d.reason_code == "NEWS_BLACKOUT" and "CPI" in d.detail


def test_check3_server_time_unknown_fails_closed():
    assert decide(good_state(server_utc_offset_s=None)).reason_code == "SERVER_TIME_UNKNOWN"


@pytest.mark.parametrize("utc,blocked", [
    (datetime(2026, 9, 23, 20, 50, tzinfo=timezone.utc), True),    # 23:50 server
    (datetime(2026, 9, 23, 21, 40, tzinfo=timezone.utc), False),   # 00:40 server: window over
    (datetime(2026, 9, 23, 20, 45, tzinfo=timezone.utc), True),    # 23:45 server: window opens
    (datetime(2026, 9, 23, 21, 30, tzinfo=timezone.utc), True),    # 00:30 server: still inside
    (datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc), False),
    (datetime(2026, 9, 25, 16, 59, tzinfo=timezone.utc), False),   # Fri 19:59 server
    (datetime(2026, 9, 25, 17, 0, tzinfo=timezone.utc), True),     # Fri 20:00 server
    (datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc), True),     # Saturday
])
def test_check3_no_entry_windows_use_server_time(utc, blocked):
    d = decide(good_state(now_utc=utc))
    assert (d.reason_code == "NO_ENTRY_WINDOW") is blocked


def test_check3_stale_calendar_blocks_only_inside_the_beirut_windows():
    inside = datetime(2026, 9, 23, 12, 30, tzinfo=timezone.utc)    # 15:30 Beirut (UTC+3 in Sept)
    outside = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)    # 15:00 Beirut
    assert decide(good_state(now_utc=inside, calendar_fresh=False)).reason_code == \
        "CALENDAR_STALE_WINDOW"
    assert decide(good_state(now_utc=outside, calendar_fresh=False)).approved
    winter_inside = datetime(2026, 1, 14, 13, 30, tzinfo=timezone.utc)   # 15:30 Beirut (UTC+2)
    d = decide(good_state(now_utc=winter_inside, calendar_fresh=False))
    assert d.reason_code == "CALENDAR_STALE_WINDOW"


# ---------------------------------------------------------------- 4 spread
@pytest.mark.parametrize("over,code", [
    (dict(spread_points=41.0), "SPREAD_MEDIAN"),                    # > 2 x 20
    (dict(spread_points=51.0, spread_hour_median_points=40.0), "SPREAD_MAX"),
    (dict(spread_hour_median_points=None), "SPREAD_BASELINE_MISSING"),
])
def test_check4_spread(over, code):
    assert decide(good_state(**over)).reason_code == code


def test_check4_unknown_symbol_has_no_spread_cap_so_it_is_rejected():
    d = decide(good_state(symbol=gold(canonical="EURUSD", name="EURUSD")),
               good_intent(symbol="EURUSD"))
    assert d.reason_code == "SPREAD_LIMIT_UNKNOWN"


# ---------------------------------------------------------------- 5 stops / 6 R:R
def test_check5_stop_distance_and_freeze_level():
    near = good_intent(sl=4000.0 - 0.05, tp=4024.30)               # 0.05 < stops_level 0.10
    assert decide(good_state(), near).reason_code == "STOPS_LEVEL"
    in_freeze = good_intent(sl=4000.0 - 4.0, tp=4010.0)            # $4.00 inside a $5.00 freeze
    d = decide(good_state(symbol=gold(freeze=500)), in_freeze)     # 500 points x 0.01
    assert d.reason_code == "FREEZE_LEVEL"
    assert decide(good_state(), in_freeze).reason_code != "FREEZE_LEVEL"   # 5 points = $0.05


def test_check6_reward_to_risk():
    low = good_intent(tp=4000.30 + 15.0)                           # 15/12 = 1.25 < 1.5
    assert decide(good_state(), low).reason_code == "RR_TOO_LOW"
    assert decide(good_state(), good_intent(tp=None)).reason_code == "RR_NO_TARGET"
    assert decide(good_state(), good_intent(tp=4000.30 + 18.0)).approved    # exactly 1.5


# ---------------------------------------------------------------- 7 per-trade risk
def test_check7_risk_is_halved_by_the_ladder_or_a_losing_streak():
    base = decide(good_state(symbol=gold(volume_min=0.001, volume_step=0.001)))
    assert base.risk_pct == 0.25
    for over in (dict(ladder_level=1), dict(risk_halved_by_streak=True)):
        d = decide(good_state(symbol=gold(volume_min=0.001, volume_step=0.001), **over))
        assert d.approved and d.halved and d.risk_pct == 0.125


def test_check7_post_l2_cap_and_requested_risk():
    d = decide(good_state(risk_cap_pct=0.10, symbol=gold(volume_min=0.001, volume_step=0.001)))
    assert d.risk_pct == 0.10
    assert decide(good_state(), good_intent(requested_risk_pct=0.5)).reason_code == \
        "RISK_OVER_STAGE"
    lower = decide(good_state(symbol=gold(volume_min=0.001, volume_step=0.001)),
                   good_intent(requested_risk_pct=0.10))
    assert lower.approved and lower.risk_pct == 0.10               # may ask for less


def test_check7_size_below_the_minimum_lot_is_skipped_never_rounded_up():
    wide = good_intent(sl=3900.0, tp=4300.0)
    d = decide(good_state(), wide)
    assert d.reason_code == "SIZE_SKIP" and d.adjusted_size is None
    assert decide(good_state(equity=500.0, day_start_equity=500.0,
                             week_start_equity=500.0)).reason_code == "SIZE_SKIP"


# ---------------------------------------------------------------- 8 exposure
def pos(symbol="EURUSD", risk=50.0, notional=10_000.0, ticket=1):
    return PositionRisk(ticket=ticket, symbol=symbol, risk_usd=risk, notional=notional)


def test_check8_position_limits_and_clusters():
    assert decide(good_state(open_positions=[pos("EURUSD", ticket=1), pos("MES", ticket=2)])
                  ).reason_code == "MAX_POSITIONS"
    assert decide(good_state(open_positions=[pos("XAUUSD")])).reason_code == "MAX_POSITIONS"
    assert decide(good_state(open_positions=[pos("XAGUSD")])).reason_code == "CLUSTER_SLOT"
    assert decide(good_state(open_positions=[pos("EURUSD")])).approved


def test_check8_open_risk_and_trade_count_and_unprotected():
    heavy = [pos("EURUSD", risk=190.0)]                            # 1.9% + 0.24% > 2%
    assert decide(good_state(open_positions=heavy)).reason_code == "OPEN_RISK"
    assert decide(good_state(open_positions=[pos("EURUSD", risk=None)])).reason_code == \
        "UNPROTECTED_POSITION"
    assert decide(good_state(trades_today=4)).reason_code == "MAX_TRADES_DAY"
    assert decide(good_state(trades_today=3)).approved


# ---------------------------------------------------------------- 9 leverage / margin
def test_check9_leverage_cap_counts_existing_positions():
    tight = good_intent(sl=4000.30 - 1.5, tp=4000.30 + 3.0)        # $1.50 stop -> ~6.4x
    s = good_state(symbol=gold(freeze=0))
    assert decide(s, tight).reason_code == "LEVERAGE"
    s2 = good_state(open_positions=[pos("EURUSD", risk=10.0, notional=45_000.0)])
    assert decide(s2).reason_code == "LEVERAGE"                    # 4.5x + new 0.8x > 5x


def test_check9_margin_usage():
    assert decide(good_state(margin_used=3000.0)).reason_code == "MARGIN"
    assert decide(good_state(margin_used=2900.0)).approved
    assert decide(good_state(margin_per_lot=None)).reason_code == "MARGIN_UNKNOWN"


# ---------------------------------------------------------------- 10 loss limits
@pytest.mark.parametrize("over,code", [
    (dict(equity=9_790.0), "DAILY_LOSS"),
    (dict(losing_trades_today=3), "DAILY_LOSING_TRADES"),
    (dict(equity=9_590.0, day_start_equity=9_600.0), "WEEKLY_LOSS"),
    (dict(consecutive_losses=6), "STREAK_STOP"),
    (dict(day_stopped=True), "STREAK_STOP"),
    (dict(ladder_level=2), "LADDER_L2"),
    (dict(ladder_level=3), "LADDER_L3"),
])
def test_check10_loss_limits(over, code):
    d = decide(good_state(**over))
    assert not d.approved and d.reason_code == code


def test_daily_loss_includes_floating_pnl_because_it_is_equity_based():
    assert decide(good_state(equity=9_801.0)).approved             # -1.99%
    assert decide(good_state(equity=9_800.0)).reason_code == "DAILY_LOSS"   # -2.00%


# ---------------------------------------------------------------- 11 sanity
def test_check11_independent_feed_and_price_sanity():
    assert decide(good_state(independent_mid=None)).reason_code == "NO_INDEPENDENT_FEED"
    assert decide(good_state(independent_mid=4010.0)).reason_code == "PRICE_SANITY"
    assert decide(good_state(atr_1m=None)).reason_code == "PRICE_SANITY"
    assert decide(good_state(independent_mid=4000.30 + 2.39)).approved     # < 3 x 0.8


def test_check11_fat_finger_needs_a_baseline_of_five_orders():
    assert decide(good_state(recent_order_sizes=[0.005] * 4)).approved
    d = decide(good_state(recent_order_sizes=[0.005] * 10))
    assert d.reason_code == "FAT_FINGER"                            # 0.02 > 3 x 0.005
    assert decide(good_state(recent_order_sizes=[0.01] * 10)).approved


# ---------------------------------------------------------------- overrides / property
def test_engine_built_with_tightened_limits_is_stricter():
    assert decide(good_state(equity=9_850.0), daily_loss_limit_pct=1.0).reason_code == \
        "DAILY_LOSS"
    assert decide(good_state(trades_today=2), max_trades_per_day=2).reason_code == \
        "MAX_TRADES_DAY"


@settings(max_examples=150, deadline=None)
@given(equity=st.floats(1_000, 200_000), dist=st.floats(2.0, 60.0),
       streak=st.booleans(), ladder=st.integers(0, 1))
def test_property_approved_risk_never_exceeds_the_stage_limit(equity, dist, streak, ladder):
    state = good_state(equity=equity, day_start_equity=equity, week_start_equity=equity,
                       risk_halved_by_streak=streak, ladder_level=ladder,
                       symbol=gold(freeze=0, stops=0))
    intent = good_intent(sl=4000.30 - dist, tp=4000.30 + 2 * dist)
    d = RiskEngine(limits()).approve(intent, state)
    if d.approved:
        cap = 0.25 * (0.5 if (streak or ladder) else 1.0)
        assert d.risk_usd <= equity * cap / 100 * 1.0000001
        assert d.adjusted_size >= 0.01
        assert abs(d.adjusted_size / 0.01 - round(d.adjusted_size / 0.01)) < 1e-6
