"""Phone dashboard: a tiny token-protected web server (standard library only).

* ``GET  /``             the mobile dashboard page (static; holds no data)
* ``GET  /api/status``   live bot snapshot as JSON            (token required)
* ``POST /api/command``  pause / resume / close_all            (token required)

Security model
--------------
* Binds to 127.0.0.1 by default and is meant to be reached privately through
  Tailscale (``tailscale serve``), so it is never exposed to the internet.
* Every API call needs ``Authorization: Bearer <token>``; the token comes from
  an environment variable and is compared in constant time. Without a token
  (or with one shorter than 16 characters) the dashboard refuses to start.
* "close_all" additionally requires the typed confirmation word ``CLOSE``.
* The page is served with a strict Content-Security-Policy (per-response
  nonce, no third-party scripts) and renders all text with ``textContent``,
  so a malicious news headline cannot inject script.
* The web thread never calls MT5: it reads the orchestrator's published
  snapshot and queues commands that the trading loop executes.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from .config import DashboardConfig

log = logging.getLogger(__name__)

_PAGE = Path(__file__).with_name("dashboard.html")
_MIN_TOKEN_LEN = 16
_MAX_BODY = 1024


class DashboardServer:
    def __init__(self, cfg: DashboardConfig, bot, token: Optional[str] = None,
                 fail_delay_s: float = 1.0):
        self.cfg = cfg
        self.fail_delay_s = fail_delay_s
        self.bot = bot
        self.token = token if token is not None else os.getenv(cfg.token_env, "")
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._page = _PAGE.read_text(encoding="utf-8")

    # ----------------------------------------------------------------- #
    def start(self) -> bool:
        if not self.cfg.enabled:
            return False
        if len(self.token) < _MIN_TOKEN_LEN:
            log.warning("Dashboard NOT started: set a secret of at least %d characters "
                        "in the %s environment variable", _MIN_TOKEN_LEN, self.cfg.token_env)
            return False
        handler = self._make_handler()
        try:
            self._httpd = ThreadingHTTPServer((self.cfg.host, self.cfg.port), handler)
        except OSError as exc:
            log.error("Dashboard could not bind %s:%s: %s", self.cfg.host, self.cfg.port, exc)
            return False
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="dashboard", daemon=True)
        self._thread.start()
        log.info("Dashboard on http://%s:%s (controls=%s, close_all=%s)",
                 self.cfg.host, self.port, self.cfg.allow_controls,
                 self.cfg.allow_close_all)
        return True

    @property
    def port(self) -> int:
        return self._httpd.server_address[1] if self._httpd else self.cfg.port

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()

    # ----------------------------------------------------------------- #
    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "GoldBot"
            sys_version = ""

            def log_message(self, fmt, *args):  # route to logging, quietly
                log.debug("dashboard %s - " + fmt, self.client_address[0], *args)

            # -- helpers ------------------------------------------------ #
            def _send(self, code: int, body: bytes, ctype: str, extra=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, payload) -> None:
                self._send(code, json.dumps(payload).encode("utf-8"),
                           "application/json; charset=utf-8")

            def _authorized(self) -> bool:
                header = self.headers.get("Authorization", "")
                supplied = header[7:] if header.startswith("Bearer ") else ""
                ok = hmac.compare_digest(supplied.encode(), server.token.encode())
                if not ok:
                    time.sleep(server.fail_delay_s)  # slow down token guessing
                    log.warning("Dashboard: rejected request from %s", self.client_address[0])
                return ok

            # -- routes ------------------------------------------------- #
            def do_GET(self):  # noqa: N802 - http.server naming
                path = self.path.split("?", 1)[0]
                if path in ("/", "/index.html"):
                    nonce = secrets.token_urlsafe(16)
                    csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; "
                           f"style-src 'nonce-{nonce}'; connect-src 'self'; "
                           "img-src 'self' data:; base-uri 'none'; "
                           "frame-ancestors 'none'; form-action 'none'")
                    body = server._page.replace("{{NONCE}}", nonce).encode("utf-8")
                    self._send(200, body, "text/html; charset=utf-8",
                               {"Content-Security-Policy": csp,
                                "X-Frame-Options": "DENY"})
                elif path == "/favicon.ico":
                    self.send_response(204)
                    self.end_headers()
                elif path == "/api/status":
                    if not self._authorized():
                        return self._json(401, {"error": "unauthorized"})
                    self._json(200, server.bot.status())
                else:
                    self._json(404, {"error": "not found"})

            def do_POST(self):  # noqa: N802
                if self.path.split("?", 1)[0] != "/api/command":
                    return self._json(404, {"error": "not found"})
                if not self._authorized():
                    return self._json(401, {"error": "unauthorized"})
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = -1
                if not 0 < length <= _MAX_BODY:
                    return self._json(400, {"error": "bad request body"})
                try:
                    data = json.loads(self.rfile.read(length))
                    action = str(data.get("action", ""))
                except (ValueError, AttributeError):
                    return self._json(400, {"error": "invalid JSON"})

                cfg = server.cfg
                if action in ("pause", "resume") and not cfg.allow_controls:
                    return self._json(403, {"error": "controls disabled in config"})
                if action == "close_all":
                    if not cfg.allow_close_all:
                        return self._json(403, {"error": "close-all disabled in config"})
                    if data.get("confirm") != "CLOSE":
                        return self._json(400, {"error": "type CLOSE to confirm"})
                try:
                    message = server.bot.submit_command(action)
                except ValueError as exc:
                    return self._json(400, {"error": str(exc)})
                log.warning("Dashboard command '%s' from %s", action, self.client_address[0])
                self._json(202, {"ok": True, "message": message})

        return Handler
