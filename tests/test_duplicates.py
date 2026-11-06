from database import Database
from duplicate_manager import DuplicateManager
from signal_manager import signal_from_event
from conftest import LAST_CLOSED, NOW

EV = {"type": "Long", "candle_ts": LAST_CLOSED, "event_id": "9", "entry": 1.0, "sl": 0.9, "tp": 1.1}


def sig():
    return signal_from_event(EV, "EURUSD", "twelvedata", "", NOW)


def test_pending_is_not_delivered_and_delivered_persists_across_restart(tmp_path):
    path = str(tmp_path / "s.db")
    db = Database(path); d = DuplicateManager(db)
    d.register_pending(sig())
    assert not d.is_delivered(sig().signal_id)           # failed Telegram => retried later
    d.mark_delivered(sig().signal_id)
    db.close()
    d2 = DuplicateManager(Database(path))                # new process / new workflow run
    assert d2.is_delivered(sig().signal_id)


def test_register_is_idempotent_and_does_not_downgrade(tmp_path):
    db = Database(":memory:"); d = DuplicateManager(db)
    d.register_pending(sig()); d.mark_delivered(sig().signal_id); d.register_pending(sig())
    assert db.get_signal_status(sig().signal_id) == "delivered" and db.count_signals() == 1


def test_candle_and_state_persistence(tmp_path):
    from candle_manager import Candle
    path = str(tmp_path / "s.db")
    db = Database(path)
    db.save_candles("EURUSD@twelvedata", [Candle(900 * i, 1, 2, 1, 1) for i in range(1, 11)], 5)
    db.save_state("EURUSD@twelvedata", {"last_ts": 5})
    db.close(); db = Database(path)
    assert len(db.load_candles("EURUSD@twelvedata")) == 5 and db.latest_ts("EURUSD@twelvedata") == 9000
    assert db.load_state("EURUSD@twelvedata") == {"last_ts": 5}
