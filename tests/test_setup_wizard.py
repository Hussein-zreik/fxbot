import json
from pathlib import Path

import pytest

from goldbot.config import load_config
from goldbot.notifier import TelegramSetupError, find_chat_id
from goldbot.secrets_file import read_secrets
from goldbot.setup_wizard import detect_mt5_terminals, ensure_config, run_wizard

REPO = Path(__file__).resolve().parents[1]


class Resp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._p = status, payload or {}

    def json(self):
        return self._p


def updates(chat_id=555, name="Hussein"):
    return Resp(payload={"result": [{"message": {"chat": {"id": chat_id,
                                                          "first_name": name}}}]})


@pytest.fixture
def install(tmp_path):
    (tmp_path / "config.example.json").write_text(
        (REPO / "config.example.json").read_text(encoding="utf-8"), encoding="utf-8")
    return tmp_path


def scripted(answers):
    it = iter(answers)
    return lambda prompt="": next(it)


def test_detect_terminals(tmp_path):
    (tmp_path / "Broker MT5").mkdir()
    (tmp_path / "Broker MT5" / "terminal64.exe").write_text("x")
    (tmp_path / "Other").mkdir()
    assert detect_mt5_terminals([tmp_path, tmp_path / "missing"]) == [
        tmp_path / "Broker MT5" / "terminal64.exe"]


def test_ensure_config_creates_valid_config_without_bom(install):
    path = ensure_config(install, Path("C:/Broker/terminal64.exe"))
    assert not path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert json.loads(path.read_text(encoding="utf-8"))["mt5"]["terminal_path"] \
        == "C:/Broker/terminal64.exe" or "terminal64.exe" in path.read_text()
    assert load_config(str(path)).execution.dry_run is True      # still validates


def test_ensure_config_keeps_user_edits(install):
    path = ensure_config(install, None)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["execution"]["dry_run"] = False
    data["mt5"]["terminal_path"] = "D:/mine/terminal64.exe"
    path.write_text(json.dumps(data), encoding="utf-8")
    ensure_config(install, Path("C:/other/terminal64.exe"))
    again = json.loads(path.read_text(encoding="utf-8"))
    assert again["execution"]["dry_run"] is False
    assert again["mt5"]["terminal_path"] == "D:/mine/terminal64.exe"


def test_find_chat_id_errors_are_human():
    with pytest.raises(TelegramSetupError, match="rejected the token"):
        find_chat_id("bad", lambda *a, **k: Resp(401))
    with pytest.raises(TelegramSetupError, match="press Start"):
        find_chat_id("t", lambda *a, **k: Resp(payload={"result": []}))
    assert find_chat_id("t", lambda *a, **k: updates())[0] == 555


def test_full_wizard_happy_path(install):
    posts = []

    def post(url, json, timeout):
        posts.append(json["text"])
        return Resp()

    answers = scripted(["", "y"])      # "press Enter after hi", "send token to Telegram"
    out = []
    summary = run_wizard(
        install, ask=answers, ask_secret=scripted(["gsk_test", "123:TOKEN"]),
        http_get=lambda *a, **k: updates(), http_post=post, out=out.append,
        terminals=[Path("C:/Broker/terminal64.exe")])
    assert summary["groq"] and summary["telegram"] and summary["dashboard_token_created"]
    secrets_ = read_secrets(str(install / "secrets.env"))
    assert secrets_["GROQ_API_KEY"] == "gsk_test"
    assert secrets_["TELEGRAM_CHAT_ID"] == "555"
    assert len(secrets_["DASHBOARD_TOKEN"]) >= 16
    assert any("alerts are working" in p for p in posts)
    assert any(secrets_["DASHBOARD_TOKEN"] in p for p in posts)   # sent to the phone
    assert (install / "config.json").exists()


def test_wizard_skips_everything_and_rerun_keeps_values(install):
    first = run_wizard(install, ask=scripted([]), ask_secret=scripted(["", ""]),
                       out=lambda *a: None, terminals=[])
    assert not first["groq"] and not first["telegram"]
    token = read_secrets(str(install / "secrets.env"))["DASHBOARD_TOKEN"]
    second = run_wizard(install, ask=scripted([]), ask_secret=scripted(["", ""]),
                        out=lambda *a: None, terminals=[])
    assert not second["dashboard_token_created"]
    assert read_secrets(str(install / "secrets.env"))["DASHBOARD_TOKEN"] == token


def test_wizard_telegram_retry_then_give_up(install):
    calls = {"n": 0}

    def get(*a, **k):
        calls["n"] += 1
        return Resp(payload={"result": []})

    out = []
    summary = run_wizard(install, ask=scripted(["", "y", "", "n"]),
                         ask_secret=scripted(["", "123:TOKEN"]), http_get=get,
                         out=out.append, terminals=[])
    assert not summary["telegram"] and calls["n"] == 2
    assert "TELEGRAM_BOT_TOKEN" not in read_secrets(str(install / "secrets.env"))
