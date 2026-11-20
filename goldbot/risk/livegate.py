"""The four-part live gate (prime directive 6). Live trading needs ALL of:

  1. MODE=live in the environment
  2. the matching account number in config
  3. a LIVE_CONFIRM file containing today's date (UTC or Beirut date)
  4. a typed confirmation at startup:  LIVE <account number>

Anything missing keeps the bot in paper mode. The typed step cannot happen
under Task Scheduler, so an unattended restart can never go live by itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import List, Mapping, Optional


@dataclass
class GateResult:
    ok: bool
    missing: List[str] = field(default_factory=list)


def check_live_gate(*, env: Mapping[str, str], account_login: int,
                    expected_login: Optional[int], utc_today: date, beirut_today: date,
                    confirm_text: Optional[str], typed: Optional[str]) -> GateResult:
    missing: List[str] = []
    if env.get("MODE", "").strip().lower() != "live":
        missing.append("MODE: set MODE=live in the environment")
    if not expected_login or account_login != expected_login:
        missing.append("ACCOUNT: the account number in config must match the logged-in account")
    lines = (confirm_text or "").strip().splitlines()
    first_line = lines[0].strip() if lines else ""
    if first_line not in {utc_today.isoformat(), beirut_today.isoformat()}:
        missing.append("LIVE_CONFIRM: the file must contain today's date "
                       f"({utc_today.isoformat()})")
    if typed != f"LIVE {account_login}":
        missing.append(f"TYPED: type exactly 'LIVE {account_login}' at startup")
    return GateResult(not missing, missing)
