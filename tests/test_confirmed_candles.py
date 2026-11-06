from candle_manager import Candle
from config import Settings
from conftest import NOW, LAST_CLOSED, STEP, make_series
from database import Database
from test_scanner import build, FakeTG


def crash_forming():
    s = make_series(LAST_CLOSED, 120, None, forming=True)
    s[-1] = Candle(s[-1].ts, 100, 100.5, 90, 95)                 # forming candle already shows the setup
    return s


def test_default_is_confirmed_only():
    assert Settings().evaluate_forming is False
    tg = FakeTG(); sc, _ = build({"EURUSD": crash_forming()}, tg=tg)
    sc.run()
    assert tg.sent == []


def test_provisional_sent_only_when_explicitly_enabled_then_not_repeated_when_confirmed(tmp_path):
    path = str(tmp_path / "s.db")
    s = Settings(); s.evaluate_forming = True
    tg = FakeTG()
    sc, db = build({"EURUSD": crash_forming()}, db=Database(path), tg=tg, settings=s)
    sc.run(); db.close()
    assert len(tg.sent) == 1 and "PROVISIONAL" in tg.sent[0]
    # next scan: the same candle is now closed (and its final values are the same setup)
    closed_now = crash_forming()
    closed_now.append(Candle(closed_now[-1].ts + STEP, 95, 95.4, 94.6, 95))
    tg2 = FakeTG()
    sc2, db2 = build({"EURUSD": closed_now}, db=Database(path), tg=tg2, now=NOW + STEP)
    r = sc2.run()
    assert tg2.sent == [] and r["summary"]["dup"] == 1             # same signal id -> confirmed version is blocked


def test_provisional_signal_id_equals_confirmed_signal_id():
    from signal_manager import signal_from_event
    ev = {"type": "Long", "candle_ts": LAST_CLOSED, "event_id": "7", "entry": 1, "sl": .9, "tp": 1.1}
    a = signal_from_event(dict(ev, provisional=True), "EURUSD", "twelvedata", "", NOW)
    b = signal_from_event(ev, "EURUSD", "twelvedata", "", NOW + STEP)
    assert a.signal_id == b.signal_id


def test_open_candle_is_never_cached_or_treated_as_closed():
    sc, db = build({"EURUSD": make_series(LAST_CLOSED, 120, None, forming=True)}, tg=FakeTG())
    sc.run()
    assert db.latest_ts("EURUSD@twelvedata") == LAST_CLOSED        # forming candle (LAST_CLOSED+STEP) not stored
