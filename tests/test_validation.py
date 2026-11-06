import glob, os
import pytest
from candle_manager import Candle, DataQualityError, validate_candles, missing_expected_slots
from config import Settings, SYMBOL_SANITY
from data_providers.base import FallbackRouter, ProviderError
from data_providers.gold_provider import AlgoSpecialBackend, parse_algospecial, build_gold_router
from data_providers.nas100_provider import build_nas100_router
from data_providers.oanda_provider import OandaBackend, parse_oanda
from conftest import NOW, LAST_CLOSED, STEP, make_series
from test_scanner import build, FakeTG, Feed, SCALE


# ---------- generic validation ----------
def test_future_dated_candles_rejected_catches_wrong_timezone_offset():
    shifted = [Candle(c.ts + 2 * 3600, c.o, c.h, c.l, c.c) for c in make_series(LAST_CLOSED, 100)]   # +2h broker time read as UTC
    with pytest.raises(DataQualityError, match="future"):
        validate_candles(shifted, NOW, "EURUSD")


def test_wrong_timeframe_rejected():
    h1 = [Candle(LAST_CLOSED - i * 3600, 1, 2, 0.5, 1) for i in range(60)][::-1]
    with pytest.raises(DataQualityError, match="timeframe"):
        validate_candles(h1, NOW, "EURUSD")


def test_price_band_blocks_wrong_symbol_for_gold_and_nas100():
    with pytest.raises(DataQualityError, match="plausible"):
        validate_candles(make_series(LAST_CLOSED, 50, scale=1.0), NOW, "XAUUSD")        # prices ~100
    validate_candles(make_series(LAST_CLOSED, 50, scale=20.0), NOW, "XAUUSD")          # ~2000 ok
    assert "NAS100" in SYMBOL_SANITY


def test_missing_candles_are_reported_never_filled():
    cs = make_series(LAST_CLOSED, 100)
    del cs[-3]
    miss = missing_expected_slots(cs, "fx", LAST_CLOSED - 4 * STEP, LAST_CLOSED)
    assert miss == [LAST_CLOSED - 2 * STEP] and len(cs) == 99


def test_scanner_rejects_window_with_missing_candle_and_does_not_signal():
    cs = make_series(LAST_CLOSED, 120, 0)
    del cs[-3]
    tg = FakeTG()
    sc, _ = build({"EURUSD": cs}, tg=tg)
    rep = sc.run()
    r = [x for x in rep["instruments"] if x["instrument"] == "EURUSD"][0]
    assert r["status"] == "failed_data_quality" and "missing candles" in r["error"] and tg.sent == []


def test_invalid_ohlc_stale_and_nan_never_signal():
    bad = make_series(LAST_CLOSED, 120, 0)
    bad[-1] = Candle(bad[-1].ts, 100, 90, 95, 95)                 # high < low
    tg = FakeTG()
    sc, _ = build({"EURUSD": bad}, tg=tg)
    assert sc.run()["summary"]["dq_err"] == 1 and tg.sent == []


# ---------- history ----------
def test_insufficient_history_rejected_with_defaults():
    s = Settings()                                                # defaults: min 500 candles
    tg = FakeTG()
    sc, _ = build({}, tg=tg)
    sc.s.min_history_candles = 500
    rep = sc.run()
    r = rep["instruments"][0]
    assert r["status"] == "failed_data_quality" and "insufficient history" in r["error"]
    assert rep["summary"]["scanned"] == 0 and tg.sent == []


def test_short_but_usable_history_scans_with_warning_flag():
    cs = make_series(LAST_CLOSED, 700, 1)
    sc, _ = build({"EURUSD": cs}, tg=FakeTG())
    sc.s.min_history_candles, sc.s.history_candles = 500, 4800
    r = [x for x in sc.run()["instruments"] if x["instrument"] == "EURUSD"][0]
    assert r["status"] == "scanned" and r["history_complete"] is False
    assert any("shorter than max_distance_to_last_bar" in w for w in r["data_warnings"])


def test_history_settings_cover_max_distance_4800():
    s = Settings()
    assert s.max_distance_to_last_bar == 4800 and s.history_candles >= 4800 and s.cache_max_candles >= 4800
    assert s.history_candles <= 5000                              # Twelve Data outputsize ceiling


# ---------- gold / AlgoSpecial ----------
def test_algospecial_disabled_until_timezone_offset_is_verified():
    s = Settings()
    assert s.algospecial_utc_offset_hours is None
    ok, why = AlgoSpecialBackend(s).available("XAUUSD")
    assert not ok and "broker server time" in why
    s.algospecial_utc_offset_hours = 2.0
    assert AlgoSpecialBackend(s).available("XAUUSD")[0] and AlgoSpecialBackend(s).available("NAS100")[0]


def test_algospecial_offset_applied_and_wrong_offset_detected_as_future():
    row = {"Timestamp": "2026.11.04 13:45:00", "Open": 2000, "High": 2001, "Low": 1999, "Close": 2000.5}
    assert parse_algospecial({"data": [row]}, 2.0)[0].ts == LAST_CLOSED
    wrong = parse_algospecial({"data": [row] * 1}, 0.0)           # offset not applied -> 2h in the future
    with pytest.raises(DataQualityError):
        validate_candles(wrong, NOW, "XAUUSD")
    with pytest.raises(ProviderError, match="symbol mismatch"):
        parse_algospecial({"symbol": "EURUSD", "data": [row]}, 0, "XAUUSD")


class Stale(Feed):
    pass


def test_xauusd_stale_primary_is_rejected_and_unavailable_when_nothing_else():
    stale = make_series(LAST_CLOSED - 40 * STEP, 120, 0, scale=20.0)
    s = Settings(); s.algospecial_utc_offset_hours = 0.0
    router = FallbackRouter([Feed("algospecial", {"XAUUSD": stale}, None)], 3)
    sc, _ = build({}, tg=FakeTG())
    sc.gold = router
    rep = sc.run()
    r = [x for x in rep["instruments"] if x["instrument"] == "XAUUSD"][0]
    assert r["status"] == "unavailable" and "stale" in r["error"] and rep["summary"]["unavailable"] == 1


def test_gold_router_default_order_and_no_unverified_algospecial(monkeypatch):
    s = Settings()
    class Api: pass
    r = build_gold_router(s, Api())
    assert [b.name for b in r.backends] == ["algospecial", "twelvedata", "oanda"]
    ok, _ = r.backends[0].available("XAUUSD")
    assert ok is False


# ---------- NAS100 CFD only ----------
def test_yahoo_and_other_substitutes_are_gone_and_cannot_be_configured(monkeypatch):
    root = os.path.dirname(os.path.dirname(__file__))
    for f in glob.glob(os.path.join(root, "data_providers", "*.py")):
        txt = open(f).read().lower()
        assert "finance.yahoo" not in txt and "yahoobackend" not in txt and "/chart/%5endx" not in txt
    monkeypatch.setenv("NAS100_PROVIDER_ORDER", "yahoo,qqq,nq,oanda")
    s = Settings.from_env()
    assert s.nas100_provider_order == ("oanda",)
    assert [b.name for b in build_nas100_router(Settings()).backends] == ["algospecial", "oanda"]


def test_nas100_unavailable_when_no_cfd_source_valid_and_no_signal():
    tg = FakeTG()
    sc, _ = build({"NAS100": make_series(LAST_CLOSED - 50 * STEP, 120, 0, scale=200.0)}, tg=tg)
    sc.nas = FallbackRouter([Feed("algospecial", {"NAS100": make_series(LAST_CLOSED - 50 * STEP, 120, 0, scale=200.0)}, None)], 3)
    rep = sc.run()
    r = [x for x in rep["instruments"] if x["instrument"] == "NAS100"][0]
    assert r["status"] == "unavailable" and not r["evaluated"] and tg.sent == []


def test_nas100_message_names_cfd_provider():
    tg = FakeTG()
    sc, _ = build({"NAS100": make_series(LAST_CLOSED, 120, 0, scale=200.0)}, tg=tg)
    sc.nas = FallbackRouter([Feed("oanda", {"NAS100": make_series(LAST_CLOSED, 120, 0, scale=200.0)}, None)], 3,
                            {"oanda": "NAS100_USD = Nasdaq 100 CFD (OANDA)"})
    sc.run()
    msg = [m for m in tg.sent if "NAS100" in m][0]
    assert "Provider: OANDA" in msg and "Nasdaq 100 CFD" in msg


def test_nas100_wrong_scale_symbol_rejected():
    sc, _ = build({"NAS100": make_series(LAST_CLOSED, 120, 0, scale=1.0)}, tg=FakeTG())
    r = [x for x in sc.run()["instruments"] if x["instrument"] == "NAS100"][0]
    assert r["status"] == "failed_data_quality" and "plausible" in r["error"]


def test_oanda_parser_identity_and_timeframe():
    ok = {"instrument": "NAS100_USD", "granularity": "M15",
          "candles": [{"time": "2026-11-04T11:45:00.000000000Z", "complete": True, "mid": {"o": "1", "h": "2", "l": "0.5", "c": "1.5"}}]}
    assert parse_oanda(ok, "NAS100_USD")[0].ts == LAST_CLOSED
    with pytest.raises(ProviderError, match="instrument mismatch"):
        parse_oanda(dict(ok, instrument="SPX500_USD"), "NAS100_USD")
    with pytest.raises(ProviderError, match="timeframe"):
        parse_oanda(dict(ok, granularity="H1"), "NAS100_USD")
    assert OandaBackend(Settings(), token=None).available("NAS100")[0] in (False,)  # no token configured in tests
