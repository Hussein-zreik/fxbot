"""Interactive setup wizard:  python -m goldbot.setup_wizard

Run by the installer after the code is in place. It

1. creates ``config.json`` from ``config.example.json`` (never overwrites yours),
2. finds the MetaTrader 5 terminal and records its path,
3. asks for the optional keys (Groq AI key, Telegram bot token), finds your
   Telegram chat automatically and sends a test message,
4. generates a dashboard token (and can send it to your Telegram chat so you
   can copy it on your phone),
5. saves all secrets to ``secrets.env`` (owner-only).

Every question can be skipped with Enter; re-running keeps what is already set.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import shutil
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional

import requests

from .config import AlertsConfig
from .notifier import TelegramNotifier, TelegramSetupError, find_chat_id
from .secrets_file import DEFAULT_PATH, read_secrets, update_secrets_file


def detect_mt5_terminals(roots: Optional[List[Path]] = None) -> List[Path]:
    """terminal64.exe files in the usual Program Files folders (one level deep)."""
    if roots is None:
        roots = [Path(os.environ[v]) for v in ("ProgramFiles", "ProgramFiles(x86)")
                 if os.environ.get(v)]
    found: List[Path] = []
    for root in roots:
        if root.is_dir():
            found.extend(sorted(root.glob("*/terminal64.exe")))
    return found


def ensure_config(install_dir: Path, terminal: Optional[Path]) -> Path:
    """Create config.json if missing and fill mt5.terminal_path if empty."""
    cfg_path = install_dir / "config.json"
    example = install_dir / "config.example.json"
    if not cfg_path.exists():
        if not example.exists():
            raise FileNotFoundError(f"{example} is missing - re-run the installer")
        shutil.copyfile(example, cfg_path)
    data = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    mt5_section = data.setdefault("mt5", {})
    if terminal and not mt5_section.get("terminal_path"):
        mt5_section["terminal_path"] = str(terminal)
    # UTF-8 without BOM: Python's json module rejects a BOM.
    cfg_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    return cfg_path


def _yes(answer: str, default: bool) -> bool:
    answer = answer.strip().lower()
    return default if not answer else answer.startswith("y")


def run_wizard(install_dir: Path, *, ask: Callable[[str], str] = input,
               ask_secret: Callable[[str], str] = getpass.getpass,
               http_get: Callable = requests.get, http_post: Callable = requests.post,
               out: Callable[..., None] = print,
               terminals: Optional[List[Path]] = None) -> Dict[str, object]:
    """Run the wizard; returns a summary (used by tests and the installer)."""
    summary: Dict[str, object] = {}
    secrets_path = str(install_dir / DEFAULT_PATH)
    existing = read_secrets(secrets_path)
    updates: Dict[str, str] = {}

    # 1-2. config + MT5 terminal
    found = detect_mt5_terminals() if terminals is None else terminals
    terminal: Optional[Path] = None
    if len(found) == 1:
        terminal = found[0]
    elif len(found) > 1:
        out("Several MetaTrader 5 installations found:")
        for i, path in enumerate(found, 1):
            out(f"  {i}. {path}")
        choice = ask("Which one is your broker's? Number: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(found):
            terminal = found[int(choice) - 1]
    cfg_path = ensure_config(install_dir, terminal)
    summary["config"] = str(cfg_path)
    summary["terminal"] = str(terminal) if terminal else None
    out(f"MetaTrader 5: {terminal}" if terminal else
        "MetaTrader 5 not found automatically - edit mt5.terminal_path in config.json.")

    # 3. Groq key
    if existing.get("GROQ_API_KEY"):
        out("Groq AI key: already saved.")
    else:
        out("\nAI news reading needs a FREE Groq key (console.groq.com -> API Keys).")
        key = ask_secret("Paste it (input hidden) or press Enter to skip: ").strip()
        if key:
            updates["GROQ_API_KEY"] = key
    summary["groq"] = bool(existing.get("GROQ_API_KEY") or updates.get("GROQ_API_KEY"))

    # 4. Telegram
    token = existing.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = existing.get("TELEGRAM_CHAT_ID", "")
    if token and chat_id:
        out("Telegram: already set up.")
    else:
        out("\nTelegram alerts: in Telegram message @BotFather -> /newbot -> copy the token.")
        token = token or ask_secret("Paste the bot token (hidden) or press Enter to skip: ").strip()
        for _ in range(3):
            if not token:
                break
            ask("Now open your bot in Telegram, press Start, send 'hi', then press Enter.")
            try:
                cid, name = find_chat_id(token, http_get)
            except TelegramSetupError as exc:
                out(f"  {exc}")
                if not _yes(ask("  Try again? [Y/n] "), True):
                    token = ""
                continue
            chat_id = str(cid)
            out(f"  Found your chat ({name}).")
            break
        else:
            token = chat_id = ""
        if token and chat_id:
            updates["TELEGRAM_BOT_TOKEN"], updates["TELEGRAM_CHAT_ID"] = token, chat_id
    notifier = None
    if token and chat_id:
        notifier = TelegramNotifier(AlertsConfig(), http_post=http_post,
                                    token=token, chat_id=chat_id)
        ok = notifier.deliver("GoldBot: Telegram alerts are working ✅") \
            if "TELEGRAM_CHAT_ID" in updates else True
        out("  Test message sent." if ok else "  Test message failed (see doctor).")
    summary["telegram"] = bool(token and chat_id)

    # 5. Dashboard token
    dash = existing.get("DASHBOARD_TOKEN", "")
    if dash:
        out("Dashboard token: already saved.")
    else:
        dash = secrets.token_urlsafe(18)  # 24 characters
        updates["DASHBOARD_TOKEN"] = dash
        out("\nYour dashboard token (you type it once on your phone):")
        out(f"\n    {dash}\n")
        if notifier and _yes(ask("Send it to your Telegram chat so you can copy it on your "
                                 "phone? It only works inside your private Tailscale "
                                 "network. [Y/n] "), True):
            sent = notifier.deliver(f"GoldBot dashboard token:\n{dash}")
            out("  Sent." if sent else "  Could not send - copy it from the screen.")
    summary["dashboard_token_created"] = "DASHBOARD_TOKEN" in updates

    if updates:
        update_secrets_file(secrets_path, updates)
    out(f"\nSecrets saved to {secrets_path}")
    return summary


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="GoldBot setup wizard")
    parser.add_argument("--dir", default=".", help="install directory (default: current)")
    args = parser.parse_args()
    try:
        run_wizard(Path(args.dir).resolve())
    except (FileNotFoundError, KeyboardInterrupt) as exc:
        print(f"Setup stopped: {exc or 'cancelled'}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
