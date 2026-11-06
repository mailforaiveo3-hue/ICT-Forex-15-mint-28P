"""Mandatory state persistence around Telegram sends (pending -> sending -> delivered preserved)."""
import json, os, types
import pytest
import main as main_mod
import scanner as scanner_mod
from data_providers.base import FetchResult
from database import Database
from state_sync import StateSyncError
from conftest import NOW, LAST_CLOSED, STEP, make_series
from test_scanner import build, FakeTG


class SpyTG(FakeTG):
    def __init__(self, ok=True):
        super().__init__(ok); self.calls = 0
    def send(self, text):
        self.calls += 1
        return super().send(text)


def two_signals():
    return {"EURUSD": make_series(LAST_CLOSED, 120, 0), "GBPUSD": make_series(LAST_CLOSED, 120, 1)}


# ---------- 1) pre-send checkpoint failure => Telegram never called ----------
def test_pre_send_checkpoint_failure_never_calls_telegram_and_aborts():
    tg = SpyTG()
    sc, db = build(two_signals(), tg=tg)
    def cp(): raise StateSyncError("push rejected")
    sc.checkpoint = cp
    rep = sc.run()                                                  # aborts safely, does not raise
    assert tg.calls == 0 and tg.sent == []                          # NO Telegram call at all
    ab = rep["summary"]["aborted"]
    assert ab["phase"] == "pre-send" and ab["message_sent"] is False and "push rejected" in ab["reason"]
    assert rep["summary"]["sent"] == 0
    # ledger: restored to pending (message never left), not left as 'sending'
    assert db.count_signals("pending") == 1 and db.count_signals("sending") == 0 and db.count_signals("delivered") == 0


def test_pre_send_failure_stops_all_later_signals_and_instruments_are_reported():
    tg = SpyTG()
    sc, _ = build(two_signals(), tg=tg)
    sc.checkpoint = lambda: (_ for _ in ()).throw(StateSyncError("down"))
    rep = sc.run()
    assert tg.calls == 0                                            # the second signal is not sent either
    statuses = {r["instrument"]: r["status"] for r in rep["instruments"]}
    assert "aborted_not_processed" in statuses.values()
    assert any(n["status"] == "aborted_not_processed" for n in rep["summary"]["not_scanned"])


def test_pre_send_failure_keeps_earlier_sending_status_if_it_was_ambiguous():
    db = Database(":memory:")
    series = {"EURUSD": make_series(LAST_CLOSED, 120, 1)}
    sc, db = build(series, db=db, tg=SpyTG())
    sc.checkpoint = lambda: (_ for _ in ()).throw(StateSyncError("x"))
    sc.run()
    sid = db.conn.execute("SELECT signal_id FROM signals").fetchone()[0]
    db.set_status(sid, "sending")                                   # earlier ambiguous attempt
    sc2, _ = build(series, db=db, tg=SpyTG())
    sc2.checkpoint = lambda: (_ for _ in ()).throw(StateSyncError("x"))
    sc2.run()
    assert db.get_signal_status(sid) == "sending"                   # still ambiguous, not downgraded to pending


def test_workflow_unchanged_when_checkpoint_succeeds():
    seen, tg = [], SpyTG()
    sc, db = build({"EURUSD": make_series(LAST_CLOSED, 120, 1)}, tg=tg)
    sc.checkpoint = lambda: seen.append((tg.calls, db.conn.execute("SELECT status FROM signals").fetchone()[0]))
    rep = sc.run()
    assert seen == [(0, "sending"), (1, "delivered")] and rep["summary"]["aborted"] is None and rep["summary"]["sent"] == 1


# ---------- 2) post-send checkpoint failure => reported as possible-repeat risk ----------
def test_post_send_checkpoint_failure_is_reported_as_possible_repeat_risk(tmp_path):
    tg = SpyTG()
    remote = str(tmp_path / "persisted_remote.db")                  # what the 'state' branch would hold
    sc, db = build(two_signals(), tg=tg)
    n = {"c": 0}
    def cp():
        n["c"] += 1
        if n["c"] == 1:
            db.snapshot_to(remote)                                  # pre-send checkpoint succeeds (status 'sending')
        else:
            raise StateSyncError("network down after send")        # post-send checkpoint fails
    sc.checkpoint = cp
    rep = sc.run()
    ab = rep["summary"]["aborted"]
    assert tg.calls == 1                                            # message sent exactly once, then scan stops
    assert ab["phase"] == "post-send" and ab["message_sent"] is True and ab["possible_repeat_risk"] is True
    sig_status = [s["status"] for r in rep["instruments"] for s in r["signals"]]
    assert "sent_state_not_persisted" in sig_status and "sent" not in sig_status   # NOT reported as persisted/sent-ok
    # what is persisted remotely still says 'sending' -> the next run (new runner) will resend, flagged
    persisted = Database(remote)
    sid = persisted.conn.execute("SELECT signal_id FROM signals").fetchone()[0]
    assert persisted.get_signal_status(sid) == "sending"
    tg2 = SpyTG()
    series = {"EURUSD": make_series(LAST_CLOSED + STEP, 121, 1), "GBPUSD": make_series(LAST_CLOSED + STEP, 121, 2)}
    sc2, _ = build(series, db=persisted, tg=tg2, now=NOW + STEP)
    sc2.run()
    assert any("possible repeat" in m for m in tg2.sent)            # the unavoidable repeat, clearly flagged


def test_report_file_states_possible_repeat_risk(tmp_path):
    tg = SpyTG()
    sc, db = build({"EURUSD": make_series(LAST_CLOSED, 120, 1)}, tg=tg)
    n = {"c": 0}
    def cp():
        n["c"] += 1
        if n["c"] == 2: raise StateSyncError("boom")
    sc.checkpoint = cp
    m, j = scanner_mod.write_report(sc.run(), str(tmp_path), NOW)
    txt = open(m).read()
    assert "SCAN ABORTED" in txt and "POSSIBLE-REPEAT RISK" in txt and "NOT persisted" in txt


# ---------- 3) main.py: no swallowing, non-zero exit ----------
def test_make_checkpoint_propagates_state_sync_error(monkeypatch):
    import state_sync
    class FakeSync:
        def push(self, db): raise StateSyncError("rejected")
    monkeypatch.setenv("STATE_SYNC_CHECKPOINT", "1")
    monkeypatch.setattr(state_sync.StateSync, "from_env", staticmethod(lambda: FakeSync()))
    cp = main_mod.make_checkpoint(object())
    with pytest.raises(StateSyncError, match="rejected"):
        cp()


def test_make_checkpoint_disabled_outside_actions(monkeypatch):
    monkeypatch.delenv("STATE_SYNC_CHECKPOINT", raising=False)
    assert main_mod.make_checkpoint(object()) is None


def run_main(monkeypatch, tmp_path, checkpoint):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABCDEFGHIJKLMNOP"); monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    tg = SpyTG()
    monkeypatch.setattr(main_mod, "TelegramNotifier", lambda t, c: tg)
    monkeypatch.setattr(main_mod, "time", types.SimpleNamespace(time=lambda: NOW))
    monkeypatch.setattr(main_mod, "make_checkpoint", lambda db: checkpoint)
    monkeypatch.setattr(scanner_mod.Scanner, "_fetch_all", lambda self, insts: {
        i: FetchResult(make_series(LAST_CLOSED, 600, 1), "twelvedata", "", ["twelvedata: ok"]) for i in insts})
    code = main_mod.main(["--only", "EURUSD"])
    return code, tg


def test_main_exits_nonzero_and_sends_nothing_when_pre_send_checkpoint_fails(monkeypatch, tmp_path):
    def cp(): raise StateSyncError("rejected")
    code, tg = run_main(monkeypatch, tmp_path, cp)
    assert code == 2 and tg.calls == 0
    rep = json.load(open(next(p for p in (tmp_path / "reports").glob("scan_*.json"))))
    assert rep["summary"]["aborted"]["phase"] == "pre-send"


def test_main_exits_nonzero_on_post_send_failure_and_says_possible_repeat(monkeypatch, tmp_path, caplog):
    n = {"c": 0}
    def cp():
        n["c"] += 1
        if n["c"] == 2: raise StateSyncError("lost connection")
    code, tg = run_main(monkeypatch, tmp_path, cp)
    assert code == 2 and tg.calls == 1
    rep = json.load(open(next(p for p in (tmp_path / "reports").glob("scan_*.json"))))
    assert rep["summary"]["aborted"]["possible_repeat_risk"] is True


def test_main_exit_zero_when_checkpoints_succeed(monkeypatch, tmp_path):
    code, tg = run_main(monkeypatch, tmp_path, lambda: None)
    assert code == 0 and tg.calls == 1


def test_workflow_run_step_is_not_continue_on_error():
    import yaml
    wf = yaml.safe_load(open(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".github", "workflows", "scan.yml")))
    run = [s for s in wf["jobs"]["scan"]["steps"] if s.get("name") == "Run scanner"][0]
    assert "continue-on-error" not in run and run["run"].strip() == "python main.py"   # non-zero exit fails the run
