import pytest
from config import Settings, FOREX_PAIRS_BY_KEY
from data_providers.api_manager import ApiManager, SCANS_PER_DAY
from data_providers.base import QuotaExceeded, ProviderError, RateLimitError
from database import Database
from conftest import NOW, LAST_CLOSED, STEP, make_series
from test_scanner import build, FakeTG

KEYS = {1: "a", 2: "b", 3: "c", 4: "d", 5: "g"}


def test_daily_budget_is_always_enforced_default_800_and_cannot_be_disabled(monkeypatch):
    assert Settings().twelve_daily_budget == 800 and Settings().twelve_retry_reserve == 80
    monkeypatch.setenv("TWELVE_DATA_DAILY_BUDGET", "0")
    assert Settings.from_env().twelve_daily_budget == 1          # 0 no longer means "unlimited"


def test_expected_workload_fits_under_free_plan_per_key():
    s = Settings()
    for k, pairs in FOREX_PAIRS_BY_KEY.items():
        scheduled = len(pairs) * SCANS_PER_DAY
        assert scheduled == 672
        assert scheduled <= s.twelve_daily_budget - s.twelve_retry_reserve     # leaves reserve for retries


def test_scheduled_requests_stop_at_budget_minus_reserve_retries_may_use_reserve():
    s = Settings(); s.twelve_daily_budget, s.twelve_retry_reserve = 10, 3
    api = ApiManager(s, keys=dict(KEYS), sleep=lambda x: None)
    for _ in range(7):
        api.call(1, lambda: 1)                                   # 7 = budget - reserve
    with pytest.raises(QuotaExceeded):
        api.call(1, lambda: 1)                                   # 8th first attempt refused
    n = {"c": 0}
    def flaky():
        n["c"] += 1
        raise ProviderError("x")
    api2 = ApiManager(s, keys=dict(KEYS), sleep=lambda x: None, usage_store=api.stats)   # same counters
    with pytest.raises(QuotaExceeded):
        api2.call(1, flaky)                                      # scheduled limit already hit
    for _ in range(2):
        api.stats.stat_add(api.day(), 2, "requests_forex", 0)
    # key 2 untouched and independent
    assert api.call(2, lambda: "ok") == "ok" and api.used_today(2) == 1


def test_retry_can_consume_reserve_but_never_exceed_budget():
    s = Settings(); s.twelve_daily_budget, s.twelve_retry_reserve = 6, 3
    api = ApiManager(s, keys=dict(KEYS), sleep=lambda x: None)
    for _ in range(2):
        api.call(1, lambda: 1)                                   # used 2 (limit for first attempts = 3)
    n = {"c": 0}
    def flaky():
        n["c"] += 1
        raise ProviderError("down")
    with pytest.raises(Exception):
        api.call(1, flaky, attempts=6)
    assert api.used_today(1) <= 6                                # never beyond budget


def test_usage_persists_across_runners_and_quota_flag_blocks_without_request(tmp_path):
    path = str(tmp_path / "s.db")
    db = Database(path); api = ApiManager(Settings(), keys=dict(KEYS), sleep=lambda x: None, usage_store=db)
    api.call(1, lambda: 1); api.call(1, lambda: 1)
    def q(): raise QuotaExceeded("daily")
    with pytest.raises(QuotaExceeded):
        api.call(3, q)
    db.close()
    db2 = Database(path)                                          # brand-new runner, restored DB
    api2 = ApiManager(Settings(), keys=dict(KEYS), sleep=lambda x: None, usage_store=db2)
    assert api2.used_today(1) == 2 and api2.used_today(3) == 1
    called = []
    with pytest.raises(QuotaExceeded):
        api2.call(3, lambda: called.append(1))                   # provider said exhausted -> no new request today
    assert called == []


def test_per_minute_limit_is_independent_per_key():
    t = {"now": 1000.0}; sleeps = []
    def sl(x): sleeps.append(x); t["now"] += x
    s = Settings(); s.twelve_rpm = 2
    api = ApiManager(s, keys=dict(KEYS), sleep=sl, clock=lambda: t["now"])
    api.call(1, lambda: 1); api.call(1, lambda: 1)
    api.call(2, lambda: 1); api.call(2, lambda: 1)               # key 2 has its own window
    assert sleeps == []
    api.call(1, lambda: 1)                                       # key 1's 3rd within 60s -> must wait
    assert sleeps and sum(sleeps) >= 59


def test_rate_limit_event_counted_and_waits():
    sleeps = []
    api = ApiManager(Settings(), keys=dict(KEYS), sleep=sleeps.append)
    n = {"c": 0}
    def f():
        n["c"] += 1
        if n["c"] == 1: raise RateLimitError("minute")
        return 1
    assert api.call(1, f) == 1 and sleeps[0] >= 61
    assert api.stats.stat_get(api.day(), 1, "rate_limit_events") == 1


def test_gold_requests_counted_in_own_bucket():
    api = ApiManager(Settings(), keys=dict(KEYS), sleep=lambda x: None)
    api.call(5, lambda: 1, bucket="gold"); api.call(1, lambda: 1)
    d = api.day()
    assert api.stats.stat_get(d, 5, "requests_gold") == 1 and api.stats.stat_get(d, 1, "requests_forex") == 1
    assert api.stats.stat_get(d, 1, "requests_gold") == 0


def test_scanner_reports_quota_skips_never_silently():
    sc, db = build({})
    api = sc.api
    api.stats.stat_set(api.day(), 4, "quota_exhausted", 1)       # key 4 exhausted earlier today
    class Quota:   # twelve backend honouring the manager like the real one
        def available(self, i): return True, ""
        def fetch(self, inst, now, since):
            kid, bucket = api.route(inst)
            return api.call(kid, lambda: make_series(LAST_CLOSED, 120), bucket=bucket)
    sc.twelve = Quota()
    rep = sc.run()
    skipped = {r["instrument"] for r in rep["instruments"] if r["status"] == "skipped_quota"}
    assert skipped == set(FOREX_PAIRS_BY_KEY[4]) and len(skipped) == 7
    assert {n["instrument"] for n in rep["summary"]["not_scanned"]} == skipped
    assert rep["summary"]["scanned"] == 23 and rep["summary"]["quota_skipped"] == 7
    k4 = [k for k in rep["api_usage"]["keys"] if k["key"] == 4][0]
    assert k4["quota_exhausted"] is True and k4["remaining"] >= 0


def test_cache_hit_skips_api_in_same_slot_and_is_reported():
    sc, db = build({})
    sc.run()
    n_calls = len(sc.feed.calls) + len(sc.router_feed.calls)
    assert n_calls == 30
    sc2, _ = build({}, db=db)                                    # same slot, same DB (e.g. manual re-run)
    rep = sc2.run()
    assert sc2.feed.calls == [] and sc2.router_feed.calls == []
    assert rep["summary"]["cache_hits"] == 30 and rep["summary"]["scanned"] == 30
    assert sum(k["cache_hits"] for k in rep["api_usage"]["keys"]) >= 28


def test_next_slot_fetches_again():
    sc, db = build({})
    sc.run()
    sc2, _ = build({}, db=db, now=NOW + STEP)                    # new candle slot: cache no longer current
    sc2.feed.default = None
    rep = sc2.run()
    assert len(sc2.feed.calls) > 0


def test_usage_report_shape():
    sc, _ = build({})
    u = sc.run()["api_usage"]
    k1 = [k for k in u["keys"] if k["key"] == 1][0]
    for f in ("requests_forex", "requests_gold", "used_today", "budget", "remaining", "rate_limit_events",
              "failed_requests", "cache_hits", "projected_scheduled_per_day", "quota_exhausted"):
        assert f in k1
    assert k1["projected_scheduled_per_day"] == 672
