# TEST_REPORT.md (v2.1)

## A. Tests successfully completed locally
Fresh extraction of `ICT_PO3_15M_Signal_Scanner_v2.1.zip`, new virtualenv, `pip install -r requirements.txt` (Python 3.12.3):
`python -m compileall .` clean; `python -m pytest -q` → **153 passed, 0 failed** (141 from v2 + 12 new checkpoint tests).

| File | Tests | Covers |
|---|---|---|
| test_po3_strategy.py | 11 | ATR/highest/lowest warm-up, BUY/SELL, no-signal, TP exit, no exit on entry bar, SL-overrides-TP, Fixed mode, incremental==batch, mode/risk inputs, Close vs Wick |
| test_parity_reference.py | 27 | engine vs independent Pine transcription: 24 input combinations × 6 random series, both directions/exits exercised, persisted incremental runs, `max_distance` window |
| test_compare_tool_and_init.py | 6 | TradingView comparison tool (epoch+ISO, mismatch detection), state re-init on settings change / stale state, state continuity, bootstrap no old alerts, 4800-bar bootstrap |
| test_providers.py | 10 | exactly 28 unique pairs + CHFJPY + 30 instruments, 4 keys × 7 pairs (exact spec), routing, gold separate from forex keys, missing key, backoff, Twelve Data parsing/symbol/timeframe, credit header, router fallback/unavailable, secret redaction |
| test_quota.py | 12 | budget always enforced (0 clamped), 672 ≤ 720, reserve semantics, never beyond budget, persistence across runners, provider-quota flag, per-key per-minute limit, rate-limit events, gold bucket, `skipped_quota` reporting, cache hits (same slot = 0 requests), usage report |
| test_validation.py | 18 | future-dated/timezone, wrong timeframe, price band, missing candles (reported, window rejection), invalid OHLC, insufficient/short history, AlgoSpecial disabled until offset verified, offset handling, stale XAUUSD → unavailable, no Yahoo/substitutes, NAS100 unavailable/CFD provider named/wrong-scale rejected, OANDA identity |
| test_confirmed_candles.py | 4 | default confirmed-only, provisional only if enabled, provisional→confirmed not repeated, forming candle never cached |
| test_scanner.py | 16 | signal on each of the 4 closed window candles, older than window ignored, duplicates across runs, failed delivery retried, multiple signals, all 30 instruments, API/DQ/stale failures not counted as scanned, weekend skip, delayed start, dry run, report files |
| test_state_sync.py | 8 | against a **local bare git repo**: first run, restore on a new runner, single-commit branch, lease blocks overlapping push, restore failure is an error, token never in errors; ledger ordering (`sending` before Telegram, `delivered` after), crash-after-send → one flagged resend, ambiguous vs definite failure |
| **test_checkpoint_failure.py (new, v2.1)** | 12 | **pre-send checkpoint failure ⇒ Telegram is never called** (call counter = 0), ledger restored to `pending` (or kept `sending` if it already was), later signals/instruments not processed and reported; unchanged `sending`→`delivered` ordering when checkpoints succeed; **post-send checkpoint failure** ⇒ reported as `sent_state_not_persisted` / possible-repeat risk (not as persisted), remote snapshot still `sending`, next runner resends once flagged "possible repeat"; report file wording; `make_checkpoint()` propagates `StateSyncError` and is `None` outside Actions; `main.py` exits **2** (pre- and post-send failure), **0** on success; workflow "Run scanner" step has no `continue-on-error` |
| test_duplicates.py / test_telegram.py / test_signals.py / test_candles.py / test_workflow.py | 3 / 6 / 5 / 11 / 4 | ledger persistence, Telegram retry/429/4xx/network, message & PKT, grid/sessions/DST, workflow YAML (cron `*/15`, concurrency, restore/save steps, 4 keys + gold + OANDA secrets) |

A smoke run (`main.py --dry-run --only EURUSD,CHFJPY,XAUUSD,NAS100`) with no network/keys reported every instrument as NOT SCANNED with a reason and wrote the reports - nothing skipped silently.

## B. Tests requiring live API credentials - NOT EXECUTED
Twelve Data (4 keys + gold key) responses, real limits/credits headers, XAU/USD plan availability, data freshness; AlgoSpecial endpoint/timezone offset; OANDA XAU_USD / NAS100_USD. All provider code is tested with mocked payloads only.

## C. Tests requiring Telegram credentials - NOT EXECUTED
Real `sendMessage` delivery, chat-id validity, message rendering. Telegram logic is tested with a fake HTTP client.

## D. Tests requiring GitHub Actions - NOT EXECUTED
Real scheduling (`*/15`), delayed/skipped runs, token permissions, orphan-branch push to GitHub, concurrency queue behaviour. Verified locally only: YAML structure and the same git commands against a local bare repo.

## E. Tests requiring TradingView comparison - NOT EXECUTED
No TradingView OHLC/signal export was available. `tools/compare_tradingview.py` is tested on synthetic data only. **TradingView parity is not demonstrated.**

## v2.1 verification notes
* Mutation check: temporarily re-introducing the old behaviour (swallowing the pre-send checkpoint failure) made 4 of the 12 new tests fail; the code was restored and the full suite re-run (153 passed).
* The post-send scenario is **unavoidable by design** (Telegram delivery and the git push cannot be atomic). The tests prove it is detected, reported, exits non-zero, and recovers with one flagged resend - not that it can be prevented.
* "Run fails on GitHub" is verified only as: `main.py` returns exit code 2 and the workflow step has no `continue-on-error`. Actual GitHub behaviour was **not** executed (section D).
