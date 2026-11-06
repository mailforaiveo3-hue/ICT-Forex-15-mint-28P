# API_DOCUMENTATION.md - internal interfaces (v2.1)

* `Backend.available(instrument) -> (bool, reason)` and `Backend.fetch(instrument, now_ts, since_ts) -> list[Candle]`; candle `ts` = open time, UTC epoch seconds.
* `ApiManager` (`data_providers/api_manager.py`)
  * `route(instrument) -> (physical_key_id, bucket)`: pairs -> keys 1-4 ("forex"), XAUUSD -> key 5 / explicit share ("gold").
  * `call(kid, func, attempts=4, bucket)`: quota check (first attempt <= budget-reserve, retries <= budget), per-key sliding 60 s limiter, stats, exponential backoff (2,4,8 s; 61 s after a per-minute 429); `QuotaExceeded` never retried and flagged for the UTC day.
  * Stats in SQLite `api_stats(day,key_id,metric)`: `requests_forex`, `requests_gold`, `rate_limit_events`, `failed`, `cache_hits`, `quota_exhausted`, `credits_left_reported` (from the `api-credits-left` header when present).
  * `usage_report()` -> per-key requests, used/budget, remaining, reserve, projected scheduled load, rate-limit events, failures, cache hits.
* `FallbackRouter` - ordered backends, skips unavailable ones, freshness gate, returns `FetchResult(candles, provider, note, attempts)`; all-quota failure raises `QuotaExceeded`.
* `validate_candles(candles, now_ts, instrument)`, `missing_expected_slots(...)` (`candle_manager.py`).
* `PO3Engine(params, state).process(closed_candles) -> new entry events`; `.to_state()` JSON.
* `Scanner.run() -> {"summary", "instruments", "api_usage"}`. Statuses: `scanned`, `skipped_market_closed`, `skipped_quota`, `unavailable` (XAUUSD/NAS100 without a valid provider), `failed_api`, `failed_data_quality`, `failed_internal`.
* Ledger statuses: `pending`, `sending`, `delivered`. Signal ID: `INSTRUMENT|BUY/SELL|M15|<candle UTC yyyymmddThhmm>|PO3-<PO3 start epoch>`.
* `state_sync.py restore|push` - orphan-branch persistence with `--force-with-lease`.
* `tools/compare_tradingview.py ohlc.csv signals.csv` - demonstrate parity against a TradingView export.
* CLI: `python main.py [--dry-run] [--only EURUSD,XAUUSD]`.
* `Scanner.checkpoint` (callable): called after `sending` is set and after `delivered` is set. If it raises, `Scanner` raises `CheckpointError(phase, signal_id, cause, message_sent)` internally, `run()` aborts and returns `summary["aborted"] = {phase: "pre-send"|"post-send", signal_id, reason, message_sent, possible_repeat_risk}`; `main.py` exits with code 2. `main.make_checkpoint()` does not catch `StateSyncError`.
* Signal status in reports: `sent`, `not_sent_checkpoint_failed` (pre-send), `sent_state_not_persisted` (post-send), `telegram_failed_will_retry`, `duplicate_blocked`.
