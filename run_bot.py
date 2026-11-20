"""Entry point: python run_bot.py --config config.json"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from goldbot.config import AppConfig, load_config
from goldbot.clock import beirut_time
from goldbot.dashboard import DashboardServer
from goldbot.macro_engine import MacroDataEngine
from goldbot.mt5_connector import MT5Connector
from goldbot.news_filter import NewsFilterEngine
from goldbot.notifier import TelegramNotifier
from goldbot.orchestrator import BotOrchestrator
from goldbot.risk.livegate import GateResult, check_live_gate
from goldbot.risk.suite import build_suite
from goldbot.risk_manager import RiskManager
from goldbot.secrets_file import DEFAULT_PATH, load_secrets_file
from goldbot.sentiment_engine import SentimentEngine
from goldbot.state import StateStore


def setup_logging(cfg: AppConfig) -> None:
    Path(cfg.log_dir).mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(cfg.log_level.upper())
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logfile = RotatingFileHandler(Path(cfg.log_dir) / "goldbot.log",
                                  maxBytes=10_000_000, backupCount=10, encoding="utf-8")
    logfile.setFormatter(fmt)
    root.handlers[:] = [console, logfile]
    # Third-party libraries are chatty at INFO.
    for noisy in ("yfinance", "urllib3", "peewee"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def resolve_mode() -> str:
    mode = os.getenv("MODE", "paper").strip().lower() or "paper"
    if mode not in ("paper", "live"):
        logging.getLogger("goldbot").warning("Unknown MODE=%r - using paper", mode)
        return "paper"
    return mode


def make_live_gate(cfg: AppConfig, typed: str):
    """Closure the orchestrator calls once the real account number is known."""
    def gate(account_login: int) -> GateResult:
        now = datetime.now(timezone.utc)
        confirm = None
        try:
            with open(cfg.risk.live_confirm_file, "r", encoding="utf-8") as fh:
                confirm = fh.read()
        except OSError:
            pass
        return check_live_gate(env=os.environ, account_login=account_login,
                               expected_login=cfg.mt5.login or None, utc_today=now.date(),
                               beirut_today=beirut_time(now).date(), confirm_text=confirm,
                               typed=typed or None)
    return gate


def build_bot(cfg: AppConfig, mode: str = "paper", typed: str = "") -> BotOrchestrator:
    state = StateStore(cfg.state_file)
    suite = build_suite(cfg)                       # raises if limits.yaml is looser than caps
    # Real accounts stay locked unless MODE=live; the full four-part gate is checked later.
    cfg.execution.allow_live_trading = mode == "live"
    connector = MT5Connector(cfg.mt5, cfg.execution)
    macro = MacroDataEngine(cfg.strategy)
    news = NewsFilterEngine(cfg.news)
    risk = RiskManager(cfg.risk)
    sentiment = SentimentEngine(cfg.ai_news)
    notifier = TelegramNotifier(cfg.alerts)
    return BotOrchestrator(cfg, connector, macro, news, risk, state, suite,
                           sentiment=sentiment, notifier=notifier, mode=mode,
                           live_gate=make_live_gate(cfg, typed))


def main() -> int:
    parser = argparse.ArgumentParser(description="GoldBot XAUUSD inter-market trader")
    parser.add_argument("--config", default="config.json",
                        help="path to JSON config (default: config.json)")
    parser.add_argument("--secrets", default=DEFAULT_PATH,
                        help="private key file (default: secrets.env)")
    args = parser.parse_args()

    # Windows consoles default to a legacy code page that cannot print emoji.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    loaded = load_secrets_file(args.secrets)
    cfg = load_config(args.config if Path(args.config).exists() else None)
    setup_logging(cfg)
    log = logging.getLogger("goldbot")
    if loaded:
        log.info("Loaded %d secret(s) from %s", len(loaded), args.secrets)
    if not Path(args.config).exists():
        log.warning("Config %s not found - running with built-in defaults", args.config)

    mode = resolve_mode()
    typed = ""
    if mode == "live":
        if sys.stdin and sys.stdin.isatty():
            typed = input("MODE=live. To trade real money type  LIVE <your account number>  : ")
        else:
            log.warning("MODE=live but there is no keyboard to type the confirmation: the "
                        "bot will stay locked out of live trading.")
    log.info("Mode: %s%s", mode, " (default)" if mode == "paper" else "")
    try:
        bot = build_bot(cfg, mode, typed.strip())
    except ValueError as exc:                      # LimitsError is a ValueError
        log.critical("Refusing to start: %s", exc)
        return 1
    dashboard = DashboardServer(cfg.dashboard, bot)
    dashboard.start()
    try:
        bot.run()
    except RuntimeError as exc:
        log.critical("Startup failed: %s", exc)
        return 1
    finally:
        dashboard.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
