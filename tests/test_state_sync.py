import os, subprocess
import pytest
from database import Database
from state_sync import StateSync, StateSyncError
from signal_manager import signal_from_event
from conftest import NOW, LAST_CLOSED, STEP, make_series
from test_scanner import build, FakeTG

pytestmark = pytest.mark.skipif(subprocess.run(["git", "--version"], capture_output=True).returncode != 0, reason="git missing")


@pytest.fixture
def remote(tmp_path):
    r = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(r)], check=True)
    return str(r)


def runner(tmp_path, name, remote):
    d = tmp_path / name; d.mkdir()
    return StateSync(remote, str(d / "state.db"), sha_file=str(d / ".sha")), d


def test_first_run_has_no_state_then_push_and_new_runner_restores(tmp_path, remote):
    s1, d1 = runner(tmp_path, "run1", remote)
    assert s1.restore() is False
    db = Database(s1.db_path)
    sig = signal_from_event({"type": "Long", "candle_ts": LAST_CLOSED, "event_id": "1", "entry": 1, "sl": .9, "tp": 1.1}, "EURUSD", "twelvedata", "", NOW)
    db.register_pending(sig); db.mark_delivered(sig.signal_id)
    s1.push(db); db.close()
    s2, d2 = runner(tmp_path, "run2", remote)                    # fresh runner, nothing on disk
    assert s2.restore() is True
    db2 = Database(s2.db_path)
    assert db2.get_signal_status(sig.signal_id) == "delivered"   # duplicate prevention survives a new runner


def test_single_orphan_commit_no_history_growth(tmp_path, remote):
    s, _ = runner(tmp_path, "r", remote); s.restore()
    db = Database(s.db_path)
    for _ in range(3):
        s.push(db)
    n = subprocess.run(["git", "--git-dir", remote, "rev-list", "--count", "state"], capture_output=True, text=True).stdout.strip()
    assert n == "1"


def test_overlapping_run_cannot_overwrite_newer_state(tmp_path, remote):
    a, _ = runner(tmp_path, "a", remote); a.restore(); Database(a.db_path).close(); a.push(None)
    b, _ = runner(tmp_path, "b", remote); b.restore()            # both start from the same state
    a2 = StateSync(remote, a.db_path, sha_file=a.sha_file); a2.push(None)    # A pushes first
    with pytest.raises(StateSyncError, match="rejected"):
        b.push(None)                                             # B's push is refused (lease), nothing lost


def test_restore_failure_is_an_error_not_an_empty_state(tmp_path):
    s = StateSync(str(tmp_path / "does-not-exist"), str(tmp_path / "x.db"), sha_file=str(tmp_path / ".sha"))
    with pytest.raises(StateSyncError):
        s.restore()


def test_token_never_in_error_text(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_supersecret123")
    s = StateSync("https://x-access-token:ghs_supersecret123@127.0.0.1:1/none.git", str(tmp_path / "x.db"), sha_file=str(tmp_path / ".s"))
    with pytest.raises(StateSyncError) as e:
        s.restore()
    assert "ghs_supersecret123" not in str(e.value)


# ---------- ledger ordering around Telegram (crash-safety) ----------
def test_sending_is_persisted_before_telegram_and_delivered_after():
    seen = []
    tg = FakeTG()
    sc, db = build({"EURUSD": make_series(LAST_CLOSED, 120, 1)}, tg=tg)
    sid = []
    def cp():
        rows = db.conn.execute("SELECT signal_id,status FROM signals").fetchall()
        seen.append((len(tg.sent), rows[0][1] if rows else None))
    sc.checkpoint = cp
    sc.run()
    assert seen == [(0, "sending"), (1, "delivered")]


class Crash(Exception):
    pass


class CrashingTG(FakeTG):
    def send(self, text):
        self.sent.append(text)
        raise Crash()                                            # runner dies right after the message left


def test_crash_after_send_resends_once_flagged_as_possible_repeat(tmp_path):
    path = str(tmp_path / "s.db")
    series = {"EURUSD": make_series(LAST_CLOSED, 120, 1)}
    sc, db = build(series, db=Database(path), tg=CrashingTG())
    with pytest.raises(Crash):
        sc.run()
    assert db.count_signals("sending") == 1
    db.close()
    tg = FakeTG()
    sc2, db2 = build({"EURUSD": make_series(LAST_CLOSED + STEP, 121, 2)}, db=Database(path), tg=tg, now=NOW + STEP)
    sc2.run()
    assert len(tg.sent) == 1 and "possible repeat" in tg.sent[0]
    assert db2.count_signals("delivered") == 1
    sc3, _ = build({"EURUSD": make_series(LAST_CLOSED + 2 * STEP, 122, 3)}, db=db2, tg=FakeTG(), now=NOW + 2 * STEP)
    r = sc3.run()
    assert r["summary"]["dup"] == 1 and r["summary"]["sent"] == 0


def test_ambiguous_failure_stays_sending_definite_failure_returns_to_pending():
    class AmbTG(FakeTG):
        last_ambiguous = True
        def send(self, t): return False, "timeout"
    sc, db = build({"EURUSD": make_series(LAST_CLOSED, 120, 0)}, tg=AmbTG(ok=False))
    sc.run()
    assert db.count_signals("sending") == 1
    sc, db = build({"EURUSD": make_series(LAST_CLOSED, 120, 0)}, tg=FakeTG(ok=False))
    sc.run()
    assert db.count_signals("pending") == 1
