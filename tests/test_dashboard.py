import json
import urllib.error
import urllib.request

import pytest

from goldbot.config import DashboardConfig
from goldbot.dashboard import DashboardServer

TOKEN = "correct-horse-battery-staple"


class FakeBot:
    def __init__(self):
        self.commands = []

    def status(self):
        return {"mode": "DRY-RUN", "positions": [], "events": []}

    def submit_command(self, action):
        if action not in ("pause", "resume", "close_all"):
            raise ValueError("unknown command")
        self.commands.append(action)
        return f"'{action}' queued"


@pytest.fixture
def server():
    bot = FakeBot()
    srv = DashboardServer(DashboardConfig(port=0), bot, token=TOKEN, fail_delay_s=0)
    assert srv.start()
    yield srv, bot
    srv.stop()


def call(srv, path, token=TOKEN, body=None, method=None):
    req = urllib.request.Request(f"http://127.0.0.1:{srv.port}{path}", method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def test_page_served_with_strict_csp(server):
    srv, _ = server
    code, headers, body = call(srv, "/", token=None)
    assert code == 200
    csp = headers["Content-Security-Policy"]
    nonce = csp.split("'nonce-")[1].split("'")[0]
    html = body.decode()
    assert f'nonce="{nonce}"' in html and "{{NONCE}}" not in html
    # All dynamic text is rendered via textContent, never parsed as HTML.
    assert ".innerHTML" not in html and "insertAdjacentHTML" not in html
    assert "document.write" not in html
    assert headers["X-Frame-Options"] == "DENY"


def test_status_requires_token(server):
    srv, _ = server
    assert call(srv, "/api/status", token=None)[0] == 401
    assert call(srv, "/api/status", token="wrong-token-guess-123")[0] == 401
    code, _, body = call(srv, "/api/status")
    assert code == 200 and json.loads(body)["mode"] == "DRY-RUN"


def test_pause_command(server):
    srv, bot = server
    code, _, body = call(srv, "/api/command", body={"action": "pause"})
    assert code == 202 and json.loads(body)["ok"]
    assert bot.commands == ["pause"]


def test_close_all_needs_typed_confirmation(server):
    srv, bot = server
    assert call(srv, "/api/command", body={"action": "close_all"})[0] == 400
    assert call(srv, "/api/command", body={"action": "close_all", "confirm": "yes"})[0] == 400
    assert call(srv, "/api/command", body={"action": "close_all", "confirm": "CLOSE"})[0] == 202
    assert bot.commands == ["close_all"]


def test_commands_rejected_without_token(server):
    srv, bot = server
    assert call(srv, "/api/command", token=None, body={"action": "pause"})[0] == 401
    assert bot.commands == []


def test_unknown_command_and_bad_body(server):
    srv, _ = server
    assert call(srv, "/api/command", body={"action": "buy_everything"})[0] == 400
    assert call(srv, "/api/command", body={"action": "x" * 2000})[0] == 400


def test_close_all_can_be_disabled():
    bot = FakeBot()
    srv = DashboardServer(DashboardConfig(port=0, allow_close_all=False), bot,
                          token=TOKEN, fail_delay_s=0)
    assert srv.start()
    try:
        body = {"action": "close_all", "confirm": "CLOSE"}
        assert call(srv, "/api/command", body=body)[0] == 403
    finally:
        srv.stop()


def test_refuses_to_start_without_strong_token():
    assert not DashboardServer(DashboardConfig(port=0), FakeBot(), token="").start()
    assert not DashboardServer(DashboardConfig(port=0), FakeBot(), token="short").start()
