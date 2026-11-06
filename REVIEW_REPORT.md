# REVIEW_REPORT.md - ICT PO3 15M Signal Scanner v1 → v2 → v2.1


## v2.1 - mandatory state checkpoint correction

**Defect (v2):** `main.make_checkpoint()` caught `StateSyncError` and only logged it, so Telegram delivery continued even when the pre-send `sending` state could not be persisted. A crash after such a send would have lost the record that the send was attempted.

**Fix**
| Requirement | Implementation |
|---|---|
| Do not swallow `StateSyncError` | `main.make_checkpoint()` no longer catches anything; the error propagates |
| Failed pre-send checkpoint stops the send | `Scanner._deliver()` calls the checkpoint **before** `tg.send()`; on any exception it restores the ledger status (`pending`, or `sending` if it already was), records `not_sent_checkpoint_failed`, and raises `CheckpointError("pre-send")` - Telegram is never called |
| Scanner aborts safely | `Scanner.run()` catches `CheckpointError`, stops processing (no further sends), marks unprocessed instruments `aborted_not_processed`, and still returns/writes the full report with `summary.aborted` |
| GitHub run reports failure | `main.py` returns exit code **2** when aborted; the "Run scanner" step has no `continue-on-error`; the always-run "Save persistent state" step still tries to push |
| Keep pending → sending → delivered | unchanged order; test `test_workflow_unchanged_when_checkpoint_succeeds` |
| Post-send checkpoint failure | message is sent; `delivered` is **not** claimed as persisted: signal status `sent_state_not_persisted`, `summary.aborted.possible_repeat_risk = true`, ERROR log "POSSIBLE-REPEAT RISK", report section "SCAN ABORTED", exit code 2; scan stops |

**Unavoidable possible-repeat scenario:** Telegram and the git push are two separate systems and cannot be made atomic. If the message is accepted by Telegram and the `delivered` push then fails (or the runner dies), the remote state still says `sending`. Unless the workflow's final state push succeeds, the next run re-sends that signal once, prefixed "possible repeat". Cause and effect are reported; the risk cannot be eliminated, only made visible and bounded to one flagged repeat.

**Files changed in v2.1:** `main.py`, `scanner.py`, new `tests/test_checkpoint_failure.py`, `README.md` (section 7), `API_DOCUMENTATION.md`, `TEST_REPORT.md`, `REVIEW_REPORT.md`. Not changed: `po3_strategy.py` and all other features.

**Results (actual):** fresh extraction of `ICT_PO3_15M_Signal_Scanner_v2.1.zip`, new virtualenv, `compileall` clean, **153 passed, 0 failed** (141 previous + 12 new). Mutation check: with the old swallow behaviour re-introduced, 4 of the new tests fail. Not executed: live APIs, Telegram, GitHub Actions, TradingView (see `TEST_REPORT.md` B-E).

---

The original Flux Charts PO3 logic (`po3_strategy.py`) was **not changed**. All corrections are in data handling, API management, persistence and testing.

## 1. Corrections made
| # | Requirement / defect found in v1 | Fix |
|---|---|---|
| 1 | 27 pairs, CHFJPY missing | 28 pairs, CHFJPY added; import-time asserts (28 unique, 7/key, 30 instruments) |
| 2 | 3-key allocation | **4 keys × 7 pairs** exactly as specified; all 3-key text/config/tests/docs/workflow removed |
| 3 | Daily budget could be disabled (`0`) | always enforced, default 800/key, `0` clamped to 1; retry reserve 80 (first attempts stop at 720) |
| 4 | Usage not persisted properly / no report | per-key, per-UTC-day stats in SQLite (restored from the state branch): requests (forex/gold), rate-limit events, failures, cache hits, quota flag, header credits; `api_usage_*.json` + report table + GitHub run summary |
| 5 | Gold silently used forex key 3 | gold has its own bucket and **dedicated key** (`TWELVE_DATA_API_KEY_GOLD`); sharing a forex key only by explicit `GOLD_TWELVE_SHARE_KEY`; no key → XAUUSD reported unavailable |
| 6 | Instruments could be skipped silently | statuses `skipped_quota`, `unavailable`, `failed_*` + "NOT SCANNED" section, log warnings, run summary |
| 7 | Yahoo `^NDX` fallback | removed from code and config; NAS100 accepts CFD sources only (AlgoSpecial USTEC, optional OANDA NAS100_USD); else `unavailable` |
| 8 | AlgoSpecial timestamps assumed UTC | disabled until `ALGOSPECIAL_UTC_OFFSET_HOURS` set; future-dated candles rejected; docs state it is a historical dataset |
| 9 | History 1500/2500 vs 4800 | history 4800, cache 5000; <500 candles rejected, 500-4799 processed with `history_complete=false`; PO3 state re-initialised if settings change or state predates cache |
| 10 | Forming (partial) candle was cached | only closed candles are cached; cache hits skip the API in the same slot |
| 11 | Validation gaps | + future-dated candles, timeframe identity, symbol identity (Twelve Data meta, OANDA echo, price bands), missing-candle detection (reported, window rejection), history minimum |
| 12 | Delivery race (send → crash before push) and unsafe force-push | ledger `pending→sending→delivered`, state pushed before and after each send; one flagged resend after a crash; `--force-with-lease`; restore failure aborts the run |
| 13 | Parity untested | independent Pine transcription + 27 cross-check tests + `tools/compare_tradingview.py` |
| 14 | Provisional vs confirmed duplicates | same signal ID; test proves the confirmed version is blocked |

## 2. Files changed
New: `state_sync.py`, `data_providers/oanda_provider.py`, `tools/compare_tradingview.py`, `tests/{reference_pine,test_parity_reference,test_quota,test_validation,test_state_sync,test_confirmed_candles,test_compare_tool_and_init}.py`, `REVIEW_REPORT.md`.
Rewritten/changed: `config.py`, `logger.py`, `database.py`, `duplicate_manager.py`, `candle_manager.py`, `scanner.py`, `main.py`, `signal_manager.py`, `telegram_bot.py`, `data_providers/{base,api_manager,twelve_data,gold_provider,nas100_provider}.py`, `.github/workflows/scan.yml`, `.env.example`, `README.md`, `API_COMPATIBILITY_REPORT.md`, `API_DOCUMENTATION.md`, `STRATEGY_PARITY.md`, `TEST_REPORT.md`, tests `test_providers/test_scanner/test_signals/test_workflow.py`.
Unchanged: `po3_strategy.py`.

## 3. Four-key allocation
1: EURUSD GBPUSD USDJPY USDCHF USDCAD AUDUSD NZDUSD · 2: EURGBP EURJPY EURCHF EURCAD EURAUD EURNZD GBPJPY · 3: GBPCHF GBPCAD GBPAUD GBPNZD AUDJPY AUDCHF AUDCAD · 4: AUDNZD NZDJPY NZDCHF NZDCAD CADJPY CADCHF CHFJPY.

## 4. Quota calculation (Twelve Data Basic: 8/min, 800/day per key, 1 credit/request - from Twelve Data's pages, not live-verified)
7 × 96 = 672/day/key → fits under the enforced scheduled limit of 720 with 80 reserved for retries (48 spare scheduled). Gold 96/day on its own key.
**Caveat:** this works only if the four keys are four independent 800/day allowances. One account would be 2688 requests/day needed vs 800 allowed.

## 5. Data provider status
Twelve Data: documented limits only (not live). AlgoSpecial: **historical dataset, server-time timestamps, not usable live** → disabled/rejected as stale. OANDA: optional, unverified, region-dependent. Details: `API_COMPATIBILITY_REPORT.md`.

## 6. XAUUSD / NAS100 limitations
* XAUUSD needs a live source: Twelve Data XAU/USD on a dedicated key (Basic-plan availability unverified) or OANDA. Otherwise → `unavailable`.
* **NAS100 will be unavailable unless you add a verified live CFD feed** (OANDA token provided as an option). Yahoo/QQQ/NQ are not used.

## 7. PO3 parity status
Engine matches an independent transcription of the Pine script (local). **TradingView parity not demonstrated**; use `tools/compare_tradingview.py`. Details and known differences: `STRATEGY_PARITY.md`.

## 8. Test results
153 passed locally in a fresh extraction + new virtualenv (v2.1); compile check clean. Live API, Telegram, GitHub Actions and TradingView comparisons were **not executed** (see `TEST_REPORT.md` sections B-E).

## 9. Remaining limitations
* Nothing verified against live services; first live run may need small provider-format fixes.
* The 5-candle window plus 4-candle cushion cover short GitHub delays only; skipped schedules are not backfilled beyond the window.
* A strict missing-candle rule can reject an instrument for up to an hour when the provider omits a candle.
* Holidays are not modelled (appear as stale/missing data). Daily Twelve Data reset time assumed UTC midnight.
* State DB ≈ a few MB (28+ series × 5000 candles, gzip) pushed on every scan and twice per signal.
* A crash or push failure exactly between Telegram acceptance and the `delivered` push yields one clearly flagged repeat (unavoidable; reported with exit code 2 when detected).
* Free-tier Twelve Data data may be delayed; the freshness gate rejects data older than 3 candles but cannot detect smaller delays.

## 10. Actions required from you
1. Create **four** Twelve Data keys (separate accounts/allowances) and add secrets `TWELVE_DATA_API_KEY_1…4`; add `TWELVE_DATA_API_KEY_GOLD` (or `OANDA_API_TOKEN`) for XAUUSD.
2. For NAS100 add `OANDA_API_TOKEN` (check CFD availability in your region) or tell me another verified CFD feed.
3. Telegram secrets; set workflow permissions to Read and write; run once with `dry_run=true`, then check the run summary and `scan-report` artifact.
4. If you want AlgoSpecial, verify its server-time offset and set the `ALGOSPECIAL_UTC_OFFSET_HOURS` repository variable (it will still be rejected if its data is stale).
5. Export TradingView data and run `tools/compare_tradingview.py` to demonstrate parity.
