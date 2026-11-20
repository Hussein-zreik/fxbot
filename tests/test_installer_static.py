"""Static checks for install.ps1 (PowerShell cannot run in CI on Linux).

They catch the failures that bite hardest on a fresh server: non-ASCII text
(garbled by Windows PowerShell 5.1), unbalanced brackets or quotes, and the
script pointing at files or modules that do not exist.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = (REPO / "install.ps1").read_text(encoding="utf-8")


def test_ascii_only():
    bad = [(i + 1, line) for i, line in enumerate(SCRIPT.splitlines())
           if not line.isascii()]
    assert not bad, f"non-ASCII characters break PowerShell 5.1: {bad[:3]}"


def test_brackets_and_quotes_balance():
    for open_c, close_c in ("()", "{}", "[]"):
        assert SCRIPT.count(open_c) == SCRIPT.count(close_c), f"unbalanced {open_c}{close_c}"
    for n, line in enumerate(SCRIPT.splitlines(), 1):
        code = line.split(" #")[0] if line.lstrip().startswith("#") is False else ""
        assert code.count('"') % 2 == 0, f"odd number of double quotes on line {n}: {line}"


def test_no_scope_colon_trap_in_strings():
    # "$name:" inside a double-quoted string is parsed as a drive/scope reference.
    for n, line in enumerate(SCRIPT.splitlines(), 1):
        for match in re.finditer(r'"[^"]*"', line):
            assert not re.search(r"(?<!\()\$[A-Za-z_]+:", match.group(0)), \
                f"line {n}: use $($var): inside strings"


def test_modules_and_files_it_uses_exist():
    for module in re.findall(r"-m (goldbot\.\w+)", SCRIPT):
        assert (REPO / (module.replace(".", "/") + ".py")).exists(), module
    for name in ("requirements.txt", "start_bot.bat", "run_bot.py", "config.example.json"):
        assert name in SCRIPT or (REPO / name).exists()
        assert (REPO / name).exists(), name


def test_robocopy_does_not_exclude_needed_folders():
    excluded = re.search(r"/XD ([^/]+?) /XF", SCRIPT).group(1).split()
    shipped_dirs = {p.name for p in (REPO / "goldbot").rglob("*") if p.is_dir()}
    assert not set(excluded) & (shipped_dirs - {"__pycache__"})
    assert (REPO / "goldbot" / "dashboard.html").exists()     # shipped static asset


def test_keeps_user_files_on_update():
    assert "/XF config.json secrets.env" in SCRIPT
    assert "/XD" in SCRIPT and "data" in SCRIPT and "logs" in SCRIPT


def test_batch_file_is_crlf_and_task_safe():
    raw = (REPO / "start_bot.bat").read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    assert b"timeout" not in raw          # fails under Task Scheduler (no console input)
    assert b"--secrets secrets.env" in raw
