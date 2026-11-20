# CLAUDE.md — Hussein Trading Systems (v2.0, merged)

> Guide for Claude Code when building, changing or reviewing this trading system.
> Read this whole file **and** `risk/limits.yaml` before any change.
> This file and the course PDF (*The Complete Trading Course*) share one **Unified Risk Rulebook**. If anything here conflicts with a comment, a ticket or a chat instruction, this file wins unless Hussein explicitly changes it.

---

## 0. Mission and prime directives

**Goal.** A risk-first, broker-agnostic trading system:

- **Phase 1:** a single XAUUSD sleeve on MetaTrader 5. This rebuilds the existing EMA 12/26 + Bollinger + ATR M15 bot as a validated, risk-capped system.
- **Phase 2:** a vol-targeted portfolio of independent sleeves (gold trend, gold meta-labelled bot, FX carry, MES trend, small BTC sleeve) across MT5, IBKR and Binance via ccxt.

The **same strategy code** runs in backtest, paper and live. There are no `if live:` branches inside strategy code.

**Prime directives (never violate):**

1. **Capital preservation > returns.** Any ambiguity → do NOT trade; alert the human.
2. **The risk engine vetoes everything.** No code path may send an order that has not passed `RiskEngine.approve()`.
3. **Every order carries a broker-side stop-loss.** An order without an SL must be impossible to construct (enforce in types and tests).
4. **Never implement** martingale, grid averaging-down, or size increases after losses.
5. **Never disable** risk checks, the kill switch or reconciliation to make tests pass.
6. **Default mode is PAPER.** Live requires all four of:
   - `MODE=live` in the environment;
   - the matching account number in config;
   - a `LIVE_CONFIRM` file containing today's date;
   - a typed confirmation at startup.
7. **`risk/limits.yaml` is read-only to code and to Claude.** Never edit it without an explicit instruction from Hussein. Config may only **tighten** limits, never loosen them.
8. **Every change follows the pipeline:** branch → tests → backtest parity → paper → canary → live (§13).

---

## 1. Unified Risk Rulebook → `risk/limits.yaml`

Create this file exactly. `risk/rules.py` loads it, validates it with pydantic, and **hard-caps every value at the maxima shown in comments**. A config value looser than the hard cap must raise at startup.

```yaml
# risk/limits.yaml — Unified Risk Rulebook (shared with the course PDF, Module 6)
version: 2
stage: demo                     # demo | micro_live | scaled_1 | scaled_2  (set by human only)

per_trade_risk_pct:             # % of current equity, from entry-to-stop distance × size
  demo: 0.25
  micro_live: 0.25
  scaled_1: 0.50                # only after Gate 2
  scaled_2: 1.00                # HARD MAX 1.00 — second scale-up review required
max_open_risk_pct: 2.0          # sum of initial stop risks, all positions
cluster_counts_as_one:          # correlated positions share one risk slot
  - [XAUUSD, XAGUSD, GOLD, MGC]
  - [EURUSD, GBPUSD, USDCHF_inv]
  - [MES, MNQ]
max_open_positions: 2           # and max 1 per symbol
max_trades_per_day: 4
max_effective_leverage: 5.0     # notional / equity
max_margin_usage_pct: 30        # per broker
crypto_max_equity_per_venue_pct: 15

daily_loss_limit_pct: 2.0       # equity-based incl. floating; resets 00:00 server time
daily_max_losing_trades: 3
weekly_loss_limit_pct: 4.0
consecutive_losses_halve: 4     # halve risk until +2R recovered
consecutive_losses_stop_day: 6

drawdown_ladder_from_hwm:       # high-water mark of equity
  L1: {dd_pct: 5.0,  action: halve_risk_no_new_sleeves}
  L2: {dd_pct: 7.5,  action: stop_entries_review_resume_at_0.25}
  L3: {dd_pct: 10.0, action: kill_flatten_demo_only_manual_reset}

filters:
  news_blackout_min_before: 30
  news_blackout_min_after: 15
  news_impact_levels: [high]    # CPI, NFP, FOMC, PCE (+ ECB/BoE for EUR/GBP symbols)
  news_currencies: {XAUUSD: [USD], EURUSD: [USD, EUR], GBPUSD: [USD, GBP], USDJPY: [USD, JPY]}
  calendar_fail_closed_windows_beirut: ["15:15-16:45", "20:45-22:00"]   # if calendar fetch fails
  spread_max_mult_of_hour_median: 2.0
  spread_max_points: {XAUUSD: 50}       # 50 points = $0.50 on a 2-digit quote
  no_entry_windows_server: ["23:45-00:30", "Fri 20:00-Sun 23:59"]
  min_reward_risk: 1.5
  price_sanity_atr1m_mult: 3.0          # order price within 3×ATR(1m) of independent mid
  fat_finger_size_mult: 3.0             # size ≤ 3× median size of last 50 orders

portfolio:                       # Phase 2 only
  vol_target_annual: 0.10        # HARD MAX 0.15
  max_ex_ante_vol_mult: 1.25
  es_975_1d_max_pct: 2.5
  weekend_gap_scenario_max_loss_pct: 6.0
  gap_scenario: {XAUUSD: -0.08, FX_major: -0.03, BTC: -0.20, MES: -0.07}
  max_instrument_share_of_risk: 0.25
  max_cluster_share_of_risk: 0.40

orders:
  max_orders_per_min_per_adapter: 20
  max_modifications_per_order: 5
  requote_retries: 2
  require_broker_side_sl: true

human:
  no_risk_increase_hours_after_night_shift: 12
  risk_increase_cooling_off_hours: 48
```

**Sizing contract (`risk/sizing.py`).** This is the only place lots are computed.

```python
lots = floor_to_step(risk_usd / (abs(entry - sl) / tick_size * tick_value_per_lot), volume_step)
if lots < volume_min:
    return Skip("risk too small for min lot")      # NEVER round up
# floor_to_step uses Decimal with a 1e-9 tolerance, so 0.4999999998 -> 0.50 (USDJPY fixture), not 0.49
# include spread: for longs, risk distance = (ask_entry - sl); for shorts = (sl - bid_entry)
# after fill: actual_risk = abs(fill - sl) / tick_size * tick_value * lots
# assert actual_risk <= per_trade_risk_usd * 1.10  else close immediately and alert
```

Effective leverage after sizing must be ≤ `max_effective_leverage`, otherwise skip.

---

## 2. Architecture (event-driven, one core for backtest and live)

```
MarketData → FeatureStore → Strategies (forecasts) → Allocator → RiskEngine (veto)
→ ExecutionEngine (algos) → BrokerAdapters → Fills → Portfolio + Journal → TCA + Observability
```

- **Live loop:** on each **closed** bar: feed → `strategy.on_bar` → Signal/Forecast → `OrderIntent(entry, sl, tp)` → `RiskEngine.approve` → execution → fill → state + journal → Telegram alert.
- **Monitor loop:** a separate 5–10 s loop runs the heartbeat, reconciliation, the equity-based daily-loss check, spread monitoring and the drawdown ladder.
- **Events** are pydantic models with: `event_id` (UUIDv7), `ts_exchange`, `ts_received`, `ts_processed`, `source`, `schema_version`.
- **Bus:** in-process asyncio queues in Phase 1; Redis Streams (single host) or NATS in Phase 2.
- **Framework:** custom asyncio core with identical backtest/live interfaces (Phase 1). Optionally migrate to NautilusTrader in Phase 2.
- **Time:** all internal timestamps are **UTC in nanoseconds**. Broker server time and Asia/Beirut are converted only in `core/clock.py` and the UI layer. Never assume the server is UTC.

### Repository layout

```
bot/
  config/        settings.yaml (pydantic-validated), .env (secrets, gitignored)
  core/          clock.py, events.py, bus.py, state.py (SQLite), config.py
  data/          feed_mt5.py, feed_ccxt.py, feed_ibkr.py, calendar.py, ingest/, qa.py, store/ (parquet)
  features/      registry.py (versioned features), rv.py, garch.py, structure.py, regimes.py
  strategies/<name>/  signal.py, params.yaml, README.md, tests/
  allocator/     carver.py, hrp.py
  risk/          limits.yaml, rules.py, sizing.py, engine.py, ladder.py, scenarios.py
  execution/     engine.py, algos.py, router.py, reconciler.py
  adapters/      mt5/, ccxt_binance/, ibkr/, paper/
  tca/           logger.py, reports.py
  monitoring/    telegram_bot.py, metrics.py (Prometheus), health.py
  backtest/      engine.py, costs.py, walkforward.py, montecarlo.py, metrics.py, cpcv.py, dsr.py, pbo.py
  research/      notebooks (NEVER imported by production code)
  mlops/         mlflow hooks, registry.py
  ops/           deploy/, chaos/, runbooks/
  tests/         unit/, property/, integration/, parity/, chaos/
main.py
CHANGELOG.md
```

---

## 3. Data layer

- **Storage:** Parquet partitioned by `symbol/date`; DuckDB for queries.
- **Sources:**
  - MT5: `copy_rates_range` and `copy_ticks_range` from the **execution broker**.
  - Dukascopy bid/ask ticks as an independent reference.
  - CME GC/MGC via IBKR (Phase 2).
  - Binance via ccxt `fetch_ohlcv` and public data.
  - FRED for macro.
  - CFTC COT.
- **QA on ingest (reject or flag):**
  - Monotonic timestamps; duplicate ticks.
  - Spread outliers (> 10× median).
  - Gaps vs the session calendar (CME maintenance break, FX Friday close).
  - DST transitions (March, October, November).
- **Point-in-time correctness:** every feature uses data with `ts <= decision_time`. Macro data is stored with an `as_of` date (no revised data in backtests).
- **Continuous futures:** back-adjusted for signals; actual contract prices for P&L and sizing; a per-instrument roll calendar.
- **Bars:** signals use **closed bars only**. In MT5, `copy_rates_from_pos(symbol, tf, 0, n)` returns the forming bar last, so **signals read `rates[-2]`**.

---

## 4. Strategies (sleeves)

Each sleeve is a pure function of features. It returns a **forecast in [−20, +20]** (Carver scaling, mean |f| = 10), never an order. Phase 1 may map the forecast to a discrete signal (+1/0/−1), with entry/SL/TP attached.

| ID | Sleeve | Rules (summary) | Phase |
|---|---|---|---|
| S0 | `gold_emabb_legacy` | Existing EMA12/26 cross + Bollinger confirmation (**must pick one interpretation**: breakout *or* re-entry; test both as separate trials), SL 1.5×ATR, TP 3×ATR, M15, closed bars | 1 (validation subject) |
| S1 | `gold_trend` | EWMAC 8/32, 16/64, 32/128, 64/256 + breakouts 20/40/80, equal blend × FDM, daily bars | 2 |
| S2 | `gold_emabb_meta` | S0 side + LightGBM meta-label probability p: `f = side × 20 × max(0, (p − 0.5)/0.5)`, min p = 0.55 | 2 |
| S3 | `fx_carry` | G10 rate-differential carry, vol-scaled; risk-off regime prob > 0.7 → halve | 2 |
| S4 | `index_trend` | MES multi-speed EWMAC | 2 |
| S5 | `crypto` | BTC spot trend (no perp leverage) OR delta-neutral funding carry; venue cap | 2 |
| S6 | `gold_sweep` (optional) | Asian-range sweep at London open (Setup C, course Module 10): pre-registered rules only | 2 |

Each sleeve's `README.md` must document:

- Hypothesis and economic rationale.
- Evidence label ([Strong]/[Mixed]/[Weak]).
- Parameters.
- Validation results (DSR, PBO, CPCV, walk-forward efficiency).
- Cost assumptions and capacity.
- **Kill criteria.**

**ICT/SMC concepts** (FVG, order blocks, sweeps, AMD) may only be implemented with the **frozen objective definitions** in course Module 9.3, and only as pre-registered research trials.

---

## 5. Allocator (Phase 2)

- **Sleeve weights:** HRP on 1-year sleeve return correlations, shrunk 50% toward inverse-vol. Recompute monthly. Max weight 40%; min 5% for active sleeves.
- **Instrument weights within a sleeve:** inverse-vol. IDM = 1/√(wᵀHw), capped at 2.5.
- **Target position** = forecast/10 × capital × τ × w_sleeve × w_instr × IDM / (price × multiplier × σ_instr).
- **τ:** `vol_target_annual` from limits (default 0.10, hard max 0.15).
- **Buffer:** trade only if |target − current| > 10% of the average position. Round **down** to the lot step.
- **Regime filter:** if gold 20-day realised vol > 95th percentile of 3 years, OR GVZ > 95th percentile → τ_gold × 0.5.
- **Account realism:** below ~$50k equity, run at most 3 sleeves. If granularity error exceeds 50% of the target risk, skip that instrument.

---

## 6. Risk engine (independent module, pre-trade + continuous)

`RiskEngine.approve(intent, portfolio_state) -> RiskDecision(approved: bool, reason_code, adjusted_size)`

**Pre-trade checks.** Run them in this order. **Any failure rejects** the order with a reason code, and the decision is logged.

1. Kill-switch flag absent; halt state not set; broker connected; clock drift < 250 ms (NTP).
2. Mode and account match config (demo vs real `trade_mode`).
3. Not inside a news blackout, rollover window or Friday cut-off; calendar fresh (otherwise apply the fail-closed windows).
4. Spread ≤ `spread_max_mult_of_hour_median` × median AND ≤ `spread_max_points`.
5. SL present, on the correct side, ≥ `trade_stops_level × point` from price, outside `trade_freeze_level`.
6. Reward:risk ≥ `min_reward_risk`.
7. Per-trade risk ≤ stage limit, halved when ladder ≥ L1 or the losing streak ≥ 4.
8. Open risk ≤ `max_open_risk_pct`, with clusters counted as one; positions ≤ max; trades today ≤ max.
9. Effective leverage ≤ 5×; margin usage after the trade ≤ 30% per broker.
10. Daily, weekly and ladder limits not breached.
11. Price sanity vs an **independent** mid feed; fat-finger size check.
12. Phase 2: ex-ante vol ≤ 1.25τ; ES(97.5%, 1d) ≤ 2.5%; weekend-gap scenario loss ≤ 6%; instrument share ≤ 25%; cluster share ≤ 40%.

**Continuous checks (monitor loop):**

- Equity-based daily loss (incl. floating P&L): breach → stop new entries until the next session.
- Drawdown ladder:
  - **L1:** risk × 0.5.
  - **L2:** stop entries; human review needed to resume at 0.25%.
  - **L3:** **kill**.
- The halt state **persists in SQLite** across restarts.

**Kill switch.** Triggers:

- Telegram `/kill` (with PIN).
- The `ops/KILL` file flag.
- L3 breach.
- 3 consecutive order errors.
- > 50 orders in 5 minutes.
- P&L drop > 3% in 15 minutes.
- Reconciliation mismatch persisting 5 minutes.
- Equity data unavailable > 5 minutes with open positions.
- More than 3 unknown retcodes in 10 minutes.

Kill = cancel all working orders → flatten per policy → disable adapters → alert → require a manual reset with a signed review note in `CHANGELOG.md`.

---

## 7. Execution engine and broker specifics

### 7.1 Algorithms

| Algo | Use |
|---|---|
| `MARKETABLE_LIMIT` | Default: limit at mid ± max_slip (0.5 × median spread) |
| `PASSIVE_TIMEOUT` | Post at bid/ask, cancel after T seconds, then marketable |
| `TWAP` | Orders > 5 lots or > 1% of ADV (futures) |
| `ICEBERG` | Futures; display ≤ 10% of size |

- **Idempotency:** `client_order_id = hash(strategy, instrument, intent_id, attempt)`. Retries reuse the ID. Never send a new order until the previous one is CONFIRMED (filled, cancelled or rejected).
- **Router (Phase 2):** pick the broker by TCA cost score, availability and margin headroom. Automatic failover to a secondary broker is allowed **for exits only**.

### 7.2 MetaTrader 5 (Python `MetaTrader5` package)

- Runs on **Windows** with a running MT5 terminal and Algo Trading enabled. One terminal per account per process.
- **Startup:**
  1. `mt5.initialize(path, login, password, server, timeout)`. On False, log `mt5.last_error()` and retry with backoff.
  2. Assert `terminal_info().trade_allowed`, `account_info().trade_allowed`, and that the account number and `trade_mode` match config and MODE.
  3. `mt5.symbol_select(symbol, True)`. Abort if not visible.
  4. Cache and **refresh every session:** `digits, point, trade_tick_size, trade_tick_value, trade_contract_size, volume_min, volume_max, volume_step, trade_stops_level, trade_freeze_level, filling_mode, swap_long, swap_short, swap_rollover3days, trade_mode`.
  5. **Never hardcode contract size.** Resolve symbol aliases (XAUUSD / XAUUSD.m / GOLD) from config, then assert `trade_contract_size`.
- **Market order:**

```python
request = {
  "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": lots,
  "type": mt5.ORDER_TYPE_BUY,             # or ORDER_TYPE_SELL
  "price": tick.ask,                      # tick.bid for sells
  "sl": sl, "tp": tp,                     # normalized to digits; respect stops_level
  "deviation": max_deviation_points, "magic": MAGIC,   # unique per strategy+symbol
  "comment": f"{strategy_id}:{intent_id}"[:31],
  "type_time": mt5.ORDER_TIME_GTC, "type_filling": detected_filling,
}
check = mt5.order_check(request)          # pre-flight margin/validity
result = mt5.order_send(request)          # then CONFIRM via positions_get(ticket/magic)
```

- **Filling mode:** read the `symbol_info().filling_mode` bitmask (FOK = 1, IOC = 2). Try FOK → IOC → RETURN. On 10030, switch and retry once.
- **Retcodes** (`result is None` = connection failure → reconnect flow):

| Retcode | Meaning | Action |
|---|---|---|
| 10009 | DONE | Record fill; recompute actual risk |
| 10008 / 10010 | PLACED / PARTIAL | Reconcile the remainder |
| 10004 / 10020 / 10021 | REQUOTE / PRICE_CHANGED / PRICE_OFF | Refresh tick; retry ≤ 2 within 2 s; then abort. **Never chase** |
| 10006 | REJECT | Log + alert, no retry |
| 10013 / 10014 / 10015 | INVALID / VOLUME / PRICE | Fix rounding or normalisation; no blind retry |
| 10016 | INVALID_STOPS | Widen to the minimum distance **and recompute size**, or skip |
| 10018 | MARKET_CLOSED | Skip |
| 10019 | NO_MONEY | **CRITICAL → halt** |
| 10024 | TOO_MANY_REQUESTS | Back off |
| 10027 | AUTOTRADING_DISABLED | Alert + halt |
| 10030 | INVALID_FILL | Switch filling mode once |
| 10031 | CONNECTION | Reconnect flow |
| other | — | Log, alert, no automatic retry |

- **Modify SL/TP:** `TRADE_ACTION_SLTP` with `position=ticket` (not possible inside the freeze level). **Close:** an opposite `TRADE_ACTION_DEAL` with `position=ticket` and the same volume.
- **Ownership:** manage only positions carrying this bot's `magic`.
- **Protective EA (recommended):** a minimal MQL5 EA that re-attaches a missing SL to any position with our magic number. It protects positions if Python crashes.
- **Pitfalls:**
  - Stops levels widen at news.
  - Weekend gaps.
  - The server time zone (usually GMT+2/+3 following NY DST; Lebanon's DST dates differ).
  - The triple swap day comes from `swap_rollover3days`.

### 7.3 ccxt / Binance (Phase 2)

- `ccxt.binance({'enableRateLimit': True})` with `set_sandbox_mode(True)` (testnet) during paper. Call `load_markets()`; always use `amount_to_precision` / `price_to_precision`; respect min-notional limits.
- **Spot first.** Entry + exchange-side stop (OCO where supported); confirm the stop is resting.
- **Perps only after spot passes the gates:** isolated margin, leverage ≤ 3×, `reduceOnly` exits, exchange-side `STOP_MARKET` immediately after entry. Include funding (00/08/16 UTC) and fees in P&L.
- **API keys:** trade-only, **withdrawals disabled**, IP-whitelisted to the VPS.
- 24/7 market: daily limits reset at 00:00 UTC.
- Verify Binance availability and KYC for Lebanese residents before connecting live.

### 7.4 IBKR (Phase 2, futures incl. MGC/MES)

- Use `ib_async`. Apply contract-month roll logic. Check margin with `whatIfOrder` before sending.

---

## 8. TCA logging (every order)

Fields:

`intent_id, decision_ts, decision_price (independent mid), send_ts, ack_ts, fill_ts[], fill_px[], qty, broker, venue, order_type, algo, spread_at_decision, slippage_vs_decision (bp, $/oz, R), slippage_vs_arrival, fees, swap, reject_reason, retcode, last_look_hold_ms (if available)`

- **Daily TCA job:** stats per broker, session and algo. Alert if 20-trade rolling slippage > 1.5× the cost model.
- **Monthly:** a broker scorecard that feeds the router.
- Realised slippage feeds back into `backtest/costs.py`.

---

## 9. Journal and state (SQLite)

**Tables:**

`orders, positions, fills, signals, risk_decisions, daily_pnl, halts, config_snapshots, tca, trades`

**`trades` schema** (identical to the course journal, Module 7.3):

`trade_id, strategy, version, symbol, direction, setup_name, timeframe, session, htf_bias, entry_planned, entry_actual, slippage_entry, stop_initial, target_initial, size_lots, risk_usd, risk_pct_equity, spread_at_entry, commission, swap, exit_price, exit_reason, r_multiple, mae_r, mfe_r, holding_minutes, news_within_2h, regime, rules_followed, notes, date_open_utc, date_close_utc`

- **Traceability:** every order is traceable via signal_id → risk_decision → order → fill.
- **Logs:** structured JSON, rotated daily, **never containing secrets**. Trade records are retained 7 years.
- **On startup:**
  1. Load positions by magic number and compare them with the DB.
  2. Adopt orphans only after verifying (or adding) an SL; alert on each.
  3. Restore the halt and ladder state.
  4. Resume only if the feed is fresh.

---

## 10. Telegram control and alerts

- **Auth:** bot token from the environment; whitelisted `chat_id`s only. A **PIN** is required for `/kill`, `/risk`, `/resume` and any live-mode command.
- **Commands:**

| Command | Effect |
|---|---|
| `/status` | Mode, stage, equity, positions, open risk, daily P&L, ladder level, halt flags |
| `/pause`, `/resume` | Entries only |
| `/kill` | Flatten + halt |
| `/risk 0.25` | Lowers freely; raising is capped by the stage limit and needs the 48 h cooling-off flag |
| `/trades today` | Today's trades |
| `/health` | Latency, last bar time, spread, calendar freshness, reconciliation status |

- **Alerts:**
  - Fills; rejects with retcode.
  - SL/TP hits with R-multiple.
  - Limit or ladder changes.
  - Disconnections; stale feed > 30 s in session; heartbeat missing > 60 s.
  - Margin > 30%.
  - Daily summary at 23:00 Beirut.

---

## 11. Observability (Phase 2; Phase 1 may use logs + Telegram only)

- **Prometheus metrics:**
  - Equity; exposure by sleeve and cluster.
  - Ex-ante vol, ES, drawdown and ladder level.
  - Heartbeat per process.
  - Data latency p50/p99; order ack latency.
  - Reject rate, slippage, reconciliation diffs.
- **Grafana dashboards:** Portfolio, Risk, Execution/TCA, Data health, Infra.
- **Alertmanager → Telegram.**

---

## 12. Backtest and research pipeline

**Engine:** event-driven. It reuses `Strategy`, `RiskEngine` and `sizing` **unchanged**. A vectorised engine (vectorbt) is allowed only for research triage.

**Cost model (`backtest/costs.py`):**

- Spread by hour-of-week, from recorded broker data.
- A slippage distribution with a news multiplier.
- Commission.
- Swap by nights held, including the triple day.
- Broker minimum stop distance.

**Intrabar:** use tick data where available. Otherwise assume the **stop is hit first** when both SL and TP are touched in a bar.

**Required outputs:**

- Trade list (journal schema) and equity curve.
- Expectancy, PF, win rate, average R.
- Max drawdown and its duration; CAGR; Sharpe and Sortino.
- **Deflated Sharpe** (with the trial count from the registry).
- **PBO via CSCV.**
- **Random-entry benchmark** (same exits and sizing).
- Monte Carlo 5/50/95 percentiles (bootstrap and skip-10%).
- Walk-forward efficiency.
- Parameter heatmap.
- Regime split (trend/range, high/low ATR, 2024 vs 2026).

**Trial registry:** MLflow logs every run with git SHA, data snapshot hash, params, trial count and metrics. *Never* report a result without its trial count.

**Data splits:** IS ~60% / OOS ~20% / final holdout ~20% (the most recent period, opened once and logged).

**ML (Phase 2, `features/` + `mlops/`):**

- Stationary features (frac-diff, min-d via ADF).
- Triple-barrier labels and meta-labelling.
- Sample-uniqueness weights.
- **Purged k-fold + embargo** and CPCV.
- MDA/SHAP importance.
- Scaling with rolling or expanding statistics only.
- HMM regimes use **filtered** probabilities only.
- Drift monitors: PSI > 0.25 → alert. Monthly retrain with frozen hyper-parameters unless re-validated.

### Promotion gates (all must pass; record them in `CHANGELOG.md`)

| Stage | Requirements |
|---|---|
| Research → Shadow | OOS expectancy after costs ≥ +0.10R; PF ≥ 1.2; PBO < 0.2; DSR prob > 0.95; cost-stressed (×1.5) Sharpe > 0.3; OOS positive in ≥ 70% of CPCV paths; beats random-entry 95th percentile |
| Shadow → Paper (≥ 4 weeks) | Live signals match offline recompute exactly; ML calibration Brier within 10% of OOS |
| Paper → Micro-live (**Gate 1**) | ≥ 100 trades or 12 weeks; expectancy ≥ +0.15R after real costs; PF ≥ 1.3; max DD ≤ 10% (1%-equivalent); inside the MC 5–95% band; 100% risk-rule compliance; no unhandled errors for 4 weeks; kill switch tested |
| Micro-live → 0.50% (**Gate 2**) | ≥ 50 live trades at 0.25%; live expectancy ≥ 60% of paper; DD ≤ 1.5× MC median; slippage within 25% of model; zero reconciliation incidents |
| 0.50% → 1.00% | Gate 2 repeated at 0.50% + human scale-up review + 48 h cooling-off |

**Step-down:** an L1 drawdown or adherence < 90% → drop one stage automatically.

---

## 13. Testing (CI must pass before any merge)

- **Unit tests (≥ 90% coverage on risk, sizing, execution, adapters).** Sizing fixtures from the course answer keys ($10,000, 1%):
  - EURUSD 25 pips → 0.40
  - USDJPY @150, 30 pips → 0.50
  - EURGBP 20 pips @ GBPUSD 1.30 → 0.38
  - XAUUSD $12 → 0.08
  - XAUUSD $7.50 → 0.13
  - MGC $12 → 0 (skip)
  - MES 15 pts → 1
  - XAUUSD $4 → skip (leverage 10× > 5×)
- Also unit-test: volume_step rounding, stops levels, every retcode branch, DST transitions, blackout windows and the fail-closed calendar.
- **Property tests (Hypothesis):** for any input, risk never exceeds the limit; no SL-less order can be constructed; stops are always on the correct side; size is never rounded up.
- **Integration:** mock-MT5 plus a 0.01-lot demo smoke test; ccxt testnet.
- **Parity:** a replay of recorded ticks through the backtest and the live paths produces identical signals and timestamps.
- **Determinism regression:** same data + same seed = the same trade list.
- **Chaos (monthly, staging):**
  - Kill the broker connection mid-order.
  - Duplicate fill messages; delayed acks (5 s).
  - Stale feed; clock jump.
  - VPS reboot; Redis restart.
  - Partial fills.
  - Replays of SNB 2015 and the 30 Jan 2026 gold drop.

  Expected behaviour is documented per scenario. A test fails if any position is unreconciled or any limit is breached.

---

## 14. Deployment and operations

- **VPS:** a Windows VPS near the broker's data centre (often London LD4) for MT5, running as a Windows service with auto-restart and NTP sync. Docker Compose on Linux for the ccxt/IBKR services. **The Beirut home PC is for monitoring only.**
- **Reconnection:** exponential backoff (1, 2, 4 … 60 s), then re-`initialize`, refresh `symbol_info`, reconcile, and resume only if the feed is fresh.
- **CI/CD:** GitHub Actions runs lint, mypy, tests and a short backtest. Tagged, signed releases only. One-command rollback to the previous image tag. A canary flag per sleeve runs at 25% size.
- **No deploys:**
  - Friday after 12:00 UTC.
  - Within 2 hours of a tier-1 macro event.
  - While the ladder is ≥ L1.
  - During market hours for MT5 changes.
- **Backups:** daily DB + config; a quarterly restore drill.
- **Knight Capital rule:** delete dead code, never reuse flags, check deployment parity across hosts, and enforce hard order-rate caps.

---

## 15. Security

- Secrets live in `.env` (gitignored) or a secret manager (sops). **Never commit secrets; never log them.**
- Use a read-only investor password for monitoring dashboards.
- Exchange API keys are trade-only, have **no withdrawal permission**, are IP-whitelisted and are rotated quarterly.
- Broker accounts use 2FA. RDP is restricted by IP or VPN.
- Telegram uses a user-ID allowlist plus a PIN.

---

## 16. Governance

- **`CHANGELOG.md`:** every change gets date, author, reason, risk impact, tests run and approval.
- **Sleeve kill criteria** (any one → disable the sleeve and do a research review):
  - Live 12-month rolling Sharpe < 0 AND below the 5th percentile of the bootstrap expectation.
  - Drawdown > 2× the sleeve's backtest max drawdown.
  - Slippage > 1.5× the model over 40 trades.
  - A structural change (contract spec, regime, broker) that invalidates the hypothesis.
- **Venture stop rule:** if, after 24 months live, the portfolio's expectancy confidence interval still includes zero, return to research-only mode.
- **Quarterly review:** attribution by sleeve, TCA, incidents, ladder events and the research pipeline. Decisions are logged.
- **Human-in-the-loop:** humans may reduce risk at any time. Increasing risk needs the checklist plus 48 hours of cooling-off, and **never within 12 hours after a night shift**.

---

## 17. Coding conventions

- Python 3.12+, full type hints, `mypy --strict`, `ruff`, `pytest`, `hypothesis`, pydantic v2 for config and events.
- Strategies are **pure functions**. All I/O goes through adapters. No global state.
- Use round-to-tick helpers and `Decimal` where prices are compared. **Never compare floats for equality.**
- No magic numbers: every parameter lives in YAML with a schema.
- Unique magic numbers per strategy and symbol (e.g., `26121501`).
- Small PRs. Run the full test suite + parity replay, and report the results verbatim.

---

## 18. Instructions for Claude (coding agent) in this repo

1. Read this file and `risk/limits.yaml` before any change. **Never edit `limits.yaml`** without an explicit instruction from Hussein.
2. **Write tests first** for any change touching risk, sizing, execution or adapters.
3. Never introduce look-ahead. Features take an `as_of` argument and must pass the parity harness.
4. When unsure how a broker behaves, write a demo-account integration test instead of assuming.
5. **Do not push to GitHub** unless Hussein explicitly says so.
6. Never place a live order, change `MODE`, or create `LIVE_CONFIRM` yourself.
7. Report backtest results with the trial count, costs used and data range. Flag anything that looks too good (Sharpe > 2, PF > 2.5, negative skew with high win rate) as a likely bug or overfit.
8. **Build order for Phase 1:**
   1. `core/` (clock, events, config) + `risk/limits.yaml` loader with hard caps.
   2. `risk/sizing.py` + full unit/property tests (course fixtures).
   3. `data/` MT5 feed (closed bars), Parquet store, QA.
   4. `backtest/` engine + cost model + metrics + walk-forward + Monte Carlo + random-entry benchmark.
   5. `strategies/gold_emabb_legacy` + the validation protocol (course Module 15.5) → validation report.
   6. `risk/engine.py` + ladder + kill switch + persistence.
   7. `adapters/mt5` with retcode handling, filling modes and reconciliation.
   8. `monitoring/telegram_bot.py`.
   9. Paper run on a VPS for ≥ 100 trades or 12 weeks → Gate 1 report (`ops/graduation_report.py`).
