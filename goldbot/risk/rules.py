"""Load and validate ``risk/limits.yaml`` (the Unified Risk Rulebook).

* The file is the rulebook; code never writes to it.
* Every numeric limit is checked against a hard bound at startup. Bounds that
  CLAUDE.md names explicitly are the stage maxima (0.25 / 0.25 / 0.50 / 1.00
  percent per trade) and ``vol_target_annual`` <= 0.15. For all other limits the
  bound is the rulebook v2 value itself: a limit may be made *tighter* in the
  YAML or via ``tighten()`` overrides, never looser. Loosening one is a
  deliberate change to ``BOUNDS`` below plus a ``CHANGELOG.md`` entry.
* A looser value raises ``LimitsError`` and the bot refuses to start.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

DEFAULT_LIMITS_PATH = Path(__file__).resolve().parents[2] / "risk" / "limits.yaml"

Stage = Literal["demo", "micro_live", "scaled_1", "scaled_2"]


class LimitsError(ValueError):
    """The limits file is invalid or looser than the hard caps allow."""


# (dotted path, "max" | "min", bound). "max": value may not exceed it (tighter =
# lower). "min": value may not go below it (tighter = higher).
BOUNDS: List[Tuple[str, str, float]] = [
    ("per_trade_risk_pct.demo", "max", 0.25),
    ("per_trade_risk_pct.micro_live", "max", 0.25),
    ("per_trade_risk_pct.scaled_1", "max", 0.50),
    ("per_trade_risk_pct.scaled_2", "max", 1.00),            # HARD MAX 1.00
    ("max_open_risk_pct", "max", 2.0),
    ("max_open_positions", "max", 2),
    ("max_trades_per_day", "max", 4),
    ("max_effective_leverage", "max", 5.0),
    ("max_margin_usage_pct", "max", 30),
    ("crypto_max_equity_per_venue_pct", "max", 15),
    ("daily_loss_limit_pct", "max", 2.0),
    ("daily_max_losing_trades", "max", 3),
    ("weekly_loss_limit_pct", "max", 4.0),
    ("consecutive_losses_halve", "max", 4),
    ("consecutive_losses_stop_day", "max", 6),
    ("drawdown_ladder_from_hwm.L1.dd_pct", "max", 5.0),
    ("drawdown_ladder_from_hwm.L2.dd_pct", "max", 7.5),
    ("drawdown_ladder_from_hwm.L3.dd_pct", "max", 10.0),
    ("filters.news_blackout_min_before", "min", 30),
    ("filters.news_blackout_min_after", "min", 15),
    ("filters.spread_max_mult_of_hour_median", "max", 2.0),
    ("filters.min_reward_risk", "min", 1.5),
    ("filters.price_sanity_atr1m_mult", "max", 3.0),
    ("filters.fat_finger_size_mult", "max", 3.0),
    ("portfolio.vol_target_annual", "max", 0.15),             # HARD MAX 0.15
    ("portfolio.max_ex_ante_vol_mult", "max", 1.25),
    ("portfolio.es_975_1d_max_pct", "max", 2.5),
    ("portfolio.weekend_gap_scenario_max_loss_pct", "max", 6.0),
    ("portfolio.max_instrument_share_of_risk", "max", 0.25),
    ("portfolio.max_cluster_share_of_risk", "max", 0.40),
    ("orders.max_orders_per_min_per_adapter", "max", 20),
    ("orders.max_modifications_per_order", "max", 5),
    ("orders.requote_retries", "max", 2),
    ("human.no_risk_increase_hours_after_night_shift", "min", 12),
    ("human.risk_increase_cooling_off_hours", "min", 48),
]
SPREAD_POINT_BOUNDS = {"XAUUSD": 50}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PerTradeRisk(_Strict):
    demo: float = Field(gt=0)
    micro_live: float = Field(gt=0)
    scaled_1: float = Field(gt=0)
    scaled_2: float = Field(gt=0)

    @model_validator(mode="after")
    def _monotonic(self):
        if not self.demo <= self.micro_live <= self.scaled_1 <= self.scaled_2:
            raise ValueError("per-trade risk must not decrease from stage to stage")
        return self


class LadderStep(_Strict):
    dd_pct: float = Field(gt=0, le=100)
    action: str


class Ladder(_Strict):
    L1: LadderStep
    L2: LadderStep
    L3: LadderStep

    @model_validator(mode="after")
    def _ordered(self):
        if not self.L1.dd_pct < self.L2.dd_pct < self.L3.dd_pct:
            raise ValueError("ladder thresholds must satisfy L1 < L2 < L3")
        return self


class Filters(_Strict):
    news_blackout_min_before: float = Field(ge=0)
    news_blackout_min_after: float = Field(ge=0)
    news_impact_levels: List[str]
    news_currencies: Dict[str, List[str]]
    calendar_fail_closed_windows_beirut: List[str]
    spread_max_mult_of_hour_median: float = Field(gt=0)
    spread_max_points: Dict[str, float]
    no_entry_windows_server: List[str]
    min_reward_risk: float = Field(gt=0)
    price_sanity_atr1m_mult: float = Field(gt=0)
    fat_finger_size_mult: float = Field(gt=0)


class Portfolio(_Strict):
    vol_target_annual: float = Field(gt=0)
    max_ex_ante_vol_mult: float = Field(gt=0)
    es_975_1d_max_pct: float = Field(gt=0)
    weekend_gap_scenario_max_loss_pct: float = Field(gt=0)
    gap_scenario: Dict[str, float]
    max_instrument_share_of_risk: float = Field(gt=0, le=1)
    max_cluster_share_of_risk: float = Field(gt=0, le=1)


class Orders(_Strict):
    max_orders_per_min_per_adapter: int = Field(gt=0)
    max_modifications_per_order: int = Field(ge=0)
    requote_retries: int = Field(ge=0)
    require_broker_side_sl: bool


class Human(_Strict):
    no_risk_increase_hours_after_night_shift: float = Field(ge=0)
    risk_increase_cooling_off_hours: float = Field(ge=0)


class Limits(_Strict):
    version: int
    stage: Stage
    per_trade_risk_pct: PerTradeRisk
    max_open_risk_pct: float = Field(gt=0)
    cluster_counts_as_one: List[List[str]]
    max_open_positions: int = Field(ge=1)
    max_trades_per_day: int = Field(ge=1)
    max_effective_leverage: float = Field(gt=0)
    max_margin_usage_pct: float = Field(gt=0, le=100)
    crypto_max_equity_per_venue_pct: float = Field(ge=0, le=100)
    daily_loss_limit_pct: float = Field(gt=0, le=100)
    daily_max_losing_trades: int = Field(ge=1)
    weekly_loss_limit_pct: float = Field(gt=0, le=100)
    consecutive_losses_halve: int = Field(ge=1)
    consecutive_losses_stop_day: int = Field(ge=1)
    drawdown_ladder_from_hwm: Ladder
    filters: Filters
    portfolio: Portfolio
    orders: Orders
    human: Human

    @model_validator(mode="after")
    def _consistency(self):
        if self.version != 2:
            raise ValueError("unsupported limits version (this code understands 2)")
        if not self.orders.require_broker_side_sl:
            raise ValueError("orders.require_broker_side_sl must be true (prime directive 3)")
        if self.weekly_loss_limit_pct < self.daily_loss_limit_pct:
            raise ValueError("weekly loss limit must be >= the daily limit")
        if self.consecutive_losses_stop_day < self.consecutive_losses_halve:
            raise ValueError("stop-day streak must be >= the halving streak")
        return self

    def per_trade_cap(self, stage: Optional[str] = None) -> float:
        """Per-trade risk, percent of equity, for the given (default: current) stage."""
        return getattr(self.per_trade_risk_pct, stage or self.stage)

    def cluster_of(self, symbol: str) -> str:
        """Stable id of the correlation cluster ``symbol`` belongs to."""
        for group in self.cluster_counts_as_one:
            if symbol.upper() in {s.upper() for s in group}:
                return "|".join(group)
        return symbol.upper()


def _get(model: BaseModel, dotted: str):
    node: Any = model
    for part in dotted.split("."):
        node = getattr(node, part)
    return node


def _enforce_bounds(limits: Limits) -> None:
    problems = []
    for path, kind, bound in BOUNDS:
        value = _get(limits, path)
        if (kind == "max" and value > bound) or (kind == "min" and value < bound):
            word = "above" if kind == "max" else "below"
            problems.append(f"{path} = {value} is {word} the hard cap {bound}")
    for symbol, cap in SPREAD_POINT_BOUNDS.items():
        value = limits.filters.spread_max_points.get(symbol)
        if value is not None and value > cap:
            problems.append(f"filters.spread_max_points.{symbol} = {value} is above {cap}")
    if problems:
        raise LimitsError("limits looser than the hard caps: " + "; ".join(problems))


def load_limits(path: Optional[str] = None) -> Limits:
    """Read, validate and hard-cap the rulebook. Raises ``LimitsError``."""
    file = Path(path) if path else DEFAULT_LIMITS_PATH
    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8"))
    except OSError as exc:
        raise LimitsError(f"cannot read {file}: {exc.strerror or exc}") from None
    except yaml.YAMLError as exc:
        raise LimitsError(f"{file} is not valid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise LimitsError(f"{file} must contain a mapping")
    try:
        limits = Limits.model_validate(data)
    except ValidationError as exc:
        raise LimitsError("invalid limits file: " + "; ".join(
            f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())) from None
    _enforce_bounds(limits)
    return limits


# Flat override names accepted by tighten(): name -> (dotted path, "max"|"min")
_OVERRIDES = {
    "daily_loss_limit_pct": ("daily_loss_limit_pct", "max"),
    "weekly_loss_limit_pct": ("weekly_loss_limit_pct", "max"),
    "daily_max_losing_trades": ("daily_max_losing_trades", "max"),
    "max_trades_per_day": ("max_trades_per_day", "max"),
    "max_open_positions": ("max_open_positions", "max"),
    "max_open_risk_pct": ("max_open_risk_pct", "max"),
    "max_effective_leverage": ("max_effective_leverage", "max"),
    "max_margin_usage_pct": ("max_margin_usage_pct", "max"),
    "min_reward_risk": ("filters.min_reward_risk", "min"),
    "spread_max_mult_of_hour_median": ("filters.spread_max_mult_of_hour_median", "max"),
}


def tighten(limits: Limits, overrides: Dict[str, Any]) -> Limits:
    """Apply settings overrides; each must be at least as strict as the rulebook."""
    out = copy.deepcopy(limits)
    for name, value in (overrides or {}).items():
        if name == "per_trade_risk_pct":
            cap = limits.per_trade_cap()
            if value > cap or value <= 0:
                raise LimitsError(f"override per_trade_risk_pct={value} is looser than {cap}")
            setattr(out.per_trade_risk_pct, limits.stage, float(value))
            # keep the stage ladder monotonic after tightening the current stage
            for later in ("micro_live", "scaled_1", "scaled_2"):
                if getattr(out.per_trade_risk_pct, later) < value:
                    setattr(out.per_trade_risk_pct, later, float(value))
        elif name == "spread_max_points":
            for symbol, cap in dict(value).items():
                current = limits.filters.spread_max_points.get(symbol)
                if current is None or cap > current:
                    raise LimitsError(f"override spread_max_points.{symbol}={cap} is "
                                      f"looser than {current}")
                out.filters.spread_max_points[symbol] = cap
        elif name in _OVERRIDES:
            path, kind = _OVERRIDES[name]
            current = _get(limits, path)
            if (kind == "max" and value > current) or (kind == "min" and value < current):
                raise LimitsError(f"override {name}={value} is looser than {current}")
            parent, _, leaf = path.rpartition(".")
            setattr(_get(out, parent) if parent else out, leaf, value)
        else:
            raise LimitsError(f"unknown override '{name}'")
    _enforce_bounds(out)
    return out
