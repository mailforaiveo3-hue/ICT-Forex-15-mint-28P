# ICT Power-of-Three 15-minute Signal Scanner (v2.1)

Scans **28 Forex pairs + XAUUSD + NAS100 (30 instruments)** every 15 minutes on GitHub Actions with a Python port of the
Flux Charts PO3 Pine script and sends new BUY/SELL signals to **Telegram only** (no order execution). Informational tool, not financial advice.

> **Read `REVIEW_REPORT.md` first** — it lists what changed in v2, what is verified and what is NOT (no live API, Telegram, GitHub or TradingView test was possible in the build environment).

## 1. Install (local)
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q
```

## 2. Secrets (GitHub → Settings → Secrets and variables → Actions)
| Secret | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Telegram delivery |
| `TWELVE_DATA_API_KEY_1` … `_4` | Forex, **7 pairs per key** (allocation below) |
| `TWELVE_DATA_API_KEY_GOLD` *(optional)* | dedicated key for XAU/USD |
| `OANDA_API_TOKEN` *(optional)* | live XAUUSD / NAS100 CFD source (`OANDA_ENV=practice\|live`) |
| Repository **variable** `ALGOSPECIAL_UTC_OFFSET_HOURS` *(optional)* | only after you verified AlgoSpecial's server-time offset |

### Four-key allocation (fixed, never rotated)
| Key | Pairs |
|---|---|
| 1 | EURUSD GBPUSD USDJPY USDCHF USDCAD AUDUSD NZDUSD |
| 2 | EURGBP EURJPY EURCHF EURCAD EURAUD EURNZD GBPJPY |
| 3 | GBPCHF GBPCAD GBPAUD GBPNZD AUDJPY AUDCHF AUDCAD |
| 4 | AUDNZD NZDJPY NZDCHF NZDCAD CADJPY CADCHF CHFJPY |

`config.py` asserts at import: 28 unique pairs, 7 per key, 30 instruments in total.

## 3. Data providers (summary — details in `API_COMPATIBILITY_REPORT.md`)
* **Forex:** Twelve Data only.
* **XAUUSD:** AlgoSpecial *(disabled until the timezone offset is configured; it documents a historical dataset, so it normally fails the freshness gate)* → Twelve Data `XAU/USD` on the **dedicated gold key** → OANDA `XAU_USD`. If none is valid the instrument is reported **unavailable**.
* **NAS100 (Nasdaq-100 CFD only):** AlgoSpecial `USTEC` → OANDA `NAS100_USD`. No index/ETF/futures substitute exists in the code. **With only the free AlgoSpecial data NAS100 will be “unavailable”** — add an OANDA token (or another verified CFD feed) if you need it.

## 4. Quota (Twelve Data Basic plan: 8 credits/min, 800/day **per key**, 1 credit per `time_series` request)
* Scheduled load: 7 pairs × 96 scans = **672/day per key**. Budget 800, **retry reserve 80** → scheduled requests stop at 720; retries may use the reserve. Gold on its own key: 96/day.
* Usage is persisted per UTC day in the state DB, enforced for every key, and **cannot be disabled** (`TWELVE_DATA_DAILY_BUDGET=0` is clamped to 1).
* When a key is exhausted its instruments are reported `skipped_quota` (never silent; listed under “NOT SCANNED”).
* Re-runs inside the same 15-minute slot use the cache and make **0 requests**. Credits are per request, so fetching fewer rows saves bandwidth, not credits.
* The first run downloads 4800 candles per instrument (1 credit each).
* Each scan writes `reports/scan_*.md|json` and `reports/api_usage_*.json` (requests per key, remaining credits, rate-limit events, failures, cache hits, skipped instruments) and appends the report to the GitHub run summary.

## 5. GitHub setup
1. Private repo, push this folder. 2. Add secrets. 3. Settings → Actions → General → Workflow permissions → **Read and write**. 4. Actions → “PO3 15m Scanner” → **Run workflow** with `dry_run=true` first.

## 6. How a scan works
`restore state → fetch (4 key groups + gold + NAS100 in parallel) → validate → PO3 engine on closed candles → 5-candle window → dedupe → Telegram → push state`.
* **Window:** forming slot + 4 closed slots. Only closed candles can signal (`EVALUATE_FORMING_CANDLE=false`). If you enable it, alerts are labelled PROVISIONAL and share the confirmed signal's ID, so the later confirmed version is blocked as a duplicate.
* **Validation (reject, never trade):** finite/positive/consistent OHLC, 15-min grid, duplicates, future-dated candles (catches wrong timezone offsets), timeframe identity, symbol identity (Twelve Data meta, OANDA instrument, price band for XAUUSD/NAS100), freshness (≤3 candles old while the market is open), ≥500 closed candles, and no missing candle inside the 5-candle window. Missing candles are reported, never invented.
* **History:** 4800 candles requested/cached (Twelve Data max 5000/request). The PO3 state is persisted between runs; it is re-initialised from history if settings change or the state is older than the cache.

## 7. Duplicate prevention & persistence
Ledger `pending → sending → delivered` in SQLite, stored (gzip) as one orphan commit on branch **`state`**.
* `sending` is pushed **before** the Telegram call and `delivered` right after it (`STATE_SYNC_CHECKPOINT`). **Both pushes are mandatory (v2.1):** failures are never swallowed.
  * **Pre-send checkpoint fails →** Telegram is **not called**, the ledger returns to its previous status (`pending`), the scan aborts safely, the report has a "SCAN ABORTED" section and `main.py` exits with code **2**, so the GitHub Actions run is marked failed.
  * **Post-send checkpoint fails →** the message **was sent** but `delivered` is **not persisted**. This is reported as a **POSSIBLE-REPEAT RISK** (log, report, exit code 2); the scan stops. Unless the workflow's final state push succeeds, the next run re-sends that signal once, flagged “possible repeat”. This case is unavoidable: Telegram and git cannot be made atomic. We prefer a flagged repeat over a lost signal.
  * If a runner dies between the send and the push, the same flagged resend happens. Definite Telegram failures return to `pending` and retry while still inside the window.
* Pushes use `--force-with-lease`; an overlapping run cannot overwrite newer state (it fails loudly). `concurrency` already prevents overlap.
* If the state branch exists but cannot be restored, the run **fails** instead of starting from empty state.

## 8. Troubleshooting
`not configured` → add the secret · `skipped_quota` → plan/credits (see report) · `unavailable` → no valid fresh provider · `missing candles in the eligible window` / `stale` → provider lag or holiday · state push rejected → another run changed state; re-run.
Schedules on GitHub can start late or be skipped; the 4-candle cushion covers short delays only.
