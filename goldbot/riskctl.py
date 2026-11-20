"""Human-only risk actions:  python -m goldbot.riskctl <command>

  status        show stage, limits, ladder, halts, streak
  reset-kill    clear a kill-switch trip   (needs --note; remove ops/KILL first)
  reset-ladder  clear an L2/L3 latch       (needs --note; resumes at 0.25 % risk)
  clear-halt    clear a broker-code halt   (needs --note)

Every reset appends a signed entry to CHANGELOG.md (CLAUDE.md section 16). The
running bot reads the same SQLite file, so a reset takes effect within seconds.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from typing import List, Optional

from .config import load_config
from .risk.changelog import append_entry
from .risk.suite import RiskSuite, build_suite
from .secrets_file import DEFAULT_PATH, load_secrets_file


def _status(suite: RiskSuite) -> str:
    lim, store = suite.limits, suite.store
    streak = suite.ledger.streak()
    halts = store.active_halts()
    lines = [
        f"Stage: {lim.stage}   per-trade risk {lim.per_trade_cap()}%   "
        f"(post-L2 cap: {suite.ladder.risk_cap_pct() or 'none'})",
        f"Equity high-water mark: {store.get('hwm', 'n/a')}   "
        f"ladder level: L{store.get('ladder_level', 0)}",
        f"Daily loss limit {lim.daily_loss_limit_pct}%   weekly {lim.weekly_loss_limit_pct}%   "
        f"max trades/day {lim.max_trades_per_day}",
        f"Losing streak: {streak.consecutive_losses}"
        f"{' (risk halved until +2R recovered)' if streak.halved else ''}",
        "Active halts: " + ("; ".join(f"{h['kind']}: {h['reason']}" for h in halts)
                            or "none"),
    ]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="GoldBot risk controls")
    parser.add_argument("command", choices=["status", "reset-kill", "reset-ladder", "clear-halt"])
    parser.add_argument("--note", default="", help="why it is safe to resume (required)")
    parser.add_argument("--author", default=os.getenv("USERNAME") or os.getenv("USER") or "",
                        help="who is signing the reset")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--secrets", default=DEFAULT_PATH)
    args = parser.parse_args(argv)

    load_secrets_file(args.secrets)
    cfg = load_config(args.config if os.path.exists(args.config) else None)
    suite = build_suite(cfg)
    now = datetime.now(timezone.utc)
    try:
        if args.command == "status":
            print(_status(suite))
        elif args.command == "reset-kill":
            suite.kill.reset(args.note, args.author or "unknown", now)
            print("Kill switch cleared. Write down what you learned in CHANGELOG.md.")
        elif args.command == "reset-ladder":
            suite.ladder.reset(args.note, args.author or "unknown", now)
            print("Ladder cleared. Risk is capped at 0.25% until you change it.")
        else:
            if not args.note.strip():
                raise ValueError("a note is required to clear a halt")
            suite.store.clear_halts("HALT", args.note.strip(), now)
            append_entry(cfg.risk.changelog_file, "HALT CLEARED", args.author or "unknown",
                         args.note.strip(), "Trading halt lifted by a human", now)
            print("Halt cleared.")
    except (ValueError, RuntimeError) as exc:
        print(f"Not done: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
