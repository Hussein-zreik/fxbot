"""Local secrets file (``secrets.env``) loaded into the environment at startup.

Why a file instead of ``setx``: environment variables set with ``setx`` only
reach NEW terminals, are easy to mistype, and are not seen by every way the
bot may be started. One private file next to ``config.json`` avoids all of
that. Real environment variables always win over the file, so power users can
still override anything.

Format (one ``KEY=value`` per line, ``#`` starts a comment)::

    GROQ_API_KEY=gsk_...
    TELEGRAM_BOT_TOKEN=123456:ABC...

The file is never committed (see ``.gitignore``) and, on Windows, the
installer restricts it to your own account. Values are never logged.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, List

log = logging.getLogger(__name__)

DEFAULT_PATH = "secrets.env"
_HEADER = "# GoldBot secrets - keep this file private. Never share or commit it.\n"


def parse_secrets(text: str) -> Dict[str, str]:
    """Parse KEY=value lines; ignores blanks and comments, strips quotes."""
    out: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip().lstrip("﻿")  # tolerate a BOM from Notepad
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key.replace("_", "").isalnum():
            out[key] = value
    return out


def read_secrets(path: str = DEFAULT_PATH) -> Dict[str, str]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return parse_secrets(p.read_text(encoding="utf-8"))
    except OSError as exc:
        log.warning("Could not read %s: %s", path, type(exc).__name__)
        return {}


def load_secrets_file(path: str = DEFAULT_PATH, override: bool = False) -> List[str]:
    """Copy the file's values into ``os.environ``; returns the key names set."""
    loaded = []
    for key, value in read_secrets(path).items():
        if value and (override or not os.environ.get(key)):
            os.environ[key] = value
            loaded.append(key)
    return loaded


def update_secrets_file(path: str, updates: Dict[str, str]) -> None:
    """Merge ``updates`` into the file (atomic write, owner-only permissions)."""
    for key, value in updates.items():
        if "\n" in value or "\r" in value:
            raise ValueError(f"secret {key} must be a single line")
    merged = read_secrets(path)
    merged.update({k: v for k, v in updates.items() if v})
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(_HEADER + "".join(f"{k}={v}\n" for k, v in merged.items()),
                   encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)  # effective on POSIX; Windows uses icacls (installer)
    except OSError:
        pass
    os.replace(tmp, p)
