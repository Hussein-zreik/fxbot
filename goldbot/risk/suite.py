"""Everything the risk core needs, built once from the app config."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import AppConfig
from .engine import RiskEngine
from .killswitch import KillSwitch
from .ladder import Ladder
from .ledger import TradeLedger
from .periods import PeriodTracker
from .rules import Limits, load_limits, tighten
from .store import RiskStore


@dataclass
class RiskSuite:
    limits: Limits
    store: RiskStore
    engine: RiskEngine
    ladder: Ladder
    periods: PeriodTracker
    kill: KillSwitch
    ledger: TradeLedger


def build_suite(cfg: AppConfig, db_path: str = "") -> RiskSuite:
    """Load the rulebook (hard caps enforced), apply tighten-only overrides, open SQLite."""
    limits = tighten(load_limits(cfg.risk.limits_file or None), cfg.risk.overrides)
    path = db_path or cfg.risk.db_file
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    store = RiskStore(path)
    return RiskSuite(
        limits=limits, store=store, engine=RiskEngine(limits, store),
        ladder=Ladder(limits, store, cfg.risk.changelog_file),
        periods=PeriodTracker(store),
        kill=KillSwitch(store, cfg.risk.kill_flag_file, cfg.risk.changelog_file),
        ledger=TradeLedger(store, limits.consecutive_losses_halve,
                           limits.consecutive_losses_stop_day))
