"""Entry point: python run_bot.py --config config.json"""

from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from goldbot.config import AppConfig, load_config
from goldbot.dashboard import DashboardServer
from goldbot.macro_engine import MacroDataEngine
from goldbot.mt5_connector import MT5Connector
from goldbot.news_filter import NewsFilterEngine
from goldbot.notifier import TelegramNotifier
from goldbot.orchestrator import BotOrchestrator
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


def build_bot(cfg: AppConfig) -> BotOrchestrator:
    state = StateStore(cfg.state_file)
    connector = MT5Connector(cfg.mt5, cfg.execution)
    macro = MacroDataEngine(cfg.strategy)
    news = NewsFilterEngine(cfg.news)
    risk = RiskManager(cfg.risk, connector, state)
    sentiment = SentimentEngine(cfg.ai_news)
    notifier = TelegramNotifier(cfg.alerts)
    return BotOrchestrator(cfg, connector, macro, news, risk, state,
                           sentiment=sentiment, notifier=notifier)


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

    bot = build_bot(cfg)
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
