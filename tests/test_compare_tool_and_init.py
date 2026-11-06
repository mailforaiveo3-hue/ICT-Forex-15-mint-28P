import csv, os, sys
from datetime import datetime, timezone
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools"))
import compare_tradingview as cmp
from candle_manager import Candle
from config import Settings
from database import Database
from po3_strategy import PO3Engine, Params
from conftest import NOW, LAST_CLOSED, STEP, make_series
from test_parity_reference import synth, T0
from test_scanner import build, FakeTG


def write(tmp, bars, signals, iso=False):
    o, s = tmp / "o.csv", tmp / "s.csv"
    with open(o, "w", newline="") as f:
        w = csv.writer(f); w.writerow(["time", "open", "high", "low", "close"])
        for t, *r in bars:
            w.writerow([datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if iso else t, *r])
    with open(s, "w", newline="") as f:
        w = csv.writer(f); w.writerow(["time", "side"])
        for t, side in signals:
            w.writerow([datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if iso else t, side])
    return str(o), str(s)


def test_compare_tool_reports_identical_and_detects_differences(tmp_path):
    bars = synth(2, 1500)
    eng = PO3Engine(Params()); eng.process([Candle(*b) for b in bars])
    sigs = [(e["candle_ts"], "Buy" if e["type"] == "Long" else "Sell") for e in eng.events]
    assert len(sigs) > 3
    for iso in (False, True):
        o, s = write(tmp_path, bars, sigs, iso)
        assert cmp.main([o, s, "--skip-first", "0"]) == 0
    o, s = write(tmp_path, bars, sigs[:-1] + [(sigs[-1][0] + 900, sigs[-1][1])])
    assert cmp.main([o, s, "--skip-first", "0"]) == 1


# ---------- history / PO3 initialisation ----------
def test_state_reinitialised_when_strategy_settings_change(tmp_path):
    path = str(tmp_path / "s.db")
    sc, db = build({"EURUSD": make_series(LAST_CLOSED, 120, None)}, db=Database(path), tg=FakeTG())
    sc.run(); db.close()
    s2 = Settings(); s2.algorithm_mode = "Big Manipulation"
    sc2, db2 = build({"EURUSD": make_series(LAST_CLOSED + STEP, 121, None)}, db=Database(path), tg=FakeTG(), now=NOW + STEP, settings=s2)
    r = [x for x in sc2.run()["instruments"] if x["instrument"] == "EURUSD"][0]
    assert r["state_initialised_from_history"] is True


def test_state_continues_across_runs_and_is_not_reinitialised(tmp_path):
    path = str(tmp_path / "s.db")
    sc, db = build({"EURUSD": make_series(LAST_CLOSED, 120)}, db=Database(path), tg=FakeTG())
    r1 = [x for x in sc.run()["instruments"] if x["instrument"] == "EURUSD"][0]
    db.close()
    sc2, db2 = build({"EURUSD": make_series(LAST_CLOSED + STEP, 121)}, db=Database(path), tg=FakeTG(), now=NOW + STEP)
    r2 = [x for x in sc2.run()["instruments"] if x["instrument"] == "EURUSD"][0]
    assert r1["state_initialised_from_history"] is True and r2["state_initialised_from_history"] is False


def test_state_older_than_cache_triggers_reinit(tmp_path):
    db = Database(":memory:")
    sc, _ = build({"EURUSD": make_series(LAST_CLOSED, 120)}, db=db, tg=FakeTG()); sc.run()
    st = db.load_state("EURUSD@twelvedata"); st["last_ts"] = 5      # state from long ago, before cached history
    db.save_state("EURUSD@twelvedata", st)
    sc2, _ = build({"EURUSD": make_series(LAST_CLOSED + STEP, 121)}, db=db, tg=FakeTG(), now=NOW + STEP)
    r = [x for x in sc2.run()["instruments"] if x["instrument"] == "EURUSD"][0]
    assert r["state_initialised_from_history"] is True and r["status"] == "scanned"


def test_bootstrap_does_not_alert_on_old_history_signals():
    # a signal 30 candles ago is found while initialising but is outside the 5-candle window
    tg = FakeTG()
    sc, _ = build({"EURUSD": make_series(LAST_CLOSED, 150, 30)}, tg=tg)
    rep = sc.run()
    assert tg.sent == [] and rep["summary"]["buy"] == 0


def test_full_4800_history_bootstrap_matches_whole_series_engine():
    bars = synth(5, 4800)
    cs = [Candle(*b) for b in bars]
    one = PO3Engine(Params()); one.process(cs)
    assert one.bar_index == 4799                                   # all 4800 bars processed (max_distance_to_last_bar)
    two = PO3Engine(Params()); two.process(cs[:3000]); two = PO3Engine(Params(), two.to_state()); two.process(cs)
    assert [e["candle_ts"] for e in one.events] == [e["candle_ts"] for e in two.events]
