"""Entry point: python main.py [--dry-run] [--only EURUSD,XAUUSD]"""
import argparse
import os
import sys
import time

from config import Settings, telegram_creds, ALL_INSTRUMENTS
from data_providers.api_manager import ApiManager
from data_providers.gold_provider import build_gold_router
from data_providers.nas100_provider import build_nas100_router
from database import Database
from logger import get_logger
from scanner import Scanner, write_report
from telegram_bot import TelegramNotifier

log = get_logger()


def make_checkpoint(db):
    """In GitHub Actions (STATE_SYNC_CHECKPOINT=1) push the state right before/after each Telegram send.
    Failures are NOT swallowed: StateSyncError propagates to the scanner, which aborts safely
    (before a send: the message is not sent; after a send: reported as a possible-repeat risk)."""
    if os.environ.get("STATE_SYNC_CHECKPOINT") != "1":
        return None
    from state_sync import StateSync
    sync = StateSync.from_env()

    def cp():
        sync.push(db)
    return cp


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="scan but do not send/mark Telegram signals")
    ap.add_argument("--only", help="comma separated instruments")
    a = ap.parse_args(argv)
    s = Settings.from_env()
    if a.dry_run:
        s.dry_run = True
    db = Database(s.db_path)
    api = ApiManager(s, usage_store=db)
    token, chat = telegram_creds()
    tg = TelegramNotifier(token, chat)
    if not tg.configured and not s.dry_run:
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing: signals cannot be delivered")
    now = int(time.time())
    insts = [x.strip().upper() for x in a.only.split(",")] if a.only else ALL_INSTRUMENTS
    sc = Scanner(s, db, api, tg, build_gold_router(s, api), build_nas100_router(s), now, insts,
                 checkpoint=make_checkpoint(db))
    report = sc.run()
    mpath, _ = write_report(report, s.report_dir, now)
    sm = report["summary"]
    log.info(f"Scan done: scanned={sm['scanned']}/{sm['total_instruments']} failed={sm['failed']} "
             f"unavailable={sm['unavailable']} quota_skipped={sm['quota_skipped']} market_closed={sm['skipped']} "
             f"cache_hits={sm['cache_hits']} sent={sm['sent']} dup={sm['dup']} tg_fail={sm['tg_fail']} report={mpath}")
    ab = sm.get("aborted")
    if ab:
        log.error(f"SCAN ABORTED - mandatory state persistence failed ({ab['phase']}): {ab['reason']}")
        if ab["message_sent"]:
            log.error(f"POSSIBLE-REPEAT RISK: {ab['signal_id']} WAS sent to Telegram but its 'delivered' state is NOT persisted; "
                      "unless the final state push succeeds, the next run will re-send it flagged 'possible repeat'.")
        else:
            log.error(f"{ab['signal_id']} was NOT sent (no Telegram call was made).")
    for n in sm["not_scanned"]:
        log.warning(f"NOT SCANNED: {n['instrument']} [{n['status']}] {str(n['reason'])[:160]}")
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a") as f:
            f.write(open(mpath).read())
    db.close()
    if ab:
        return 2          # makes the GitHub Actions run fail: mandatory state persistence failed
    # otherwise non-zero only if NOTHING could be scanned although markets were open
    open_cnt = sm["total_instruments"] - sm["skipped"]
    return 1 if open_cnt > 0 and sm["scanned"] == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
