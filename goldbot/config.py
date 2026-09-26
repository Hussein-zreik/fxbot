"""Typed configuration loaded from a JSON file plus environment overrides.

Every tunable number in the strategy lives here so it can be changed without
touching code. Unknown keys in the JSON file raise an error, which catches
typos such as ``"max_open_position"`` before the bot goes live.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, List, Optional


@dataclass
class MT5Config:
    terminal_path: str = ""          # Optional path to terminal64.exe
    login: int = 0                   # 0 = attach to the already logged-in terminal
    password: str = ""
    server: str = ""
    timeout_ms: int = 60_000
    reconnect_attempts: int = 5
    reconnect_base_delay_s: float = 2.0


@dataclass
class SymbolConfig:
    gold: str = "XAUUSD"
    silver: str = "XAGUSD"
    eurusd: str = "EURUSD"
    # Broker USD index symbol ("DXY", "USDX", ...). Empty = use inverse EURUSD.
    dxy: str = ""


@dataclass
class StrategyConfig:
    execution_timeframe: str = "M5"
    correlation_timeframe: str = "M15"

    buy_threshold: int = 60
    sell_threshold: int = -60
    # Balanced weights: 25 + 20 + 20 + (10 + 10) + 10 + 5 = 100
    weight_yield: int = 25
    weight_dxy: int = 20
    weight_silver: int = 20
    weight_vwap: int = 5

    # A. Treasury yield engine
    yield_ticker: str = "^TNX"
    yield_trigger_bp: float = 2.0            # 2 bp = 0.02 percentage points
    yield_window_minutes: int = 15
    yield_zscore_lookback: int = 60          # observations of 1h change
    yield_min_abs_zscore: float = 0.0        # 0 disables the z-score gate
    yield_refresh_seconds: int = 60
    yield_max_staleness_minutes: int = 30

    # B. DXY / silver correlation engine
    donchian_period: int = 20
    breakout_lookback_bars: int = 3
    correlation_window: int = 30
    min_correlation: float = 0.6             # Pearson R of bar returns
    usd_roc_bars: int = 4
    usd_roc_min_pct: float = 0.03            # USD must move >= 0.03 % to count
    block_on_divergence: bool = True

    # D. Technical trigger engine
    atr_period: int = 14
    vwap_retest_atr_mult: float = 1.0
    require_vwap_retest: bool = True


_GOOGLE_NEWS = "https://news.google.com/rss/search?q="
_GOOGLE_NEWS_OPTS = "&hl=en-US&gl=US&ceid=US:en"


@dataclass
class TrendConfig:
    """E. Multi-timeframe trend engine (EMA alignment + ADX + market structure)."""
    enabled: bool = True
    timeframes: List[str] = field(default_factory=lambda: ["H4", "H1"])
    weights: List[int] = field(default_factory=lambda: [10, 10])  # per timeframe
    ema_fast: int = 50
    ema_slow: int = 200
    ema_slope_bars: int = 5
    adx_period: int = 14
    adx_min: float = 20.0           # below this the market is "ranging"
    swing_strength: int = 3         # bars each side that define a swing point
    # Filter: block trades against the trend of this timeframe ("" disables).
    filter_timeframe: str = "H4"
    bars: int = 400


@dataclass
class SentimentConfig:
    """F. AI headline sentiment via any OpenAI-compatible chat API (default: Groq)."""
    enabled: bool = True
    weight: int = 10
    api_base_url: str = "https://api.groq.com/openai/v1"
    api_key_env: str = "GROQ_API_KEY"   # the key itself is read from this env var
    model: str = "llama-3.3-70b-versatile"
    json_mode: bool = True           # set false if a provider rejects response_format
    feeds: List[str] = field(default_factory=lambda: [
        _GOOGLE_NEWS + "gold+price+OR+XAUUSD+when:1d" + _GOOGLE_NEWS_OPTS,
        _GOOGLE_NEWS + "Federal+Reserve+OR+Treasury+yields+OR+US+dollar+when:1d"
        + _GOOGLE_NEWS_OPTS,
        "https://www.fxstreet.com/rss/news",
    ])
    refresh_minutes: int = 5
    lookback_hours: float = 6.0
    half_life_hours: float = 2.0     # older headlines count less
    min_relevant_headlines: int = 3
    min_relevance: float = 0.3
    deadband: float = 0.15           # |sentiment| below this scores 0
    batch_size: int = 20
    max_new_per_refresh: int = 60    # protects the free-tier quota
    request_timeout_s: float = 30.0
    cache_file: str = "data/sentiment_cache.json"


@dataclass
class NewsConfig:
    enabled: bool = True
    calendar_url: str = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    currencies: List[str] = field(default_factory=lambda: ["USD"])
    impacts: List[str] = field(default_factory=lambda: ["High"])
    title_keywords: List[str] = field(default_factory=list)  # empty = all
    minutes_before: int = 30
    minutes_after: int = 15
    refresh_minutes: int = 60
    max_cache_age_hours: int = 24
    request_timeout_s: float = 10.0
    cache_file: str = "data/calendar_cache.json"
    fail_closed: bool = True                 # no calendar -> no new trades
    manage_open_positions: bool = True
    partial_close_fraction: float = 0.5
    breakeven_buffer_points: int = 10
    close_fully_if_unsplittable: bool = True


@dataclass
class RiskConfig:
    risk_per_trade: float = 0.01
    sl_atr_mult: float = 1.5
    tp_atr_mult: float = 3.0
    max_daily_loss: float = 0.03
    close_all_account_positions_on_halt: bool = False
    max_spread_points: int = 60
    max_lot: float = 5.0
    margin_usage_limit: float = 0.9          # max share of free margin per trade


@dataclass
class ExecutionConfig:
    dry_run: bool = True
    allow_live_trading: bool = False
    magic: int = 20260926
    order_comment: str = "goldbot"
    deviation_points: int = 30
    order_retries: int = 3
    loop_interval_seconds: int = 10
    max_open_positions: int = 1
    cooldown_minutes: int = 15
    session_filter_enabled: bool = True
    session_start_utc: str = "07:00"
    session_end_utc: str = "20:00"
    trade_weekdays: List[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])
    server_utc_offset_hours: Optional[float] = None  # None = auto-detect
    market_stale_seconds: int = 300


@dataclass
class AppConfig:
    mt5: MT5Config = field(default_factory=MT5Config)
    symbols: SymbolConfig = field(default_factory=SymbolConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    news: NewsConfig = field(default_factory=NewsConfig)
    trend: TrendConfig = field(default_factory=TrendConfig)
    ai_news: SentimentConfig = field(default_factory=SentimentConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    log_dir: str = "logs"
    log_level: str = "INFO"
    state_file: str = "data/state.json"
    journal_file: str = "logs/signal_journal.csv"


def _merge(instance: Any, overrides: dict, path: str = "") -> None:
    """Recursively apply a dict onto a dataclass, rejecting unknown keys."""
    known = {f.name: f for f in fields(instance)}
    for key, value in overrides.items():
        if key.startswith("_"):
            continue  # allow "_comment" keys in the JSON file
        if key not in known:
            raise ValueError(f"Unknown config key: {path}{key}")
        current = getattr(instance, key)
        if is_dataclass(current):
            if not isinstance(value, dict):
                raise ValueError(f"Config key {path}{key} must be an object")
            _merge(current, value, f"{path}{key}.")
        else:
            setattr(instance, key, value)


def _validate(cfg: AppConfig) -> None:
    s, r, e = cfg.strategy, cfg.risk, cfg.execution
    if not 0 < r.risk_per_trade <= 0.05:
        raise ValueError("risk.risk_per_trade must be in (0, 0.05]")
    if not 0 < r.max_daily_loss <= 0.2:
        raise ValueError("risk.max_daily_loss must be in (0, 0.2]")
    if r.sl_atr_mult <= 0 or r.tp_atr_mult <= 0:
        raise ValueError("ATR multipliers must be positive")
    if s.buy_threshold <= 0 or s.sell_threshold >= 0:
        raise ValueError("buy_threshold must be > 0 and sell_threshold < 0")
    if e.max_open_positions < 1:
        raise ValueError("execution.max_open_positions must be >= 1")
    if not 0 < cfg.news.partial_close_fraction < 1:
        raise ValueError("news.partial_close_fraction must be in (0, 1)")
    t = cfg.trend
    if len(t.timeframes) != len(t.weights):
        raise ValueError("trend.timeframes and trend.weights must have equal length")
    if t.filter_timeframe and t.filter_timeframe not in t.timeframes:
        raise ValueError("trend.filter_timeframe must be one of trend.timeframes")
    if t.ema_fast >= t.ema_slow:
        raise ValueError("trend.ema_fast must be smaller than trend.ema_slow")
    for d in e.trade_weekdays:
        if d not in range(7):
            raise ValueError("execution.trade_weekdays values must be 0-6")


def load_config(path: Optional[str] = None) -> AppConfig:
    """Load config from JSON (optional) and apply MT5 credential env vars."""
    cfg = AppConfig()
    if path:
        with open(Path(path), "r", encoding="utf-8") as fh:
            _merge(cfg, json.load(fh))

    # Credentials are best kept out of files.
    if os.getenv("MT5_LOGIN"):
        cfg.mt5.login = int(os.environ["MT5_LOGIN"])
    cfg.mt5.password = os.getenv("MT5_PASSWORD", cfg.mt5.password)
    cfg.mt5.server = os.getenv("MT5_SERVER", cfg.mt5.server)
    cfg.mt5.terminal_path = os.getenv("MT5_PATH", cfg.mt5.terminal_path)

    _validate(cfg)
    return cfg
