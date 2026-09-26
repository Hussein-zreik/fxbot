"""Telegram push alerts (free, reliable on iPhone).

Setup (once):
  1. In Telegram, message @BotFather -> /newbot -> copy the bot token.
  2. Send any message (e.g. "hi") to your new bot.
  3. On the VPS:  setx TELEGRAM_BOT_TOKEN "123456:ABC..."
                  python -m goldbot.notifier --setup
     This finds your chat id, prints the setx command for it and sends a test.

Messages go out on a background thread with a queue, so a slow or failing
Telegram call never delays trading. Messages are sent as plain text (no
Markdown/HTML parsing), so news headlines cannot inject formatting or links.
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
import sys
import threading
import time
from typing import Callable, Dict, Optional

import requests

from .config import AlertsConfig

log = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"
_MAX_LEN = 4000  # Telegram limit is 4096 characters


class TelegramNotifier:
    def __init__(self, cfg: AlertsConfig, http_post: Callable = requests.post,
                 token: Optional[str] = None, chat_id: Optional[str] = None):
        self.cfg = cfg
        self._post = http_post
        self.token = token if token is not None else os.getenv(cfg.token_env, "")
        self.chat_id = chat_id if chat_id is not None else os.getenv(cfg.chat_id_env, "")
        self.active = bool(cfg.enabled and self.token and self.chat_id)
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue(maxsize=200)
        self._recent: Dict[str, float] = {}
        self._thread: Optional[threading.Thread] = None
        if cfg.enabled and not self.active:
            log.warning("Telegram alerts disabled: set %s and %s",
                        cfg.token_env, cfg.chat_id_env)

    # ----------------------------------------------------------------- #
    def start(self) -> None:
        if not self.active or (self._thread and self._thread.is_alive()):
            return
        self._thread = threading.Thread(target=self._run, name="telegram", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        if self._thread and self._thread.is_alive():
            self._queue.put(None)
            self._thread.join(timeout=timeout)

    def send(self, text: str, category: str) -> bool:
        """Queue a message if its category is enabled. Never blocks, never raises."""
        if not self.active or category not in self.cfg.categories:
            return False
        now = time.monotonic()
        if now - self._recent.get(text, -1e9) < self.cfg.dedupe_seconds:
            return False  # identical message just sent
        self._recent[text] = now
        if len(self._recent) > 500:
            self._recent = {k: v for k, v in self._recent.items()
                            if now - v < self.cfg.dedupe_seconds}
        try:
            self._queue.put_nowait(f"{self.cfg.prefix} {text}"[:_MAX_LEN])
            return True
        except queue.Full:
            log.warning("Telegram queue full - alert dropped")
            return False

    # ----------------------------------------------------------------- #
    def _run(self) -> None:
        while True:
            text = self._queue.get()
            if text is None:
                return
            self.deliver(text)

    def deliver(self, text: str) -> bool:
        """Send one message now, with retries. Used by the worker and --setup."""
        url = _API.format(token=self.token, method="sendMessage")
        body = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}
        for attempt in range(3):
            try:
                resp = self._post(url, json=body, timeout=10)
                if resp.status_code == 429:
                    wait = resp.json().get("parameters", {}).get("retry_after", 5)
                    time.sleep(min(float(wait), 30))
                    continue
                if resp.status_code >= 400:
                    # Never log the URL: it contains the bot token.
                    log.warning("Telegram rejected message (HTTP %s)", resp.status_code)
                    return False
                return True
            except requests.RequestException as exc:
                log.warning("Telegram send failed (attempt %d): %s",
                            attempt + 1, type(exc).__name__)
                time.sleep(2 * (attempt + 1))
        return False


# --------------------------------------------------------------------------- #
# One-time setup helper:  python -m goldbot.notifier --setup
# --------------------------------------------------------------------------- #
def _setup() -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("First set your bot token:  setx TELEGRAM_BOT_TOKEN \"<token from @BotFather>\"")
        print("then open a NEW terminal and run this again.")
        return 1
    resp = requests.get(_API.format(token=token, method="getUpdates"), timeout=15)
    if resp.status_code != 200:
        print(f"Telegram rejected the token (HTTP {resp.status_code}). Check it with @BotFather.")
        return 1
    chats = {}
    for upd in resp.json().get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if "id" in chat:
            chats[chat["id"]] = chat.get("first_name") or chat.get("title") or "?"
    if not chats:
        print("No messages found. Open Telegram, send 'hi' to your bot, then run this again.")
        return 1
    chat_id, name = list(chats.items())[-1]
    print(f"Found chat with {name}: {chat_id}")
    print(f'Run:  setx TELEGRAM_CHAT_ID "{chat_id}"')
    ok = TelegramNotifier(AlertsConfig(), token=token, chat_id=str(chat_id)).deliver(
        "GoldBot: Telegram alerts are working ✅")
    print("Test message sent." if ok else "Test message failed - see log.")
    return 0 if ok else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GoldBot Telegram helper")
    parser.add_argument("--setup", action="store_true", help="find chat id and send a test")
    if parser.parse_args().setup:
        sys.exit(_setup())
    parser.print_help()
