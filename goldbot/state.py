"""Crash-safe persistent state (daily guardrail, cooldowns, news handling).

Persisting this matters: if the bot restarts mid-day after a 2.5 % loss, it
must NOT reset its daily starting balance and grant itself a fresh 3 %.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict

log = logging.getLogger(__name__)


class StateStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.data: Dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.error("State file unreadable (%s); starting with empty state", exc)
            self.data = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.save()

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2, default=str),
                           encoding="utf-8")
            os.replace(tmp, self.path)  # atomic on Windows and POSIX
        except OSError as exc:
            log.error("Could not persist state: %s", exc)
