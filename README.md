# GoldBot — Inter-Market XAUUSD Trading System for MetaTrader 5

A Python bot for XAUUSD (gold) that runs through the official `MetaTrader5`
library. It scores four independent signal blocks on a -100 to +100 scale.
It trades only when the macro picture agrees and price retests VWAP, and it
places every order with ATR-based stops and fixed 1% risk sizing.

> **Risk notice.** This is software, not financial advice. The rules are
> reasonable hypotheses, but nobody has backtested them. Run it in dry-run
> first, then on a demo account for several weeks, and review the signal
> journal before any real money is involved.

---

## 1. Strategy: the score matrix

| Block | Source | Rule | Points |
|---|---|---|---|
| **A. Yield** | `^TNX` (yfinance), 5-min | 10Y yield change over 15 min ≥ **+2 bp** → bearish gold; ≤ **−2 bp** → bullish. 1h z-score is logged (optional gate). Data older than 30 min scores 0. | ±30 |
| **B1. DXY** | MT5 `DXY`/`USDX` or `1/EURUSD`, M15 | USD closes above its 20-bar Donchian high → bearish gold; below its low → bullish. | ±30 |
| **B2. Silver** | MT5 `XAUUSD` + `XAGUSD`, M15 | Gold **and** silver break the 20-bar Donchian in the same direction, USD moves the other way, and the 30-bar return correlation R ≥ 0.6 → ±30. Gold breaks but silver doesn't → **divergence**: 0 points, and entries in that direction are blocked. | ±30 |
| **D. VWAP** | MT5 `XAUUSD`, M5 | Close inside `[VWAP, VWAP + 1×ATR]` for a long, `[VWAP − 1×ATR, VWAP]` for a short. Counts only in the direction of the macro bias. | ±10 |

**Entry:** total ≥ **+60** → BUY, ≤ **−60** → SELL. The VWAP retest is also a
hard gate. In practice an entry needs **at least two macro blocks agreeing,
plus the VWAP retest**.

**Gates that block entries even with a valid score:**
- High-impact USD news blackout: 30 min before to 15 min after the release.
- Maximum open positions reached (default 1).
- Post-exit cooldown (default 15 min).
- Outside session hours (default 07:00–20:00 UTC) or on a non-trading weekday.
- Spread too wide (default 60 points = $0.60).
- Daily loss guardrail triggered.

**Before high-impact news, for open trades:**
- **In profit** → SL moves to breakeven + 10 points.
- **Losing** → 50% of the position is closed.
- **Too small to split** → closed fully. Configurable with `news.close_fully_if_unsplittable`.

**Risk:**

```
lots = (equity × 1%) / loss_per_lot
SL   = 1.5 × ATR(14)
TP   = 3.0 × ATR(14)   (1:2 reward-to-risk)
```

- `loss_per_lot` comes from the broker's own `order_calc_profit`. This handles contract size (100 oz vs 1 oz) and account currency. The fallback is `(SL distance / tick size) × tick value`.
- Lot size is always rounded **down**. If even the minimum lot would risk more than 1%, the trade is skipped rather than oversized.
- **Daily guardrail:** if equity falls 3% below the day's starting balance, the bot closes all positions and halts until 00:00 UTC. The starting balance is recorded at 00:00 UTC. This state is saved to disk, so a restart cannot reset it.

Entries are evaluated **once per closed M5 bar**, so live behaviour matches a
bar-by-bar backtest of the same rules. Every bar is logged to
`logs/signal_journal.csv` with each block's score and the reason for the
action taken.

---

## 2. Architecture

```
run_bot.py                 entry point, logging, wiring
goldbot/
  config.py                typed config (JSON + env vars), validated
  mt5_connector.py         MT5Connector   – connect/reconnect, symbols, bars, orders, dry-run, live-account lock
  macro_engine.py          MacroDataEngine – background-thread ^TNX fetch, ΔY15m, 1h z-score
  news_filter.py           NewsFilterEngine – ForexFactory calendar, cache, blackout state
  correlation_engine.py    CorrelationEngine – gold/silver/USD breakouts, correlation, divergence
  technical_engine.py      TechnicalEngine – session VWAP (00:00 UTC anchor), ATR
  signal_model.py          score combination and entry decision
  risk_manager.py          RiskManager – sizing, SL/TP, daily guardrail
  orchestrator.py          BotOrchestrator – 10-second main loop, news management, journal
  indicators.py            pure indicator functions
  state.py                 crash-safe persistent state (data/state.json)
tests/                     43 tests using a fake MT5 connector (run anywhere)
```

---

## 3. Dependencies

| Package | Why |
|---|---|
| `MetaTrader5` | Official MT5 Python API (**Windows only**, talks to the running terminal) |
| `pandas`, `numpy` | Bars, indicators |
| `yfinance` | 10-year Treasury yield (`^TNX`) |
| `requests` | Economic calendar download |
| `pytest`, `flake8` | Tests and lint (dev only) |

```bat
pip install -r requirements.txt
```

---

## 4. Windows VPS and MT5 setup checklist

### A. VPS
1. **Rent a Windows VPS** (Windows Server 2019/2022, ≥ 2 vCPU, 4 GB RAM).
   - Pick a data centre close to your broker's trade server (usually London or New York). Many brokers offer a free or discounted VPS.
2. **Connect by Remote Desktop** (on iPhone: the *Windows App* / Microsoft Remote Desktop).
3. **Keep the machine awake and on the right clock:**
   - Settings → Time & language → *Set time automatically* **On**.
   - Power plan → never sleep.
   - Windows Update → set *active hours*, so updates don't reboot it mid-session.

### B. MetaTrader 5 terminal
4. **Install MT5 from your broker's website**, not the generic MetaQuotes build, so the broker's servers are preconfigured.
5. **Log in to your demo account** (File → Login to Trade Account) and tick *Save password*.
6. **Enable Algo Trading:**
   - Click the **Algo Trading** toolbar button until it turns green.
   - Tools → Options → Expert Advisors: tick *Allow algorithmic trading*.
   - Untick *Disable algorithmic trading when the account has been changed* and *…when the profile has been changed*.
7. **Find your broker's symbol names:**
   - Right-click Market Watch → *Show All*, or open *Symbols* (Ctrl+U) and search.
   - Gold may be listed as `XAUUSD`, `XAUUSD.a`, `XAUUSDm`, `XAUUSD+` or `GOLD`. The bot auto-detects suffixes (`XAUUSD` → `XAUUSD.a`).
   - If your broker calls it `GOLD`, put that in `symbols.gold`. Do the same check for silver (`XAGUSD`).
   - If the broker offers a dollar index (`DXY`, `USDX`, `DX`), set `symbols.dxy`. Otherwise leave it empty and the bot uses `1/EURUSD`.
8. **Check the contract specification** (right-click the symbol → *Specification*):
   - Contract size.
   - Minimum lot and lot step.
   - Stops level.
   - Trade mode must be *Full access*.

### C. Python and the bot
9. **Install Python 3.11 (64-bit)** from python.org and tick *Add python.exe to PATH*.
10. **Get the code and install dependencies:**
    ```bat
    git clone https://github.com/hussein-zreik/fxbot.git C:\goldbot
    cd C:\goldbot
    python -m venv .venv
    .venv\Scripts\activate
    pip install -r requirements.txt
    copy config.example.json config.json
    ```
11. **Edit `config.json`:**
    - Symbols (step 7).
    - `mt5.terminal_path`, e.g. `C:\\Program Files\\<Broker> MetaTrader 5\\terminal64.exe`. With this set, the bot can launch the terminal itself after a reboot.
    - Keep `mt5.login` at `0` to use the terminal's saved login. Or set credentials as environment variables, not in the file:
      ```bat
      setx MT5_LOGIN 12345678
      setx MT5_PASSWORD "your-password"
      setx MT5_SERVER "Broker-Demo"
      ```
12. **First run in dry-run** (`execution.dry_run: true`, the default):
    ```bat
    python run_bot.py --config config.json
    ```
    Check the log for:
    - `Connected: … account=DEMO`
    - `Resolved symbol XAUUSD -> …`
    - `Detected broker server time offset: UTC+3.0h`
    - `Calendar refreshed: N matching high-impact events`
    - One `BAR hh:mmZ | score … ` line every 5 minutes.
13. **Switch to demo trading:** set `execution.dry_run: false` and restart.
14. **Live trading later** needs **both** `dry_run: false` **and** `allow_live_trading: true`. If a real account is detected without the second flag, the bot forces itself back into dry-run and logs a CRITICAL message.

### D. Keep it running 24/5
15. Run it with **`start_bot.bat`**, which restarts the bot 30 seconds after any crash or exit.
16. **Start automatically after a reboot:**
    - Task Scheduler → *Create Task* → Trigger *At log on* → Action: `C:\goldbot\start_bot.bat`, "Start in" `C:\goldbot`.
    - Use Sysinternals **Autologon** so the VPS logs your user in after a reboot. MT5 needs a desktop session.
17. **Disconnect from Remote Desktop by closing the window.** Do **not** click *Sign out*, which kills the terminal and the bot.
18. **Logs** are in `logs\goldbot.log` (rotating), the per-bar journal is `logs\signal_journal.csv`, and state is in `data\state.json`.

### E. Monitoring from your iPhone
19. Install **MetaTrader 5** from the App Store.
    - Settings → *New Account* → search for your broker's server.
    - Log in with the same account number. Use the **investor (read-only) password** if you only want to watch and never trade by accident.
20. Bot trades show the comment `goldbot +70` (the score at entry) and magic number `20260926`. If you close a bot trade by hand, the bot notices and starts its cooldown.
21. **Push alerts:**
    - Copy your **MetaQuotes ID** from the iPhone app (Settings → Chat and messages).
    - Paste it into the desktop terminal: Tools → Options → Notifications, tick *Enable Push Notifications*, and enable trade-transaction notifications if your build shows the option.
22. For full log access, use Remote Desktop from the iPhone to the VPS.

---

## 5. Configuration reference (most-used keys)

| Key | Default | Meaning |
|---|---|---|
| `execution.dry_run` | `true` | Log orders instead of sending them |
| `execution.allow_live_trading` | `false` | Required for real-money accounts |
| `execution.max_open_positions` | `1` | Forced to 1 on netting accounts |
| `execution.session_filter_enabled` | `true` | Only enter between `session_start_utc` and `session_end_utc` |
| `execution.cooldown_minutes` | `15` | Wait after a position closes |
| `execution.trade_weekdays` | `[0..4]` | 0 = Monday |
| `strategy.buy_threshold` / `sell_threshold` | `60` / `-60` | Score needed to enter |
| `strategy.weight_*` | `30/30/30/10` | Block weights |
| `strategy.yield_trigger_bp` | `2.0` | Basis points in 15 min |
| `strategy.min_correlation` | `0.6` | Gold/silver return correlation needed for confluence |
| `risk.risk_per_trade` | `0.01` | 1% of equity |
| `risk.max_daily_loss` | `0.03` | 3% daily halt |
| `risk.max_spread_points` | `60` | Skip entries on wider spreads |
| `risk.close_all_account_positions_on_halt` | `false` | `true` = the guardrail also closes manual trades |
| `news.fail_closed` | `true` | No calendar data → no new entries |

---

## 6. Known limitations (read before going live)

- **Yield data latency.** Yahoo's `^TNX` is free and unofficial, and may lag or pause.
  - The bot scores the block 0 whenever the latest yield print is more than 30 minutes old, so bad data can never force a trade. It can, however, make the bot trade less.
  - FRED is not used: it publishes the 10Y only once a day, which is useless on 15 minutes.
- **Signal frequency.** The rules require two of the three macro blocks and a breakout or a 2 bp yield move at the same time. Expect few trades. Use the journal to see which gate blocks most often before loosening anything.
- **No built-in backtester.** The MT5 Strategy Tester cannot run Python, and a proper backtest also needs historical intraday yields. The journal CSV gives you a clean forward-test record instead.
- **Exits** happen through SL/TP, the pre-news rules and the guardrail. An opposite signal does not close a trade.
- **Calendar feed.** The ForexFactory JSON feed is rate-limited. The bot downloads it at most hourly and caches it on disk.

---

## 7. Development

```bash
pip install -r requirements-dev.txt
python -m pytest -q        # 43 tests, no MT5 needed
python -m flake8 --max-line-length 100 goldbot run_bot.py tests
```
