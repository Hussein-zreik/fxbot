"""riskctl (human-only resets), startup wiring and the doctor's risk checks."""

import os
from datetime import date

import pytest

import run_bot
from goldbot import riskctl
from goldbot.config import AppConfig
from goldbot.doctor import FAIL, OK, WARN, run_checks
from goldbot.risk.suite import build_suite
from tests.test_doctor import ENV, NOW, web, yields, Resp


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def ctl(*args):
    return riskctl.main(list(args))


def test_status_prints_the_rulebook_state(home, capsys):
    assert ctl("status") == 0
    out = capsys.readouterr().out
    assert "Stage: demo" in out and "0.25%" in out and "Active halts: none" in out


def test_reset_kill_needs_a_note_and_the_flag_removed(home, capsys):
    suite = build_suite(AppConfig())
    from datetime import datetime, timezone
    suite.kill.trip("PNL_DROP", datetime.now(timezone.utc))
    assert ctl("reset-kill", "--note", "") == 1
    os.makedirs("ops")
    open("ops/KILL", "w").close()
    assert ctl("reset-kill", "--note", "looked at it", "--author", "H") == 1
    os.remove("ops/KILL")
    assert ctl("reset-kill", "--note", "Broker confirmed a feed glitch", "--author", "H") == 0
    assert not build_suite(AppConfig()).kill.is_killed()
    text = open("CHANGELOG.md", encoding="utf-8").read()
    assert "KILL RESET" in text and "feed glitch" in text and "Author: H" in text


def test_reset_ladder_and_clear_halt(home):
    from datetime import datetime, timezone
    suite = build_suite(AppConfig())
    now = datetime.now(timezone.utc)
    suite.ladder.update(10_000.0, now)
    suite.ladder.update(9_200.0, now)
    suite.store.add_halt("HALT", "retcode 10019: no money", now)
    assert ctl("reset-ladder", "--note", " ") == 1
    assert ctl("reset-ladder", "--note", "Reviewed: slippage", "--author", "H") == 0
    assert ctl("clear-halt", "--note", "Account topped up", "--author", "H") == 0
    again = build_suite(AppConfig())
    assert again.ladder.risk_cap_pct() == 0.25 and not again.store.active_halts()
    assert "HALT CLEARED" in open("CHANGELOG.md", encoding="utf-8").read()


def test_a_reset_in_another_process_clears_the_running_bots_counters(home):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    bot_suite = build_suite(AppConfig())
    for i in range(3):
        bot_suite.kill.record_order_result(now, ok=False, retcode=10006)
    assert bot_suite.kill.check(now, 0, False) == "ORDER_ERRORS"
    bot_suite.kill.trip("ORDER_ERRORS", now)
    build_suite(AppConfig()).kill.reset("Broker fixed", "H", now + timedelta(minutes=1))
    assert bot_suite.kill.check(now + timedelta(minutes=2), 0, False) is None


# ------------------------------------------------------------------ startup wiring
def test_mode_defaults_to_paper_and_rejects_nonsense(monkeypatch):
    monkeypatch.delenv("MODE", raising=False)
    assert run_bot.resolve_mode() == "paper"
    monkeypatch.setenv("MODE", "LIVE")
    assert run_bot.resolve_mode() == "live"
    monkeypatch.setenv("MODE", "yolo")
    assert run_bot.resolve_mode() == "paper"


def test_the_live_gate_closure_reads_the_confirm_file(home, monkeypatch):
    monkeypatch.setenv("MODE", "live")
    cfg = AppConfig()
    cfg.mt5.login = 777
    today = date.today().isoformat()
    gate = run_bot.make_live_gate(cfg, typed="LIVE 777")
    assert not gate(777).ok                                    # no LIVE_CONFIRM yet
    open("LIVE_CONFIRM", "w").write(today)
    from datetime import datetime, timezone
    if date.today() != datetime.now(timezone.utc).date():      # rare: local vs UTC midnight
        open("LIVE_CONFIRM", "w").write(datetime.now(timezone.utc).date().isoformat())
    assert gate(777).ok
    assert not gate(778).ok                                    # wrong account
    assert not run_bot.make_live_gate(cfg, typed="")(777).ok   # nothing typed


def test_the_bot_refuses_to_start_if_a_limit_is_loosened(home):
    cfg = AppConfig()
    cfg.risk.overrides = {"max_trades_per_day": 9}
    with pytest.raises(ValueError):
        run_bot.build_bot(cfg)


def test_allow_live_trading_follows_the_mode(home, monkeypatch):
    cfg = AppConfig()
    try:
        run_bot.build_bot(cfg, mode="paper")
    except RuntimeError:                      # MetaTrader5 is Windows-only
        pass
    assert cfg.execution.allow_live_trading is False


# ------------------------------------------------------------------ doctor
def doctor(cfg=None, env=ENV):
    return {c.name: c for c in run_checks(
        cfg or AppConfig(), env=env, http_get=web, http_post=lambda *a, **k: Resp(),
        yield_fetcher=yields, now=NOW)}


def test_doctor_reports_the_rulebook_and_the_missing_feed(home):
    checks = doctor()
    assert checks["Risk rulebook"].status == OK
    assert "0.25% per trade" in checks["Risk rulebook"].detail
    assert checks["Mode"].status == OK
    assert checks["Independent price feed"].status == WARN
    assert checks["Kill flag"].status == OK


def test_doctor_flags_a_loosened_limit_and_a_kill_flag(home):
    cfg = AppConfig()
    cfg.risk.overrides = {"daily_loss_limit_pct": 5.0}
    assert doctor(cfg)["Risk rulebook"].status == FAIL
    os.makedirs("ops")
    open("ops/KILL", "w").close()
    assert doctor()["Kill flag"].status == FAIL
    assert doctor(env={**ENV, "MODE": "live"})["Mode"].status == WARN
