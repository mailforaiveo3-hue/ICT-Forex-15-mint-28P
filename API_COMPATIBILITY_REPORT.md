# API_COMPATIBILITY_REPORT.md (v2)

Legend: **DOC** = read on the provider's published page/docs on 2026-11-05 · **MOCK** = automated test with fake responses ·
**LIVE** = real call. **Nothing below was LIVE-tested** (build sandbox cannot reach these hosts; `algospecial.com/api/ohlc.php` also answers
the fetch tool with *ROBOTS_DISALLOWED*). First real verification = first workflow run.

## Twelve Data (Forex, gold fallback)
| Item | Finding | Status |
|---|---|---|
| Free (Basic) limits | 8 API credits/min and **800/day** cap per account/key (support.twelvedata.com "API credits limits", twelvedata.com/pricing) | DOC |
| Cost | `time_series` = 1 credit per symbol per request (independent of `outputsize`) | DOC (third-party summaries + llms-full.txt) |
| Max rows | 5000 data points per request → 4800-candle bootstrap fits in ONE request | DOC (third-party) |
| Response | `meta{symbol,interval,...}` + `values[]{datetime,open,high,low,close}` + `status` | DOC |
| Timestamps | requested with `timezone=UTC`; `datetime` = candle open | assumed from API semantics, MOCK |
| Freshness | free tier data may be delayed "typically 1-15 min depending on the exchange" (third-party note) - UNVERIFIED for these FX symbols; the freshness gate (<=3 candles) is the safeguard | UNVERIFIED |
| Symbols on free plan | Forex listed on Basic; **XAU/USD on Basic is unverified** | UNVERIFIED |
| Headers | `api-credits-left` is read when present and recorded | DOC (third-party) |

### Quota calculation (free plan, per key)
* Scheduled: 7 pairs × 96 scans = **672 credits/day** (weekday figure; weekends are skipped = 0).
* Enforced limits: budget 800, retry reserve 80 → scheduled requests stop at **720**, leaving 48 spare scheduled + 80 retry credits.
* Bootstrap: +7 credits per key once (4800 candles = 1 request per pair). Manual re-runs in the same slot: 0 (cache hit).
* Gold: 96/day on its **own** key (separate account) or, only by explicit opt-in, on a forex key: 672 + 96 = 768 > 720 → ~48 scheduled requests would be refused late each day. Not recommended.
* 4 keys ≠ automatically feasible: it is feasible **only if** the four keys are four separate Basic accounts/keys each with its own 800/day, and each key sees 672 + retries. A single shared account would give 800 total and be insufficient (28×96 = 2688/day). Twelve Data's terms on multiple free accounts are not assessed here.
* Per-minute: each scan sends 7 requests/key (limit 8) - OK; retries wait ≥61 s on a rate-limit error.

## AlgoSpecial (XAUUSD `XAUUSD`, NAS100 `USTEC`)
| Question | Finding | Status |
|---|---|---|
| Live or historical? | Pages are titled **"Historical Data API"**; "Updated 2026-11-02 · Data refreshed from the AlgoSpecial catalog"; XAUUSD M15 dataset ends **2026-10-30** (3 days before the page date). **Not documented as a live feed.** | DOC |
| M15 supported? | Yes, `tf=M15` (M1…MN1) | DOC |
| Timestamp / timezone | `YYYY.MM.DD HH:MM:SS`, **"broker server time, not UTC"**, offset NOT stated | DOC |
| Auth / limits | none; 60 req/min, 1000 req/day per IP; ≤366 days, ≤50 000 rows per request | DOC |
| Missing candles | "expected market-closure gaps remain visible"; audits list gaps/weekend bars | DOC |
| OHLC accuracy | A-grade quality report (97/100) for historical M15; no live accuracy claim | DOC |
| Instrument | XAUUSD "Exness-style source feed", 3 decimals. USTEC = "Nasdaq 100 CFD" broker quote | DOC |
| JSON | `{"data":[{Timestamp,Open,High,Low,Close,TickVolume,Spread}]}` | DOC (page's Python example) |

**Handling:** disabled until `ALGOSPECIAL_UTC_OFFSET_HOURS` is set explicitly (never assumed UTC). Wrong offsets show up as future-dated candles
(rejected) or stale data (rejected). Because of the refresh lag it will normally be rejected as stale; it is kept only because the spec prefers it.

## OANDA v20 (optional; XAU_USD, NAS100_USD)
Public v20 candle endpoint (`/v3/instruments/{instrument}/candles?granularity=M15&price=M&count=N`, Bearer token, RFC3339 UTC times,
response echoes `instrument` and `granularity`, which the parser checks). **Not live-tested.** CFD availability depends on account region
(not offered to US residents). Practice accounts are free; practice-feed freshness vs. live is unverified.

## XAUUSD status
Primary AlgoSpecial → rejected/disabled (historical). Live candidates: Twelve Data `XAU/USD` (dedicated key; plan availability unverified) or OANDA `XAU_USD`.
If none yields valid fresh data XAUUSD is reported **unavailable**; no signal is produced.

## NAS100 status
Only CFD sources are accepted: AlgoSpecial `USTEC` (historical → will normally be stale) and OANDA `NAS100_USD`. Yahoo `^NDX`, QQQ, NQ futures were
removed from the code. **Without an OANDA token (or another verified CFD feed added by you) NAS100 will be unavailable.** Symbol identity is checked via
provider-echoed instrument names where available and a price-band sanity check (2 000–100 000); this cannot prove an instrument is a CFD - provider choice does.

## Market sessions
FX/gold: Sun 17:00 → Fri 17:00 New York (DST-aware); index: same plus 17:00-18:00 NY daily break. Holidays are not modelled (shown as stale/missing data).
