import pytest
from candle_manager import Candle
from config import ALL_INSTRUMENTS, Settings
from data_providers.api_manager import ApiManager
from data_providers.base import FallbackRouter, Backend, ProviderError
from database import Database
from scanner import Scanner, write_report
from conftest import NOW, LAST_CLOSED, STEP, make_series
from datetime import datetime, timezone


SCALE = {"XAUUSD": 20.0, "NAS100": 200.0}   # realistic price levels for the symbol-sanity check


class Feed(Backend):
    """Serves a prepared series for chosen instruments; records calls."""
    def __init__(self, name, series_by_inst, default=None, fail=()):
        self.name, self.d, self.default, self.fail, self.calls = name, series_by_inst, default, set(fail), []
    def fetch(self, inst, now, since):
        self.calls.append(inst)
        if inst in self.fail:
            raise ProviderError("simulated API outage")
        if inst in self.d:
            return self.d[inst]
        return make_series(LAST_CLOSED, 120, scale=SCALE.get(inst, 1.0))


class FakeTG:
    last_ambiguous = False
    def __init__(self, ok=True): self.ok, self.sent = ok, []
    configured = True
    def send(self, text):
        if not self.ok: return False, "HTTP 500"
        self.sent.append(text); return True, None


def build(series, db=None, tg=None, now=NOW, settings=None, fail=(), insts=None):
    s = settings or Settings()
    s.min_history_candles, s.history_candles = 100, 120      # synthetic series are 120 candles long
    db = db or Database(":memory:")
    calm = make_series(LAST_CLOSED, 120)
    feed = Feed("twelvedata", series, calm, fail)
    api = ApiManager(s, keys={1: "a", 2: "b", 3: "c", 4: "d", 5: "g"}, sleep=lambda x: None, usage_store=db)
    router = FallbackRouter([Feed("algospecial", series, calm, fail)], 3)
    sc = Scanner(s, db, api, tg or FakeTG(), router, router, now, insts or ALL_INSTRUMENTS, twelve_backend=feed)
    sc.feed, sc.router_feed = feed, router.backends[0]
    return sc, db


@pytest.mark.parametrize("pos", [0, 1, 2, 3])
def test_signal_detected_on_each_of_the_four_closed_window_candles(pos):
    tg = FakeTG()
    sc, _ = build({"EURUSD": make_series(LAST_CLOSED, 120, pos)}, tg=tg)
    rep = sc.run()
    assert rep["summary"]["buy"] == 1 and rep["summary"]["sent"] == 1 and len(tg.sent) == 1
    assert "Symbol: EURUSD" in tg.sent[0] and "Direction: BUY" in tg.sent[0]


def test_signal_older_than_window_is_not_alerted():
    tg = FakeTG()
    sc, _ = build({"EURUSD": make_series(LAST_CLOSED, 125, 4)}, tg=tg)   # 5th closed candle back
    rep = sc.run()
    assert tg.sent == [] and rep["summary"]["buy"] == 0


def test_forming_candle_ignored_by_default_and_provisional_when_enabled():
    series = make_series(LAST_CLOSED, 120, None, forming=True)
    # forming candle is a crash bar
    series[-1] = Candle(series[-1].ts, 100, 100.5, 90, 95)
    tg = FakeTG()
    sc, _ = build({"EURUSD": series}, tg=tg)
    sc.run()
    assert tg.sent == []                                  # Pine only acts on confirmed bars
    s = Settings(); s.evaluate_forming = True
    tg2 = FakeTG()
    sc, _ = build({"EURUSD": series}, tg=tg2, settings=s)
    sc.run()
    assert len(tg2.sent) == 1 and "PROVISIONAL" in tg2.sent[0]


def test_duplicate_prevented_across_runs_and_delivery_failure_retried(tmp_path):
    path = str(tmp_path / "state.db")
    series = {"EURUSD": make_series(LAST_CLOSED, 120, 1)}
    # run 1: Telegram down -> not marked delivered
    sc, db = build(series, db=Database(path), tg=FakeTG(ok=False))
    r1 = sc.run(); db.close()
    assert r1["summary"]["tg_fail"] == 1 and r1["summary"]["sent"] == 0
    # run 2 (15 min later, new process): retried and delivered
    tg = FakeTG()
    now2 = NOW + STEP
    series2 = {"EURUSD": make_series(LAST_CLOSED + STEP, 121, 2)}
    sc, db = build(series2, db=Database(path), tg=tg, now=now2)
    r2 = sc.run(); db.close()
    assert r2["summary"]["sent"] == 1 and len(tg.sent) == 1
    # run 3: same signal still inside the window -> blocked as duplicate
    tg3 = FakeTG()
    series3 = {"EURUSD": make_series(LAST_CLOSED + 2 * STEP, 122, 3)}
    sc, db = build(series3, db=Database(path), tg=tg3, now=NOW + 2 * STEP)
    r3 = sc.run()
    assert tg3.sent == [] and r3["summary"]["dup"] == 1 and r3["summary"]["sent"] == 0


def test_multiple_signals_in_one_scan_sent_separately():
    tg = FakeTG()
    sc, _ = build({"EURUSD": make_series(LAST_CLOSED, 120, 0), "GBPJPY": make_series(LAST_CLOSED, 120, 2, "up"),
                   "XAUUSD": make_series(LAST_CLOSED, 120, 1, scale=20.0)}, tg=tg)
    rep = sc.run()
    assert len(tg.sent) == 3 and rep["summary"]["buy"] == 2 and rep["summary"]["sell"] == 1


def test_all_30_instruments_scanned_and_reported():
    sc, _ = build({})
    rep = sc.run()
    assert rep["summary"]["total_instruments"] == 30 and rep["summary"]["scanned"] == 30
    assert {r["instrument"] for r in rep["instruments"]} == set(ALL_INSTRUMENTS)
    assert all(r["evaluated"] for r in rep["instruments"])


def test_api_failure_is_reported_not_counted_as_scanned():
    sc, _ = build({}, fail=("EURUSD",))
    rep = sc.run()
    r = [x for x in rep["instruments"] if x["instrument"] == "EURUSD"][0]
    assert r["status"] == "failed_api" and not r["evaluated"] and rep["summary"]["api_err"] == 1
    assert rep["summary"]["scanned"] == 29 and rep["summary"]["not_scanned"][0]["instrument"] == "EURUSD"


def test_bad_ohlc_is_data_quality_failure():
    bad = make_series(LAST_CLOSED, 120)
    bad[-3] = Candle(bad[-3].ts, 1, 0.5, 0.4, 1)
    sc, _ = build({"EURUSD": bad})
    rep = sc.run()
    assert rep["summary"]["dq_err"] == 1


def test_stale_data_gives_no_signal_even_with_old_valid_setup():
    stale = make_series(LAST_CLOSED - 30 * STEP, 120, 0)
    tg = FakeTG()
    sc, _ = build({"EURUSD": stale}, tg=tg)
    rep = sc.run()
    assert tg.sent == [] and rep["summary"]["failed"] >= 1


def test_weekend_is_skipped_without_api_calls():
    sat = int(datetime(2026, 11, 7, 12, 7, tzinfo=timezone.utc).timestamp())
    sc, _ = build({}, now=sat)
    rep = sc.run()
    assert rep["summary"]["skipped"] == 30 and rep["summary"]["scanned"] == 0


def test_delayed_start_still_catches_signal_in_window():
    # scheduled 11:45 run starts 28 minutes late; candle 11:30 still inside window
    tg = FakeTG()
    late = LAST_CLOSED + STEP + 13 * 60          # 12:13 -> forming 12:00, closed back to 11:00
    sc, _ = build({"EURUSD": make_series(LAST_CLOSED, 120, 1)}, tg=tg, now=late)
    sc.run()
    assert len(tg.sent) == 1


def test_dry_run_sends_nothing_and_marks_nothing():
    s = Settings(); s.dry_run = True
    tg = FakeTG()
    sc, db = build({"EURUSD": make_series(LAST_CLOSED, 120, 0)}, tg=tg, settings=s)
    rep = sc.run()
    assert tg.sent == [] and db.count_signals() == 0


def test_report_files_written(tmp_path):
    sc, _ = build({"EURUSD": make_series(LAST_CLOSED, 120, 0)})
    m, j = write_report(sc.run(), str(tmp_path), NOW)
    txt = open(m).read()
    assert "Total Instruments: 30" in txt and "EURUSD" in txt
