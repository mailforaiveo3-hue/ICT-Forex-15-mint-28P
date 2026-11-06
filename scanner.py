"""Scan orchestration: fetch -> validate -> PO3 engine -> five-candle window -> dedupe -> Telegram."""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

from candle_manager import (DataQualityError, floor_slot, is_market_open, merge_candles, missing_expected_slots,
                            split_closed_forming, validate_candles, window_slots, utc_dt)
from config import (ALL_INSTRUMENTS, CANDLE_SECONDS, FOREX_PAIRS_BY_KEY, FOREX_KEY_IDS, instrument_class)
from data_providers.base import FetchResult, ProviderError, QuotaExceeded, StaleDataError, freshness_problem
from data_providers.twelve_data import TwelveDataBackend
from duplicate_manager import DuplicateManager
from logger import get_logger, redact
from po3_strategy import PO3Engine, Params
from signal_manager import fmt_pkt, format_message, signal_from_event

log = get_logger()


class CheckpointError(Exception):
    """Mandatory state persistence failed around a Telegram send.
    phase 'pre-send'  -> the message was NOT sent.
    phase 'post-send' -> the message WAS sent but 'delivered' could not be persisted (possible-repeat risk)."""
    def __init__(self, phase, signal_id, cause, message_sent):
        super().__init__(f"{phase} state checkpoint failed for {signal_id}: {cause}")
        self.phase, self.signal_id, self.cause, self.message_sent = phase, signal_id, str(cause), message_sent


def expected_open(instrument, now_ts) -> bool:
    """Open now, or open during the candle that just closed (so the last candle before a close is scanned)."""
    cls = instrument_class(instrument)
    return is_market_open(cls, now_ts) or is_market_open(cls, now_ts - CANDLE_SECONDS)


def params_signature(p: Params) -> str:
    return "|".join(str(x) for x in (p.algorithm_mode, p.breakout_method, p.tpsl_method, p.risk, p.tp_percent,
                                     p.sl_percent, p.rr, p.atr_len, p.accumulation_expand_mult))


class Scanner:
    def __init__(self, settings, db, api, notifier, gold_router, nas_router, now_ts=None,
                 instruments=None, twelve_backend=None, session=None, checkpoint=None):
        self.s, self.db, self.api, self.tg = settings, db, api, notifier
        self.gold, self.nas = gold_router, nas_router
        self.now = int(now_ts if now_ts is not None else time.time())
        self.instruments = instruments or ALL_INSTRUMENTS
        self.params = Params.from_settings(settings)
        self.dups = DuplicateManager(db)
        self.twelve = twelve_backend or TwelveDataBackend(api, settings, session)
        self.checkpoint = checkpoint or (lambda: None)   # persists state (git push) at critical points

    # ---------- fetch phase (parallel per key group, sequential inside a group) ----------
    def _fetch_one(self, inst, since):
        lookup = lambda i, p: since.get((i, p))
        if inst == "XAUUSD":
            return self.gold.fetch(inst, self.now, lookup)
        if inst == "NAS100":
            return self.nas.fetch(inst, self.now, lookup)
        ok, why = self.twelve.available(inst) if hasattr(self.twelve, "available") else (True, "")
        if not ok:
            raise ProviderError(why)
        candles = self.twelve.fetch(inst, self.now, lookup(inst, "twelvedata"))
        prob = freshness_problem(inst, candles, self.now, self.s.max_stale_candles)
        if prob:
            e = StaleDataError(f"twelvedata: stale/unusable ({prob})")
            e.attempts = [str(e)]
            raise e
        return FetchResult(candles, "twelvedata", "", ["twelvedata: ok"])

    def _fetch_group(self, insts, since):
        out = {}
        for i in insts:
            try:
                out[i] = self._fetch_one(i, since)
            except Exception as e:   # recorded per instrument, never aborts the scan
                out[i] = e
        return out

    def _cache_hit(self, inst):
        """Skip the API when the cache already holds the newest CLOSED candle (cache stores closed candles only)."""
        if self.s.evaluate_forming:
            return None
        prov = self.db.get_meta(f"provider:{inst}")
        if not prov:
            return None
        newest = self.db.latest_ts(f"{inst}@{prov}")
        if newest is not None and newest >= floor_slot(self.now) - CANDLE_SECONDS:
            return FetchResult([], prov, self.db.get_meta(f"note:{inst}", ""), ["cache hit (no request made)"])
        return None

    def _fetch_all(self, insts):
        since, hits = {}, {}
        for i in insts:
            for prov in ("twelvedata", "algospecial", "oanda"):
                since[(i, prov)] = self.db.latest_ts(f"{i}@{prov}")
            h = self._cache_hit(i)
            if h:
                hits[i] = h
                self.api.record_cache_hit(i)
        todo = [i for i in insts if i not in hits]
        groups = [[i for i in todo if i in FOREX_PAIRS_BY_KEY[k]] for k in FOREX_KEY_IDS]
        groups += [[i] for i in todo if i in ("XAUUSD", "NAS100")]
        groups = [g for g in groups if g]
        results = dict(hits)
        if groups:
            with ThreadPoolExecutor(max_workers=len(groups)) as ex:
                for r in ex.map(lambda g: self._fetch_group(g, since), groups):
                    results.update(r)
        return results

    # ---------- per instrument ----------
    def _process(self, inst, res: FetchResult, rep):
        key = f"{inst}@{res.provider}"
        rep.update(provider=res.provider, provider_note=res.note, attempts=res.attempts)
        cached = self.db.load_candles(key)
        merged = merge_candles(cached, res.candles)
        clean, warns = validate_candles(merged, self.now, inst)       # raises DataQualityError
        closed, forming = split_closed_forming(clean, self.now)
        cls = instrument_class(inst)
        n_closed = len(closed)
        rep.update(candles=n_closed, data_warnings=warns, history_complete=n_closed >= self.s.history_candles,
                   latest_candle=fmt_pkt(closed[-1].ts) if closed else None,
                   freshness_candles=((floor_slot(self.now) - CANDLE_SECONDS - closed[-1].ts) // CANDLE_SECONDS) if closed else None)
        if n_closed < self.s.min_history_candles:
            raise DataQualityError(f"insufficient history: {n_closed} closed candles < minimum "
                                   f"{self.s.min_history_candles} (provider returned too little)")
        if n_closed < self.s.history_candles:
            warns.append(f"history shorter than max_distance_to_last_bar ({n_closed}/{self.s.history_candles}); "
                         "PO3 initialised from what the provider returned (no candles fabricated)")
        slots = window_slots(self.now, self.s.window_candles)
        if self.s.max_window_gaps_reject:
            miss = missing_expected_slots(closed, cls, slots[-1], slots[1])
            miss = [t for t in miss if t > closed[0].ts]
            if miss:
                raise DataQualityError("missing candles in the eligible window: " +
                                       ", ".join(fmt_pkt(t) for t in miss))
        # ---- validated: persist CLOSED candles only (a forming candle is never cached) ----
        self.db.save_candles(key, closed, self.s.cache_max_candles)
        self.db.set_meta(f"provider:{inst}", res.provider)
        self.db.set_meta(f"note:{inst}", res.note)
        closed = closed[-self.s.cache_max_candles:]
        # ---- PO3 state (re)initialisation ----
        state = self.db.load_state(key)
        sig = params_signature(self.params)
        if state is not None:
            if state.get("params_sig") != sig:
                log.warning(f"{inst}: strategy settings changed - re-initialising PO3 state from history")
                state = None
            elif state.get("last_ts") is not None and state["last_ts"] < closed[0].ts:
                log.warning(f"{inst}: saved PO3 state older than cached history - re-initialising")
                state = None
        rep["state_initialised_from_history"] = state is None
        eng = PO3Engine(self.params, state)
        eng.process(closed)                               # strategy actually evaluated here
        st = eng.to_state(); st["params_sig"] = sig
        self.db.save_state(key, st)
        eligible_closed = set(slots[1:])
        cands = [e for e in eng.events if e["candle_ts"] in eligible_closed]
        if self.s.evaluate_forming and forming is not None and forming.ts == slots[0]:
            tmp = eng.clone()
            for e in tmp.process(closed + [forming]):
                cands.append(dict(e, provisional=True))
        rep["window_slots"] = [fmt_pkt(t) for t in slots]
        rep["evaluated"] = True
        return cands

    def _deliver(self, sig, rep, tot):
        st = self.dups.status(sig.signal_id)
        if st == "delivered":
            tot["dup"] += 1
            rep["signals"].append({"id": sig.signal_id, "status": "duplicate_blocked"})
            return
        if self.s.dry_run:
            rep["signals"].append({"id": sig.signal_id, "status": "dry_run_not_sent", "message": format_message(sig)})
            return
        sig.possible_repeat = (st == "sending")
        self.dups.register_pending(sig)
        self.dups.mark_sending(sig.signal_id)
        try:
            self.checkpoint()                # 'sending' must be persisted BEFORE the message leaves
        except Exception as e:
            # Not persisted -> do NOT send. Restore the previous ledger status (the message never left).
            self.db.set_status(sig.signal_id, "sending" if st == "sending" else "pending")
            rep["signals"].append({"id": sig.signal_id, "status": "not_sent_checkpoint_failed", "error": redact(e)})
            raise CheckpointError("pre-send", sig.signal_id, redact(e), False) from e
        ok, err = self.tg.send(format_message(sig))
        if ok:
            self.dups.mark_delivered(sig.signal_id)
            tot["sent"] += 1; tot["tg_ok"] += 1
            try:
                self.checkpoint()            # 'delivered' persisted immediately
            except Exception as e:
                rep["signals"].append({"id": sig.signal_id, "status": "sent_state_not_persisted", "error": redact(e)})
                raise CheckpointError("post-send", sig.signal_id, redact(e), True) from e
            rep["signals"].append({"id": sig.signal_id, "status": "sent"})
        else:
            self.dups.mark_failed(sig.signal_id, getattr(self.tg, "last_ambiguous", False))
            tot["tg_fail"] += 1
            rep["signals"].append({"id": sig.signal_id, "status": "telegram_failed_will_retry", "error": err})
            log.error(f"Telegram failed for {sig.signal_id}: {err}")

    def run(self):
        start = time.time()
        reps = {i: {"instrument": i, "status": "pending", "signals": [], "evaluated": False,
                    "rejection": None, "error": None} for i in self.instruments}
        tot = dict(buy=0, sell=0, sent=0, dup=0, tg_ok=0, tg_fail=0, api_err=0, dq_err=0, scanned=0,
                   failed=0, skipped=0, quota_skipped=0, unavailable=0, cache_hits=0)
        to_fetch = []
        for i in self.instruments:
            if expected_open(i, self.now):
                to_fetch.append(i)
            else:
                reps[i].update(status="skipped_market_closed", rejection="market closed (no data requested)")
                tot["skipped"] += 1
        fetched = self._fetch_all(to_fetch) if to_fetch else {}
        aborted = None
        try:
            for i in to_fetch:
                rep, res = reps[i], fetched[i]
                if isinstance(res, Exception):
                    attempts = getattr(res, "attempts", [])
                    if isinstance(res, QuotaExceeded):
                        rep.update(status="skipped_quota", error=redact(res), attempts=attempts)
                        tot["quota_skipped"] += 1
                    elif i in ("XAUUSD", "NAS100"):
                        rep.update(status="unavailable", error=redact(res), attempts=attempts)
                        tot["unavailable"] += 1
                    else:
                        rep.update(status="failed_api", error=redact(res), attempts=attempts)
                        tot["api_err"] += 1; tot["failed"] += 1
                    log.error(f"{i}: NOT SCANNED ({rep['status']}): {redact(res)}")
                    continue
                if res.attempts and res.attempts[0].startswith("cache hit"):
                    tot["cache_hits"] += 1
                try:
                    cands = self._process(i, res, rep)
                except DataQualityError as e:
                    rep.update(status="failed_data_quality", error=str(e))
                    tot["dq_err"] += 1; tot["failed"] += 1
                    log.error(f"{i}: data quality rejection: {e}")
                    continue
                except Exception as e:
                    rep.update(status="failed_internal", error=redact(f"{type(e).__name__}: {e}"))
                    tot["failed"] += 1
                    log.exception(f"{i}: unexpected error")
                    continue
                rep["status"] = "scanned"
                tot["scanned"] += 1
                if not cands:
                    rep["rejection"] = "no PO3 entry on any of the 5 window candles"
                for ev in sorted(cands, key=lambda e: e["candle_ts"]):
                    sig = signal_from_event(ev, i, res.provider, res.note, self.now)
                    tot["buy" if sig.direction == "BUY" else "sell"] += 1
                    self._deliver(sig, rep, tot)
        except CheckpointError as ce:
            aborted = dict(phase=ce.phase, signal_id=ce.signal_id, reason=ce.cause, message_sent=ce.message_sent,
                           possible_repeat_risk=ce.message_sent)
            log.error(f"ABORTING SCAN: {ce}")
            for r in reps.values():
                if r["status"] == "pending":
                    r.update(status="aborted_not_processed", error="scan aborted: mandatory state checkpoint failed")
        self.db.prune()
        not_scanned = [{"instrument": r["instrument"], "status": r["status"], "reason": r.get("error") or r.get("rejection")}
                       for r in reps.values() if r["status"] != "scanned"]
        usage = self.api.usage_report(skipped=[n for n in not_scanned if n["status"] != "skipped_market_closed"])
        summary = dict(tot, start=fmt_pkt(int(start)), end=fmt_pkt(int(time.time())),
                       total_instruments=len(self.instruments), aborted=aborted,
                       not_scanned=[n for n in not_scanned if n["status"] != "skipped_market_closed"])
        return {"summary": summary, "instruments": list(reps.values()), "api_usage": usage}


def write_report(report, report_dir, now_ts):
    os.makedirs(report_dir, exist_ok=True)
    stamp = utc_dt(now_ts).strftime("%Y%m%d_%H%M")
    jpath = os.path.join(report_dir, f"scan_{stamp}.json")
    with open(jpath, "w") as f:
        json.dump(report, f, indent=1, default=str)
    s = report["summary"]
    lines = ["# PO3 Scan Report", "",
             f"Scan Start (PKT): {s['start']}", f"Scan End (PKT): {s['end']}", "",
             f"Total Instruments: {s['total_instruments']}",
             f"Successfully Scanned: {s['scanned']}", f"Failed: {s['failed']}",
             f"Unavailable (no valid provider): {s['unavailable']}", f"Skipped - quota: {s['quota_skipped']}",
             f"Skipped - market closed: {s['skipped']}", f"Cache hits (no API request): {s['cache_hits']}", "",
             f"BUY Signals Found: {s['buy']}", f"SELL Signals Found: {s['sell']}",
             f"New Signals Sent: {s['sent']}", f"Duplicate Signals Blocked: {s['dup']}",
             f"Telegram Successful: {s['tg_ok']}", f"Telegram Failed: {s['tg_fail']}",
             f"API Errors: {s['api_err']}", f"Data Quality Errors: {s['dq_err']}", ""]
    if s.get("aborted"):
        ab = s["aborted"]
        lines += ["## SCAN ABORTED - mandatory state persistence failed", "",
                  f"* Phase: **{ab['phase']}**  Signal: `{ab['signal_id']}`", f"* Reason: {ab['reason']}"]
        lines.append("* Telegram message: **NOT sent**." if not ab["message_sent"] else
                     "* Telegram message: **SENT**, but its 'delivered' state is **NOT persisted** -> POSSIBLE-REPEAT RISK "
                     "(the next run re-sends it flagged 'possible repeat' unless the final state push succeeds).")
        lines.append("")
    if s["not_scanned"]:
        lines += ["## NOT SCANNED (needs attention)", ""]
        lines += [f"* **{n['instrument']}** - {n['status']}: {str(n['reason'])[:200]}" for n in s["not_scanned"]]
        lines.append("")
    u = report["api_usage"]
    lines += [f"## Twelve Data usage (UTC day {u['day_utc']})", "",
              "| Key | Fx req | Gold req | Used/Budget | Remaining | Rate-limit ev. | Failed | Cache hits | This run | Quota hit |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for k in u["keys"]:
        lines.append(f"| #{k['key']} | {k['requests_forex']} | {k['requests_gold']} | {k['used_today']}/{k['budget']} | "
                     f"{k['remaining']} | {k['rate_limit_events']} | {k['failed_requests']} | {k['cache_hits']} | "
                     f"{k['requests_this_run']} | {'YES' if k['quota_exhausted'] else 'no'} |")
    lines += ["", "## Instruments", "",
              "| Instrument | Status | Provider | Latest candle (PKT) | Candles | Signals | Notes |", "|---|---|---|---|---|---|---|"]
    for r in report["instruments"]:
        sigs = ", ".join(f"{x['status']}" for x in r["signals"]) or "-"
        note = r.get("error") or r.get("rejection") or ""
        lines.append(f"| {r['instrument']} | {r['status']} | {r.get('provider','-')} | {r.get('latest_candle','-')} | "
                     f"{r.get('candles','-')} | {sigs} | {str(note)[:110]} |")
    mpath = os.path.join(report_dir, f"scan_{stamp}.md")
    open(mpath, "w").write("\n".join(lines) + "\n")
    with open(os.path.join(report_dir, f"api_usage_{stamp}.json"), "w") as f:
        json.dump(u, f, indent=1)
    return mpath, jpath
