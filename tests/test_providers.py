import pytest
from config import (FOREX_PAIRS, FOREX_PAIRS_BY_KEY, PAIR_TO_KEY, ALL_INSTRUMENTS, OTHER_INSTRUMENTS, Settings)
from data_providers.api_manager import ApiManager
from data_providers.base import (FallbackRouter, Backend, ProviderError, RateLimitError, QuotaExceeded,
                                 freshness_problem)
from data_providers.twelve_data import parse_time_series, td_symbol, TwelveDataBackend
from candle_manager import Candle
from logger import redact
from conftest import NOW, LAST_CLOSED, STEP, make_series

EXPECTED = {
    1: "EURUSD GBPUSD USDJPY USDCHF USDCAD AUDUSD NZDUSD",
    2: "EURGBP EURJPY EURCHF EURCAD EURAUD EURNZD GBPJPY",
    3: "GBPCHF GBPCAD GBPAUD GBPNZD AUDJPY AUDCHF AUDCAD",
    4: "AUDNZD NZDJPY NZDCHF NZDCAD CADJPY CADCHF CHFJPY"}
KEYS = {1: "k1aaaaaaa", 2: "k2aaaaaaa", 3: "k3aaaaaaa", 4: "k4aaaaaaa", 5: "kgaaaaaaa"}


def test_exactly_28_unique_pairs_30_instruments_chfjpy_included():
    assert len(FOREX_PAIRS) == 28 and len(set(FOREX_PAIRS)) == 28
    assert "CHFJPY" in FOREX_PAIRS
    assert len(ALL_INSTRUMENTS) == 30 and len(set(ALL_INSTRUMENTS)) == 30
    assert OTHER_INSTRUMENTS == ["XAUUSD", "NAS100"] and not set(OTHER_INSTRUMENTS) & set(FOREX_PAIRS)


def test_four_keys_seven_pairs_each_exact_spec_and_covered_once():
    assert sorted(FOREX_PAIRS_BY_KEY) == [1, 2, 3, 4]
    for k, txt in EXPECTED.items():
        assert FOREX_PAIRS_BY_KEY[k] == txt.split() and len(FOREX_PAIRS_BY_KEY[k]) == 7
    flat = [p for v in FOREX_PAIRS_BY_KEY.values() for p in v]
    assert len(flat) == len(set(flat)) == 28
    assert sorted(flat) == sorted(" ".join(EXPECTED.values()).split())


def test_api_manager_routes_every_pair_to_its_key_and_gold_is_separate():
    api = ApiManager(Settings(), keys=dict(KEYS))
    for p in FOREX_PAIRS:
        kid, bucket, key = api.key_for(p)
        assert kid == PAIR_TO_KEY[p] and bucket == "forex" and key == KEYS[kid]
    kid, bucket, key = api.key_for("XAUUSD")
    assert (kid, bucket) == (5, "gold") and key == KEYS[5]          # dedicated key, not a forex key


def test_gold_without_dedicated_key_is_not_silently_charged_to_forex_keys():
    api = ApiManager(Settings(), keys={1: "a", 2: "b", 3: "c", 4: "d", 5: None})
    with pytest.raises(ProviderError, match="TWELVE_DATA_API_KEY_GOLD"):
        api.key_for("XAUUSD")
    s = Settings(); s.gold_twelve_share_key = 2                      # explicit opt-in, separate bucket
    kid, bucket, _ = ApiManager(s, keys=dict(KEYS)).key_for("XAUUSD")
    assert (kid, bucket) == (2, "gold")


def test_missing_key_reported():
    with pytest.raises(ProviderError, match="TWELVE_DATA_API_KEY_4"):
        ApiManager(Settings(), keys={1: "x", 4: None}).key_for("CHFJPY")


def test_retry_with_exponential_backoff_then_success():
    sleeps = []
    api = ApiManager(Settings(), keys=dict(KEYS), sleep=sleeps.append)
    n = {"c": 0}
    def f():
        n["c"] += 1
        if n["c"] < 3: raise ProviderError("boom")
        return "ok"
    assert api.call(1, f) == "ok" and n["c"] == 3
    assert 2 <= sleeps[0] < 3 and 4 <= sleeps[1] < 5
    assert api.stats.stat_get(api.day(), 1, "failed") == 2 and api.used_today(1) == 3


def test_parse_twelve_data_ok_errors_symbol_and_timeframe():
    ok = {"meta": {"symbol": "EUR/USD", "interval": "15min"}, "status": "ok",
          "values": [{"datetime": "2026-11-04 11:45:00", "open": "1.1", "high": "1.2", "low": "1.0", "close": "1.15"}]}
    c = parse_time_series(ok, "EUR/USD")[0]
    assert c.ts == LAST_CLOSED and c.h == 1.2
    with pytest.raises(ProviderError, match="symbol mismatch"):
        parse_time_series(ok, "GBP/USD")
    bad = dict(ok, meta={"symbol": "EUR/USD", "interval": "1h"})
    with pytest.raises(ProviderError, match="timeframe"):
        parse_time_series(bad, "EUR/USD")
    with pytest.raises(RateLimitError):
        parse_time_series({"code": 429, "message": "You have run out of API credits for the current minute", "status": "error"})
    with pytest.raises(QuotaExceeded):
        parse_time_series({"code": 429, "message": "You have run out of API credits for the day", "status": "error"})
    with pytest.raises(ProviderError):
        parse_time_series({"code": 400, "message": "symbol invalid", "status": "error"})
    with pytest.raises(ProviderError):
        parse_time_series({"status": "ok", "values": []})
    with pytest.raises(ProviderError):
        parse_time_series({"values": [{"datetime": "bad"}]})


class FakeResp:
    def __init__(self, code=200, js=None, headers=None): self.status_code, self._j, self.headers = code, js, headers or {}
    def json(self):
        if self._j is None: raise ValueError
        return self._j


class FakeHttp:
    def __init__(self, resp): self.resp, self.calls = resp, []
    def get(self, url, params=None, **kw):
        self.calls.append(params); return self.resp


def test_twelve_backend_incremental_size_credit_header_and_buckets():
    row = {"datetime": "2026-11-04 11:45:00", "open": "1", "high": "2", "low": "1", "close": "1"}
    http = FakeHttp(FakeResp(200, {"meta": {"symbol": "EUR/USD", "interval": "15min"}, "values": [row]},
                             {"api-credits-left": "123"}))
    api = ApiManager(Settings(), keys=dict(KEYS), sleep=lambda x: None)
    b = TwelveDataBackend(api, Settings(), http)
    b.fetch("EURUSD", NOW, None)
    b.fetch("EURUSD", NOW, LAST_CLOSED - 3 * STEP)
    assert http.calls[0]["outputsize"] == 4800 and http.calls[1]["outputsize"] < 15
    assert http.calls[0]["interval"] == "15min" and http.calls[0]["timezone"] == "UTC"
    d = api.day()
    assert api.stats.stat_get(d, 1, "requests_forex") == 2 and api.stats.stat_get(d, 1, "credits_left_reported") == 123
    assert api.stats.stat_get(d, 5, "requests_gold") == 0


class Fixed(Backend):
    def __init__(self, name, candles=None, err=None, avail=(True, "")): self.name, self.c, self.err, self.av = name, candles, err, avail
    def available(self, i): return self.av
    def fetch(self, i, now, since):
        if self.err: raise self.err
        return self.c


def test_freshness_and_router_fallback_and_unavailable_backends():
    stale = make_series(LAST_CLOSED - 20 * STEP, 60)
    fresh = make_series(LAST_CLOSED, 60)
    assert "old" in freshness_problem("XAUUSD", stale, NOW, 3) and freshness_problem("XAUUSD", fresh, NOW, 3) is None
    r = FallbackRouter([Fixed("algospecial", stale), Fixed("twelvedata", fresh)], 3).fetch("XAUUSD", NOW, lambda i, p: None)
    assert r.provider == "twelvedata" and "algospecial: stale" in r.attempts[0]
    r = FallbackRouter([Fixed("algospecial", avail=(False, "tz unverified")), Fixed("oanda", fresh)], 3).fetch("XAUUSD", NOW, lambda i, p: None)
    assert r.provider == "oanda" and "not available" in r.attempts[0]
    with pytest.raises(ProviderError) as e:
        FallbackRouter([Fixed("a", stale), Fixed("b", err=ProviderError("down"))], 3).fetch("XAUUSD", NOW, lambda i, p: None)
    assert len(e.value.attempts) == 2


def test_secret_redaction(monkeypatch):
    monkeypatch.setenv("TWELVE_DATA_API_KEY_4", "SECRETKEY123456")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABCdefGhIJKlmnoPQRstuVWxyz")
    monkeypatch.setenv("OANDA_API_TOKEN", "oandatoken-abcdef-123")
    t = redact("url?apikey=SECRETKEY123456&x=1 https://api.telegram.org/bot123456:ABCdefGhIJKlmnoPQRstuVWxyz/sendMessage "
               "Authorization: Bearer oandatoken-abcdef-123 https://x-access-token:ghs_abc123@github.com/x/y.git")
    for leak in ("SECRETKEY", "ABCdef", "oandatoken", "ghs_abc123"):
        assert leak not in t
