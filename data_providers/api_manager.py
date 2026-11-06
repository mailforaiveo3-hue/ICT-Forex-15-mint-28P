"""Twelve Data key allocation, ENFORCED daily budgets, per-key rate limiting, persisted usage, retry/backoff.

Physical keys: 1-4 (Forex, 7 pairs each) and 5 (optional dedicated XAU/USD key). Gold may instead
explicitly share a forex key (GOLD_TWELVE_SHARE_KEY=N); it is then still accounted in its own
'gold' bucket while the physical key's daily/minute limits are shared.
"""
import random
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

from config import PAIR_TO_KEY, FOREX_PAIRS_BY_KEY, FOREX_KEY_IDS, GOLD_KEY_ID, twelve_keys
from data_providers.base import ProviderError, RateLimitError, QuotaExceeded
from logger import get_logger

log = get_logger()
SCANS_PER_DAY = 96


class MemoryStats:
    """In-memory stand-in with the same interface as Database's stat_* methods (tests / no DB)."""
    def __init__(self):
        self.d = defaultdict(int)
    def stat_add(self, day, k, m, n=1): self.d[(day, k, m)] += n
    def stat_set(self, day, k, m, n): self.d[(day, k, m)] = n
    def stat_get(self, day, k, m): return self.d[(day, k, m)]
    def stat_total_requests(self, day, k): return self.d[(day, k, "requests_forex")] + self.d[(day, k, "requests_gold")]


class ApiManager:
    def __init__(self, settings, keys=None, sleep=time.sleep, clock=time.time, usage_store=None):
        self.s = settings
        self.keys = keys if keys is not None else twelve_keys()
        self.sleep, self.clock = sleep, clock
        self.stats = usage_store if usage_store is not None else MemoryStats()
        self.window = defaultdict(deque)
        self.lock = threading.Lock()
        self.run_usage = defaultdict(int)       # requests made in THIS run, per physical key
        self.allocation_log = []
        self.exhausted = set()

    # ---------------- allocation (fixed, never rotated) ----------------
    def route(self, instrument: str):
        """-> (physical_key_id, bucket). Raises ProviderError if no key can serve the instrument."""
        if instrument in PAIR_TO_KEY:
            return PAIR_TO_KEY[instrument], "forex"
        if instrument == "XAUUSD":
            share = self.s.gold_twelve_share_key
            if share:
                if share not in FOREX_KEY_IDS:
                    raise ProviderError(f"GOLD_TWELVE_SHARE_KEY must be 1-4, got {share}")
                return share, "gold"
            return GOLD_KEY_ID, "gold"
        raise ProviderError(f"{instrument} has no Twelve Data allocation")

    def key_for(self, instrument: str):
        kid, bucket = self.route(instrument)
        key = self.keys.get(kid)
        if not key:
            name = "TWELVE_DATA_API_KEY_GOLD" if kid == GOLD_KEY_ID else f"TWELVE_DATA_API_KEY_{kid}"
            raise ProviderError(f"{name} is not configured")
        self.allocation_log.append((instrument, kid, bucket))
        log.info(f"API allocation: {instrument} -> Twelve Data key #{kid} ({bucket} bucket)")
        return kid, bucket, key

    # ---------------- helpers ----------------
    def day(self):
        return datetime.fromtimestamp(self.clock(), timezone.utc).strftime("%Y-%m-%d")

    def used_today(self, kid):
        return self.stats.stat_total_requests(self.day(), kid)

    def remaining_today(self, kid):
        return max(0, self.s.twelve_daily_budget - self.used_today(kid))

    def _throttle(self, kid):
        rpm = max(1, self.s.twelve_rpm)
        while True:
            with self.lock:
                w = self.window[kid]
                now = self.clock()
                while w and now - w[0] >= 60:
                    w.popleft()
                if len(w) < rpm:
                    w.append(now)
                    return
                wait = max(0.5, 60 - (now - w[0]) + 0.2)
            self.sleep(wait)

    def _check_budget(self, kid, attempt):
        day = self.day()
        if kid in self.exhausted or self.stats.stat_get(day, kid, "quota_exhausted"):
            raise QuotaExceeded(f"key #{kid}: provider reported daily quota exhausted today")
        used = self.stats.stat_total_requests(day, kid)
        limit = self.s.twelve_daily_budget if attempt > 1 else self.s.twelve_daily_budget - self.s.twelve_retry_reserve
        if used >= limit:
            raise QuotaExceeded(f"key #{kid}: {used}/{self.s.twelve_daily_budget} credits used today "
                                f"({'retry' if attempt > 1 else 'scheduled'} limit {limit})")

    def note_credits_left(self, kid, left):
        try:
            self.stats.stat_set(self.day(), kid, "credits_left_reported", int(float(left)))
        except (TypeError, ValueError):
            pass

    def record_cache_hit(self, instrument):
        try:
            kid, _ = self.route(instrument)
        except ProviderError:
            return
        self.stats.stat_add(self.day(), kid, "cache_hits", 1)

    # ---------------- guarded call ----------------
    def call(self, kid, func, attempts=4, bucket="forex"):
        """Run func() under quota + throttle with exponential backoff. QuotaExceeded is never retried."""
        delay, last = 2.0, None
        for n in range(1, attempts + 1):
            day = self.day()
            self._check_budget(kid, n)          # raises QuotaExceeded -> instrument reported as skipped_quota
            self._throttle(kid)
            self.stats.stat_add(day, kid, f"requests_{bucket}", 1)
            self.run_usage[kid] += 1
            try:
                return func()
            except QuotaExceeded:
                self.exhausted.add(kid)
                self.stats.stat_set(day, kid, "quota_exhausted", 1)
                self.stats.stat_add(day, kid, "failed", 1)
                raise
            except RateLimitError as e:
                self.stats.stat_add(day, kid, "rate_limit_events", 1)
                last, wait = e, 61.0
            except ProviderError as e:
                last, wait = e, delay
            except Exception as e:
                last, wait = ProviderError(f"{type(e).__name__}: {e}"), delay
            self.stats.stat_add(day, kid, "failed", 1)
            if n < attempts:
                log.warning(f"Twelve Data key #{kid} attempt {n}/{attempts} failed ({last}); retry in {wait:.0f}s")
                self.sleep(wait + random.uniform(0, 0.5))
            delay *= 2
        raise last

    # ---------------- reporting ----------------
    def usage_report(self, skipped=None):
        day = self.day()
        rows = []
        for kid in (*FOREX_KEY_IDS, GOLD_KEY_ID):
            if kid == GOLD_KEY_ID and not self.keys.get(kid) and not self.s.gold_twelve_share_key:
                continue
            pairs = len(FOREX_PAIRS_BY_KEY.get(kid, []))
            rows.append({
                "key": kid, "instruments": pairs if pairs else 1,
                "requests_forex": self.stats.stat_get(day, kid, "requests_forex"),
                "requests_gold": self.stats.stat_get(day, kid, "requests_gold"),
                "used_today": self.used_today(kid), "budget": self.s.twelve_daily_budget,
                "remaining": self.remaining_today(kid), "retry_reserve": self.s.twelve_retry_reserve,
                "projected_scheduled_per_day": (pairs or 0) * SCANS_PER_DAY,
                "rate_limit_events": self.stats.stat_get(day, kid, "rate_limit_events"),
                "failed_requests": self.stats.stat_get(day, kid, "failed"),
                "cache_hits": self.stats.stat_get(day, kid, "cache_hits"),
                "quota_exhausted": bool(self.stats.stat_get(day, kid, "quota_exhausted")),
                "credits_left_reported": self.stats.stat_get(day, kid, "credits_left_reported") or None,
                "requests_this_run": self.run_usage.get(kid, 0)})
        return {"day_utc": day, "keys": rows, "skipped_instruments": skipped or []}
