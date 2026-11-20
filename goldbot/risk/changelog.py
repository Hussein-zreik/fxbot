"""Append signed entries to CHANGELOG.md (CLAUDE.md section 16)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

HEADER = "# CHANGELOG\n\nEvery change and every manual risk action is recorded here.\n"


def append_entry(path: str, title: str, author: str, reason: str, risk_impact: str,
                 now: datetime, tests: str = "n/a (operational action)",
                 approval: str = "") -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = p.read_text(encoding="utf-8") if p.exists() else HEADER
    text += (f"\n## {now:%Y-%m-%d %H:%M} UTC - {title}\n"
             f"- Author: {author}\n- Reason: {reason}\n- Risk impact: {risk_impact}\n"
             f"- Tests run: {tests}\n- Approval: {approval or author}\n")
    p.write_text(text, encoding="utf-8")
