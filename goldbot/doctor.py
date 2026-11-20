"""Setup doctor:  python -m goldbot.doctor

Checks everything the bot depends on and says, in plain words, what to fix.
Safe to run any time (it never places orders; with a Telegram chat configured
it sends one test message). Exit code 1 means at least one blocking problem.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, List, Optional

import requests

from .config import AppConfig, load_config
from .news_filter import parse_calendar
from .secrets_file import DEFAULT_PATH, load_secrets_file

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
_MARK = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]", SKIP: "[SKIP]"}


@dataclass
class Check:
    name: str
    status: str
    detail: str
    hint: str = ""


def _python(cfg: AppConfig) -> Check:
    v = sys.version_info
    text = f"Python {v.major}.{v.minor}.{v.micro}"
    if (3, 10) <= (v.major, v.minor) <= (3, 12):
        return Check("Python", OK, text)
    return Check("Python", WARN, text, "Use Python 3.11 (the installer does this).")


def _packages(cfg: AppConfig) -> Check:
    missing = []
    for mod in ("pandas", "numpy", "requests", "yfinance"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if sys.platform == "win32":
        try:
            __import__("MetaTrader5")
        except ImportError:
            missing.append("MetaTrader5")
    if missing:
        return Check("Packages", FAIL, "missing: " + ", ".join(missing),
                     "Run: .venv\\Scripts\\python -m pip install -r requirements.txt")
    return Check("Packages", OK, "all installed")


def _config(cfg: AppConfig) -> Check:
    mode = "DRY-RUN (no real orders)" if cfg.execution.dry_run else "orders ENABLED"
    return Check("Config", OK, f"valid; {mode}; live accounts "
                 f"{'allowed' if cfg.execution.allow_live_trading else 'blocked'}")


def _secret_checks(cfg: AppConfig, env) -> List[Check]:
    out = []
    groq = env.get(cfg.ai_news.api_key_env, "")
    out.append(Check("AI key", OK, "Groq key present") if groq else
               Check("AI key", WARN, "not set - AI news scores 0",
                     "Re-run: python -m goldbot.setup_wizard"))
    token_ok = bool(env.get(cfg.alerts.token_env)) and bool(env.get(cfg.alerts.chat_id_env))
    out.append(Check("Telegram keys", OK, "token and chat id present") if token_ok else
               Check("Telegram keys", WARN, "not set - no phone alerts",
                     "Re-run: python -m goldbot.setup_wizard"))
    dash = env.get(cfg.dashboard.token_env, "")
    out.append(Check("Dashboard token", OK, "present") if len(dash) >= 16 else
               Check("Dashboard token", WARN, "missing or shorter than 16 characters - "
                     "the dashboard will not start", "Re-run: python -m goldbot.setup_wizard"))
    return out


def _calendar(cfg: AppConfig, http_get: Callable) -> Check:
    try:
        resp = http_get(cfg.news.calendar_url, timeout=cfg.news.request_timeout_s,
                        headers={"User-Agent": "goldbot/1.0"})
        resp.raise_for_status()
        events = parse_calendar(resp.json(), cfg.news)
        return Check("News calendar", OK, f"{len(events)} high-impact USD events this week")
    except Exception as exc:  # noqa: BLE001 - any network/parse error
        status = FAIL if (cfg.news.enabled and cfg.news.fail_closed) else WARN
        return Check("News calendar", status, f"download failed ({type(exc).__name__})",
                     "Check the server's internet. Until it works the bot will not trade "
                     "(safe default).")


def _yield(cfg: AppConfig, fetcher: Callable, now: datetime) -> Check:
    try:
        series = fetcher(cfg.strategy.yield_ticker)
        value = float(series.iloc[-1])
        if value > 20:
            value /= 10.0
        age = (now - series.index[-1].to_pydatetime()).total_seconds() / 60
        detail = f"10Y yield {value:.3f}%, last print {age:.0f} min ago"
        if age > cfg.strategy.yield_max_staleness_minutes:
            return Check("Yield data", WARN, detail,
                         "Stale - normal when the bond market is closed (weekend/night).")
        return Check("Yield data", OK, detail)
    except Exception as exc:  # noqa: BLE001
        return Check("Yield data", WARN, f"fetch failed ({type(exc).__name__})",
                     "The yield signal will score 0 until Yahoo data works.")


def _groq(cfg: AppConfig, env, http_get: Callable) -> Check:
    key = env.get(cfg.ai_news.api_key_env, "")
    if not key:
        return Check("Groq API", SKIP, "no key")
    try:
        resp = http_get(f"{cfg.ai_news.api_base_url.rstrip('/')}/models",
                        headers={"Authorization": f"Bearer {key}"}, timeout=15)
    except requests.RequestException as exc:
        return Check("Groq API", WARN, f"unreachable ({type(exc).__name__})")
    if resp.status_code in (401, 403):
        return Check("Groq API", FAIL, "key rejected",
                     "Create a new key at console.groq.com and re-run the wizard.")
    if resp.status_code != 200:
        return Check("Groq API", WARN, f"HTTP {resp.status_code}")
    ids = {m.get("id") for m in resp.json().get("data", [])}
    if cfg.ai_news.model not in ids:
        return Check("Groq API", WARN, f"key works but model '{cfg.ai_news.model}' not offered",
                     "Pick a current model at console.groq.com/docs/models and set "
                     "ai_news.model in config.json.")
    return Check("Groq API", OK, f"key works, model '{cfg.ai_news.model}' available")


def _telegram(cfg: AppConfig, env, http_post: Callable) -> Check:
    token, chat = env.get(cfg.alerts.token_env, ""), env.get(cfg.alerts.chat_id_env, "")
    if not (token and chat):
        return Check("Telegram", SKIP, "not configured")
    from .notifier import TelegramNotifier
    ok = TelegramNotifier(cfg.alerts, http_post=http_post, token=token,
                          chat_id=chat).deliver("GoldBot doctor: Telegram works ✅")
    return Check("Telegram", OK, "test message sent - check your phone") if ok else \
        Check("Telegram", FAIL, "could not send", "Re-run: python -m goldbot.setup_wizard")


def _dashboard_port(cfg: AppConfig) -> Check:
    if not cfg.dashboard.enabled:
        return Check("Dashboard port", SKIP, "dashboard disabled")
    with socket.socket() as s:
        try:
            s.bind((cfg.dashboard.host, cfg.dashboard.port))
        except OSError:
            return Check("Dashboard port", WARN, f"port {cfg.dashboard.port} is busy",
                         "Is the bot already running? Otherwise change dashboard.port.")
    return Check("Dashboard port", OK, f"port {cfg.dashboard.port} is free")


def _mt5(cfg: AppConfig, connector_factory: Optional[Callable]) -> List[Check]:
    if connector_factory is None:
        return [Check("MetaTrader 5", SKIP, "needs Windows with the MT5 terminal")]
    out: List[Check] = []
    try:
        conn = connector_factory()
    except RuntimeError as exc:
        return [Check("MetaTrader 5", FAIL, str(exc)[:120],
                      "Install with: .venv\\Scripts\\python -m pip install MetaTrader5")]
    try:
        if not conn.connect():
            return [Check("MetaTrader 5", FAIL, "could not connect",
                          "Open MT5, log in to your account (File > Login to Trade "
                          "Account), and set mt5.terminal_path in config.json.")]
        acc = conn.account()
        if acc is None:
            return [Check("MetaTrader 5", FAIL, "connected but no account info")]
        out.append(Check("MT5 account", OK, f"{'DEMO' if acc.is_demo else 'LIVE'} #{acc.login} "
                         f"on {acc.server}, {acc.balance:,.2f} {acc.currency}, "
                         f"{'hedging' if acc.hedging else 'netting'}"))
        if not acc.is_demo and not cfg.execution.allow_live_trading:
            out.append(Check("Live safety", OK, "real account but allow_live_trading is false "
                             "- the bot will stay in dry-run"))
        allowed = conn.algo_trading_allowed()
        out.append(Check("Algo Trading", WARN if allowed is False else OK,
                         "button is OFF" if allowed is False else
                         ("on" if allowed else "unknown"),
                         "Click the 'Algo Trading' button in MT5 until it is green."
                         if allowed is False else ""))

        names = {}
        sym = cfg.symbols
        wanted = [("gold", sym.gold), ("silver", sym.silver)]
        wanted.append(("USD proxy", sym.dxy) if sym.dxy else ("EURUSD", sym.eurusd))
        for label, base in wanted:
            name = conn.resolve_symbol(base)
            names[label] = name
            out.append(Check(f"Symbol {label}", OK if name else FAIL,
                             f"{base} -> {name}" if name else f"'{base}' not found",
                             "" if name else "Find your broker's name in MT5 Market Watch "
                             "and set it under symbols in config.json."))
        gold = names.get("gold")
        if gold:
            needs = [(cfg.strategy.execution_timeframe, 400)] + \
                    [(tf, cfg.trend.bars) for tf in cfg.trend.timeframes]
            for tf, count in needs:
                df = conn.get_rates(gold, tf, count)
                got = 0 if df is None else len(df)
                out.append(Check(f"Bars {tf}", OK if got >= count else WARN,
                                 f"{got}/{count} bars",
                                 "" if got >= count else "Open a gold chart on that timeframe in "
                                 "MT5 and scroll back once so the history downloads."))
            tick = conn.get_tick(gold)
            if tick:
                spread = (tick.ask - tick.bid) / conn.symbol_spec(gold).point
                out.append(Check("Spread", OK if spread <= cfg.risk.max_spread_points else WARN,
                                 f"{spread:.0f} points (limit {cfg.risk.max_spread_points})"))
            live = conn.market_is_live(gold)
            out.append(Check("Live prices", OK if live else WARN,
                             "ticking" if live else "no fresh ticks",
                             "" if live else "Normal on weekends and market holidays."))
    finally:
        conn.shutdown()
    return out


def run_checks(cfg: AppConfig, *, env=os.environ, connector_factory=None,
               http_get: Callable = requests.get, http_post: Callable = requests.post,
               yield_fetcher: Optional[Callable] = None,
               now: Optional[datetime] = None) -> List[Check]:
    if yield_fetcher is None:
        from .macro_engine import fetch_yield_series_yfinance as yield_fetcher
    now = now or datetime.now(timezone.utc)
    checks = [_python(cfg), _packages(cfg), _config(cfg)]
    checks += _secret_checks(cfg, env)
    checks += [_calendar(cfg, http_get), _yield(cfg, yield_fetcher, now),
               _groq(cfg, env, http_get), _telegram(cfg, env, http_post),
               _dashboard_port(cfg)]
    checks += _mt5(cfg, connector_factory)
    return checks


def format_report(checks: List[Check]) -> str:
    lines = []
    for c in checks:
        lines.append(f"{_MARK[c.status]} {c.name}: {c.detail}")
        if c.hint and c.status in (WARN, FAIL):
            lines.append(f"         -> {c.hint}")
    fails = sum(c.status == FAIL for c in checks)
    warns = sum(c.status == WARN for c in checks)
    lines.append("")
    lines.append("READY: nothing blocking." if not fails else
                 f"NOT READY: {fails} problem(s) must be fixed first.")
    if warns:
        lines.append(f"{warns} warning(s): the bot runs, but some features are reduced.")
    return "\n".join(lines)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="GoldBot setup doctor")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--secrets", default=DEFAULT_PATH)
    parser.add_argument("--skip-mt5", action="store_true", help="skip the MT5 checks")
    args = parser.parse_args()

    import logging
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)   # its errors are reported below
    load_secrets_file(args.secrets)
    cfg = load_config(args.config if os.path.exists(args.config) else None)

    factory: Optional[Callable] = None
    if not args.skip_mt5 and sys.platform == "win32":
        from .mt5_connector import MT5Connector

        def factory():  # noqa: F811 - defined only on Windows
            return MT5Connector(cfg.mt5, cfg.execution)
    checks = run_checks(cfg, connector_factory=factory)
    print(format_report(checks))
    return 1 if any(c.status == FAIL for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
