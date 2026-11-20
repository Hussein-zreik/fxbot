"""risk/limits.yaml loader: validation, hard caps, tighten-only overrides."""

import copy
from pathlib import Path

import pytest
import yaml

from goldbot.risk.rules import DEFAULT_LIMITS_PATH, LimitsError, load_limits, tighten


def raw():
    return yaml.safe_load(DEFAULT_LIMITS_PATH.read_text(encoding="utf-8"))


def write(tmp_path: Path, data) -> str:
    p = tmp_path / "limits.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return str(p)


def test_shipped_file_loads_exactly_as_the_rulebook_says():
    lim = load_limits()
    assert lim.version == 2 and lim.stage == "demo"
    assert lim.per_trade_cap() == 0.25
    assert lim.max_open_risk_pct == 2.0 and lim.max_open_positions == 2
    assert lim.max_trades_per_day == 4 and lim.max_effective_leverage == 5.0
    assert lim.daily_loss_limit_pct == 2.0 and lim.weekly_loss_limit_pct == 4.0
    assert lim.drawdown_ladder_from_hwm.L1.dd_pct == 5.0
    assert lim.drawdown_ladder_from_hwm.L3.dd_pct == 10.0
    assert lim.filters.spread_max_points["XAUUSD"] == 50
    assert lim.filters.no_entry_windows_server == ["23:45-00:30", "Fri 20:00-Sun 23:59"]
    assert lim.orders.require_broker_side_sl is True


@pytest.mark.parametrize("path,value", [
    (("daily_loss_limit_pct",), 3.0),
    (("weekly_loss_limit_pct",), 5.0),
    (("max_open_risk_pct",), 2.5),
    (("max_trades_per_day",), 5),
    (("max_effective_leverage",), 6.0),
    (("max_margin_usage_pct",), 50),
    (("per_trade_risk_pct", "demo"), 0.5),            # demo is capped at 0.25
    (("per_trade_risk_pct", "scaled_2"), 1.5),        # HARD MAX 1.00
    (("portfolio", "vol_target_annual"), 0.20),       # HARD MAX 0.15
    (("filters", "min_reward_risk"), 1.0),            # floor: 1.5
    (("filters", "news_blackout_min_before"), 10),    # floor: 30
    (("filters", "spread_max_points"), {"XAUUSD": 80}),
    (("drawdown_ladder_from_hwm", "L3", "dd_pct"), 15.0),
    (("human", "risk_increase_cooling_off_hours"), 24),
])
def test_loosened_values_raise_at_startup(tmp_path, path, value):
    data = raw()
    node = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(LimitsError):
        load_limits(write(tmp_path, data))


def test_tighter_values_are_allowed(tmp_path):
    data = raw()
    data["daily_loss_limit_pct"] = 1.5
    data["filters"]["min_reward_risk"] = 2.0
    data["portfolio"]["vol_target_annual"] = 0.15
    lim = load_limits(write(tmp_path, data))
    assert lim.daily_loss_limit_pct == 1.5 and lim.filters.min_reward_risk == 2.0


def test_structural_errors_raise(tmp_path):
    for mutate in (
        lambda d: d.update(extra_key=1),                                  # unknown key
        lambda d: d["orders"].update(require_broker_side_sl=False),       # SL mandatory
        lambda d: d.update(stage="yolo"),
        lambda d: d.update(version=1),
        lambda d: d["drawdown_ladder_from_hwm"]["L1"].update(dd_pct=8.0),  # L1 >= L2
        lambda d: d["per_trade_risk_pct"].update(micro_live=0.1, demo=0.25),  # not monotonic
        lambda d: d.update(daily_loss_limit_pct=-1),
    ):
        data = raw()
        mutate(data)
        with pytest.raises(LimitsError):
            load_limits(write(tmp_path, data))


def test_missing_or_garbage_file(tmp_path):
    with pytest.raises(LimitsError):
        load_limits(str(tmp_path / "nope.yaml"))
    bad = tmp_path / "bad.yaml"
    bad.write_text(":\n  - [unclosed", encoding="utf-8")
    with pytest.raises(LimitsError):
        load_limits(str(bad))


def test_stage_selects_per_trade_cap(tmp_path):
    data = raw()
    data["stage"] = "scaled_1"
    assert load_limits(write(tmp_path, data)).per_trade_cap() == 0.50


def test_cluster_lookup():
    lim = load_limits()
    assert lim.cluster_of("XAUUSD") == lim.cluster_of("GOLD") == lim.cluster_of("MGC")
    assert lim.cluster_of("XAUUSD") != lim.cluster_of("EURUSD")
    assert lim.cluster_of("UNLISTED") == "UNLISTED"          # its own cluster


def test_overrides_may_only_tighten():
    base = load_limits()
    tight = tighten(base, {"daily_loss_limit_pct": 1.0, "max_trades_per_day": 2,
                           "min_reward_risk": 2.0, "per_trade_risk_pct": 0.10,
                           "spread_max_points": {"XAUUSD": 40}})
    assert tight.daily_loss_limit_pct == 1.0 and tight.max_trades_per_day == 2
    assert tight.filters.min_reward_risk == 2.0 and tight.per_trade_cap() == 0.10
    assert tight.filters.spread_max_points["XAUUSD"] == 40
    assert base.daily_loss_limit_pct == 2.0                      # original untouched
    for loose in ({"daily_loss_limit_pct": 3.0}, {"max_trades_per_day": 9},
                  {"min_reward_risk": 1.0}, {"per_trade_risk_pct": 0.5},
                  {"spread_max_points": {"XAUUSD": 60}}, {"unknown_knob": 1}):
        with pytest.raises(LimitsError):
            tighten(base, loose)


def test_loader_does_not_mutate_inputs(tmp_path):
    data = raw()
    snapshot = copy.deepcopy(data)
    load_limits(write(tmp_path, data))
    assert data == snapshot
