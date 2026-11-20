"""Persistence, ladder, period tracking, kill switch, live gate, retcodes."""

from datetime import date, datetime, timedelta, timezone

import pytest

from goldbot.risk.killswitch import KillSwitch
from goldbot.risk.ladder import Ladder
from goldbot.risk.ledger import TradeLedger
from goldbot.risk.livegate import check_live_gate
from goldbot.risk.periods import PeriodTracker
from goldbot.risk.retcodes import Action, action_for, is_known
from goldbot.risk.store import RiskStore
from tests.risk_helpers import limits

T0 = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
OFFSET = 3 * 3600


@pytest.fixture
def store(tmp_path):
    return RiskStore(str(tmp_path / "risk.db"))


def minutes(n):
    return T0 + timedelta(minutes=n)


# ------------------------------------------------------------------ store
def test_store_kv_and_halts_survive_a_restart(tmp_path):
    path = str(tmp_path / "risk.db")
    first = RiskStore(path)
    first.set("hwm", 10_500.0)
    first.add_halt("MANUAL", "operator pause", T0)
    first.add_halt("KILL", "ops/KILL file", T0)
    again = RiskStore(path)
    assert again.get("hwm") == 10_500.0 and again.get("missing", 7) == 7
    assert {h["kind"] for h in again.active_halts()} == {"MANUAL", "KILL"}
    again.clear_halts("KILL", "reviewed", T0)
    assert [h["kind"] for h in again.active_halts()] == ["MANUAL"]


def test_store_records_decisions_for_traceability(store):
    store.record_decision(T0, "i-1", "gold_macro", "XAUUSD", "BUY", False, "SPREAD_MAX",
                          "51 > 50", None)
    row = store.decisions(limit=1)[0]
    assert row["reason_code"] == "SPREAD_MAX" and row["approved"] == 0


def test_store_uses_wal_and_is_safe_to_reopen(store):
    assert store.query("PRAGMA journal_mode")[0][0].lower() == "wal"


# ------------------------------------------------------------------ ledger
def test_ledger_counts_trades_and_losers_per_server_day(store):
    led = TradeLedger(store, halve_after=4, stop_after=6)
    led.record_open("2026-09-23", ticket=1, risk_usd=25.0, lots=0.02)
    led.record_open("2026-09-23", ticket=2, risk_usd=25.0, lots=0.02)
    led.record_close("2026-09-23", ticket=1, profit=-24.0)
    led.record_close("2026-09-23", ticket=2, profit=48.0)
    assert led.trades_today("2026-09-23") == 2 and led.losing_today("2026-09-23") == 1
    assert led.trades_today("2026-09-24") == 0


def test_four_losses_halve_risk_until_two_r_are_recovered(store):
    led = TradeLedger(store, halve_after=4, stop_after=6)
    for t in range(1, 5):
        led.record_open("d", t, 25.0, 0.02)
        led.record_close("d", t, -25.0)
    assert led.streak().losses == 4 and led.streak().halved
    led.record_open("d", 5, 25.0, 0.02)
    led.record_close("d", 5, 25.0)                      # +1R: still halved
    assert led.streak().halved and led.streak().consecutive_losses == 0
    led.record_open("d", 6, 25.0, 0.02)
    led.record_close("d", 6, 30.0)                      # +1.2R -> total +2.2R
    assert not led.streak().halved


def test_six_losses_stop_the_rest_of_the_day_only(store):
    led = TradeLedger(store, halve_after=4, stop_after=6)
    for t in range(1, 7):
        led.record_open("2026-09-23", t, 25.0, 0.02)
        led.record_close("2026-09-23", t, -25.0)
    assert led.day_stopped("2026-09-23") and not led.day_stopped("2026-09-24")


def test_unknown_ticket_close_is_ignored_safely(store):
    led = TradeLedger(store, halve_after=4, stop_after=6)
    assert led.record_close("d", ticket=999, profit=-5.0) is None


def test_recent_order_sizes_keep_the_last_fifty(store):
    led = TradeLedger(store, halve_after=4, stop_after=6)
    for i in range(60):
        led.record_open("d", i, 25.0, 0.01 + i / 1000)
    sizes = led.recent_sizes()
    assert len(sizes) == 50 and sizes[-1] == pytest.approx(0.069)


# ------------------------------------------------------------------ periods
def test_day_and_week_baselines_roll_at_server_midnight_and_survive_restart(store):
    p = PeriodTracker(store)
    a = p.update(10_000.0, T0, OFFSET)
    assert a.day_start_equity == 10_000.0 and a.week_start_equity == 10_000.0
    later = p.update(9_900.0, minutes(60), OFFSET)             # same day: baseline kept
    assert later.day_start_equity == 10_000.0
    restarted = PeriodTracker(store).update(9_850.0, minutes(120), OFFSET)
    assert restarted.day_start_equity == 10_000.0              # restart must not reset it
    # 21:30 UTC = 00:30 server on Thursday -> new day, same week
    nxt = PeriodTracker(store).update(9_800.0, datetime(2026, 9, 23, 21, 30, tzinfo=timezone.utc),
                                      OFFSET)
    assert nxt.day_start_equity == 9_800.0 and nxt.week_start_equity == 10_000.0
    monday = PeriodTracker(store).update(9_700.0, datetime(2026, 9, 27, 21, 30,
                                                           tzinfo=timezone.utc), OFFSET)
    assert monday.week_start_equity == 9_700.0                 # Monday 00:30 server


# ------------------------------------------------------------------ ladder
def test_ladder_levels_follow_drawdown_from_the_high_water_mark(store, tmp_path):
    lad = Ladder(limits(), store, changelog=str(tmp_path / "CHANGELOG.md"))
    assert lad.update(10_000.0, T0).level == 0
    assert lad.update(10_400.0, T0).hwm == 10_400.0
    assert lad.update(10_400.0 * 0.96, T0).level == 0           # -4%
    assert lad.update(10_400.0 * 0.95, T0).level == 1           # -5%
    assert lad.update(10_400.0 * 0.925, T0).level == 2          # -7.5%
    assert lad.update(10_400.0 * 0.90, T0).level == 3           # -10%


def test_level_one_clears_on_recovery_but_two_and_three_latch(store, tmp_path):
    lad = Ladder(limits(), store, changelog=str(tmp_path / "CHANGELOG.md"))
    lad.update(10_000.0, T0)
    assert lad.update(9_450.0, T0).level == 1
    assert lad.update(9_900.0, T0).level == 0                   # L1 is dynamic
    assert lad.update(9_200.0, T0).level == 2
    assert lad.update(9_900.0, T0).level == 2                   # L2 latches: human review
    assert Ladder(limits(), store, changelog=str(tmp_path / "CHANGELOG.md")
                  ).update(9_900.0, T0).level == 2              # ...and survives a restart


def test_reset_needs_a_note_writes_the_changelog_and_caps_risk(store, tmp_path):
    log = tmp_path / "CHANGELOG.md"
    lad = Ladder(limits(), store, changelog=str(log))
    lad.update(10_000.0, T0)
    lad.update(9_200.0, T0)
    with pytest.raises(ValueError):
        lad.reset("  ", "Hussein", T0)
    lad.reset("Reviewed trades; slippage was the cause", "Hussein", T0)
    assert lad.update(9_900.0, T0).level == 0
    assert lad.risk_cap_pct() == 0.25                           # resume at 0.25%
    entry = log.read_text(encoding="utf-8")
    assert "Hussein" in entry and "slippage was the cause" in entry and "LADDER" in entry


# ------------------------------------------------------------------ kill switch
@pytest.fixture
def kill(store, tmp_path):
    return KillSwitch(store, flag_path=str(tmp_path / "ops" / "KILL"),
                      changelog=str(tmp_path / "CHANGELOG.md"))


def test_nothing_triggers_when_everything_is_normal(kill):
    kill.record_equity(T0, 10_000.0)
    assert kill.check(T0, ladder_level=0, has_positions=True) is None


def test_flag_file_and_l3_trigger(kill, tmp_path):
    (tmp_path / "ops").mkdir()
    (tmp_path / "ops" / "KILL").write_text("")
    assert kill.check(T0, 0, False) == "KILL_FLAG"
    (tmp_path / "ops" / "KILL").unlink()
    assert kill.check(T0, 3, False) == "LADDER_L3"


def test_three_consecutive_order_errors(kill):
    for i in range(2):
        kill.record_order_result(minutes(i), ok=False, retcode=10006)
    assert kill.check(minutes(2), 0, False) is None
    kill.record_order_result(minutes(2), ok=False, retcode=10006)
    assert kill.check(minutes(3), 0, False) == "ORDER_ERRORS"


def test_a_success_resets_the_error_streak(kill):
    kill.record_order_result(T0, ok=False, retcode=10006)
    kill.record_order_result(T0, ok=False, retcode=10006)
    kill.record_order_result(T0, ok=True, retcode=10009)
    kill.record_order_result(T0, ok=False, retcode=10006)
    assert kill.check(T0, 0, False) is None


def test_more_than_fifty_orders_in_five_minutes(kill):
    for i in range(50):
        kill.record_order_sent(T0 + timedelta(seconds=i))
    assert kill.check(T0 + timedelta(seconds=50), 0, False) is None
    kill.record_order_sent(T0 + timedelta(seconds=51))
    assert kill.check(T0 + timedelta(seconds=52), 0, False) == "ORDER_RATE"


def test_pnl_drop_over_three_percent_inside_fifteen_minutes(kill):
    kill.record_equity(minutes(0), 10_000.0)
    kill.record_equity(minutes(10), 9_760.0)
    assert kill.check(minutes(10), 0, True) is None             # -2.4%
    kill.record_equity(minutes(14), 9_690.0)
    assert kill.check(minutes(14), 0, True) == "PNL_DROP"       # -3.1%


def test_a_slow_three_percent_slide_is_not_a_fifteen_minute_drop(kill):
    kill.record_equity(minutes(0), 10_000.0)
    kill.record_equity(minutes(20), 9_900.0)
    kill.record_equity(minutes(34), 9_690.0)
    assert kill.check(minutes(34), 0, True) is None


def test_reconciliation_mismatch_must_persist_five_minutes(kill):
    kill.record_reconciliation(minutes(0), mismatch=True)
    assert kill.check(minutes(4), 0, True) is None
    assert kill.check(minutes(5), 0, True) == "RECONCILIATION"
    kill.record_reconciliation(minutes(5), mismatch=False)       # healed: timer resets
    kill.record_reconciliation(minutes(6), mismatch=True)
    assert kill.check(minutes(7), 0, True) is None


def test_missing_equity_only_matters_with_open_positions(kill):
    kill.record_equity_unavailable(minutes(0))
    assert kill.check(minutes(6), 0, has_positions=False) is None
    assert kill.check(minutes(6), 0, has_positions=True) == "EQUITY_UNAVAILABLE"
    kill.record_equity(minutes(7), 10_000.0)
    assert kill.check(minutes(8), 0, True) is None


def test_more_than_three_unknown_retcodes_in_ten_minutes(kill):
    for i in range(3):
        kill.record_order_result(minutes(i), ok=True, retcode=77777)
    assert kill.check(minutes(3), 0, False) is None
    kill.record_order_result(minutes(3), ok=True, retcode=77778)
    assert kill.check(minutes(4), 0, False) == "UNKNOWN_RETCODES"
    fresh = KillSwitch(RiskStore(":memory:"), flag_path="x/KILL", changelog="x/C.md")
    for i in range(10):                                          # known codes never count
        fresh.record_order_result(minutes(i), ok=False, retcode=10006)
    assert fresh.check(minutes(10), 0, False) != "UNKNOWN_RETCODES"


def test_trip_persists_and_reset_needs_note_and_removed_flag(store, tmp_path):
    flag = tmp_path / "ops" / "KILL"
    log = tmp_path / "CHANGELOG.md"
    ks = KillSwitch(store, flag_path=str(flag), changelog=str(log))
    ks.trip("PNL_DROP", T0)
    assert ks.is_killed()
    assert KillSwitch(store, flag_path=str(flag), changelog=str(log)).is_killed()   # restart
    with pytest.raises(ValueError):
        ks.reset("", "Hussein", T0)
    flag.parent.mkdir()
    flag.write_text("")
    with pytest.raises(RuntimeError):
        ks.reset("looked at it", "Hussein", T0)                  # flag file still there
    flag.unlink()
    ks.reset("Feed glitch confirmed with broker", "Hussein", T0)
    assert not ks.is_killed()
    text = log.read_text(encoding="utf-8")
    assert "KILL RESET" in text and "Feed glitch" in text and "PNL_DROP" in text


# ------------------------------------------------------------------ live gate
def gate(**over):
    base = dict(env={"MODE": "live"}, account_login=123, expected_login=123,
                utc_today=date(2026, 9, 23), beirut_today=date(2026, 9, 23),
                confirm_text="2026-09-23\n", typed="LIVE 123")
    base.update(over)
    return check_live_gate(**base)


def test_live_gate_opens_only_with_all_four_conditions():
    assert gate().ok and gate().missing == []
    for over, key in [(dict(env={"MODE": "paper"}), "MODE"),
                      (dict(env={}), "MODE"),
                      (dict(expected_login=None), "ACCOUNT"),
                      (dict(account_login=999), "ACCOUNT"),
                      (dict(confirm_text=None), "LIVE_CONFIRM"),
                      (dict(confirm_text="2026-09-22"), "LIVE_CONFIRM"),
                      (dict(typed=None), "TYPED"),
                      (dict(typed="live 123"), "TYPED"),
                      (dict(typed="LIVE 999"), "TYPED")]:
        result = gate(**over)
        assert not result.ok and any(key in m for m in result.missing), (over, result)


def test_live_confirm_may_use_the_utc_or_the_beirut_date():
    late = dict(utc_today=date(2026, 9, 23), beirut_today=date(2026, 9, 24))
    assert gate(confirm_text="2026-09-24", **late).ok
    assert gate(confirm_text="2026-09-23", **late).ok
    assert not gate(confirm_text="2026-09-25", **late).ok


# ------------------------------------------------------------------ retcodes
def test_retcode_table_matches_the_rulebook():
    assert action_for(10009) is Action.DONE
    assert action_for(10019) is Action.HALT and action_for(10027) is Action.HALT
    assert action_for(10004) is Action.RETRY_REFRESH and action_for(10020) is Action.RETRY_REFRESH
    assert action_for(10030) is Action.SWITCH_FILLING
    assert action_for(10016) is Action.WIDEN_OR_SKIP
    assert action_for(10018) is Action.SKIP and action_for(10024) is Action.BACKOFF
    assert action_for(10031) is Action.RECONNECT
    assert action_for(10006) is Action.REJECT
    assert action_for(424242) is Action.UNKNOWN
    assert is_known(10009) and not is_known(424242)
