# CHANGELOG

Every change and every manual risk action is recorded here (CLAUDE.md section 16).

## 2026-11-20 - Risk core: Unified Risk Rulebook implemented (Phase 1 build steps 1, 2 and 6)
- Author: Claude (coding agent), working for Hussein
- Reason: CLAUDE.md v2.0 makes the Unified Risk Rulebook binding. Hussein chose: retrofit `goldbot/`
  in place, risk core first, the macro bot (`gold_macro`) as the first sleeve.
- Risk impact: LARGE and deliberately TIGHTER. Per-trade risk 1% -> 0.25%; daily loss limit 3% ->
  2% (now blocks entries instead of flattening); new weekly limit, ladder, kill switch, trade-count,
  leverage and margin caps; day/week boundary moved from UTC to broker server midnight; every order
  now needs `RiskEngine.approve()`. Check 11 fails closed, so the bot cannot place orders until an
  independent price feed exists (dry-run only).
- Tests run: full suite (pytest), including the 8 course sizing fixtures, one test per pre-trade
  check, property tests (Hypothesis) for "risk never exceeds the limit" and "size never rounds up",
  and whole-bot tests for the engine veto, kill switch persistence, ladder escalation, fill
  verification and the live gate. Not run: parity replay, demo-account integration, chaos tests
  (those components do not exist yet).
- Approval: PENDING - not yet reviewed by Hussein.

Interpretations made (please confirm or correct):
1. `limits.yaml` hard caps: CLAUDE.md names two maxima (stage 1.00% per trade, vol target 0.15). For
   every other limit the cap is the rulebook v2 value itself, so values may be tightened but never
   loosened without a code change. See `goldbot/risk/rules.py` (`BOUNDS`).
2. `MODE=paper` requires a DEMO account and `MODE=live` a REAL account (check 2). Dry-run (nothing
   sent) skips that check because no money is at risk.
3. The typed live confirmation is `LIVE <account number>`. `LIVE_CONFIRM` may hold today's UTC or
   Beirut date. An unattended restart can never go live (nobody can type).
4. After `reset-ladder` the high-water mark is rebased to current equity (otherwise the ladder would
   re-trigger at once) and risk is capped at 0.25%. Both are written to this file.
5. Clock drift is estimated from fresh broker ticks (smallest recent sample), which includes network
   latency, so it is conservative.
6. The hour-of-day median spread comes from the last 7 days of M15 bars' spread field.

Not yet built (CLAUDE.md section 18.8): data layer and Parquet store (3), backtest engine, cost
model, DSR/PBO/CPCV and the validation report (4-5), MT5 adapter retcode handling beyond the table
and halts, filling-mode switching, startup reconciliation and orphan adoption (7), Telegram control
commands with PIN (8), the Gate 1 graduation report (9), the protective MQL5 EA, TCA logging, the
`trades` journal schema, Python 3.12 + `mypy --strict`.
