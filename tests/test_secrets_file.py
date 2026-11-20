import os

import pytest

from goldbot.secrets_file import (load_secrets_file, parse_secrets, read_secrets,
                                  update_secrets_file)


def test_parse_handles_comments_quotes_bom_and_equals_in_value():
    text = ('\ufeff# comment\n\nGROQ_API_KEY = "gsk_abc"\nTELEGRAM_BOT_TOKEN=123:AB=C\n'
            "SINGLE='x y'\nbad line\n1 BAD=2\n")
    assert parse_secrets(text) == {"GROQ_API_KEY": "gsk_abc",
                                   "TELEGRAM_BOT_TOKEN": "123:AB=C", "SINGLE": "x y"}


def test_load_does_not_override_real_environment(tmp_path, monkeypatch):
    f = tmp_path / "secrets.env"
    f.write_text("A_KEY=from_file\nB_KEY=from_file\n")
    monkeypatch.setenv("A_KEY", "from_env")
    monkeypatch.delenv("B_KEY", raising=False)
    assert load_secrets_file(str(f)) == ["B_KEY"]
    assert os.environ["A_KEY"] == "from_env" and os.environ["B_KEY"] == "from_file"
    monkeypatch.delenv("B_KEY")


def test_missing_file_is_fine(tmp_path):
    assert read_secrets(str(tmp_path / "nope.env")) == {}
    assert load_secrets_file(str(tmp_path / "nope.env")) == []


def test_update_merges_and_is_private(tmp_path):
    f = tmp_path / "sub" / "secrets.env"
    update_secrets_file(str(f), {"A": "1", "B": "2"})
    update_secrets_file(str(f), {"B": "3", "C": "", "D": "4"})   # blank = keep nothing new
    assert read_secrets(str(f)) == {"A": "1", "B": "3", "D": "4"}
    if os.name == "posix":
        assert oct(f.stat().st_mode & 0o777) == "0o600"
    assert f.read_text().startswith("# GoldBot secrets")


def test_multiline_secret_rejected(tmp_path):
    with pytest.raises(ValueError):
        update_secrets_file(str(tmp_path / "s.env"), {"A": "x\ny"})
