"""Candle model, validation, 15-minute grid helpers, and market-session rules."""
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from config import CANDLE_SECONDS, SYMBOL_SANITY

NY = ZoneInfo("America/New_York")
UTC = timezone.utc


@dataclass(frozen=True)
class Candle:
    ts: int  # candle OPEN time, epoch seconds UTC
    o: float
    h: float
    l: float
    c: float


class DataQualityError(Exception):
    pass


def floor_slot(ts: int) -> int:
    return ts - (ts % CANDLE_SECONDS)


def utc_dt(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, UTC)


def split_closed_forming(candles, now_ts: int):
    """A candle opened at t is closed once now >= t + 15 min."""
    closed = [c for c in candles if c.ts + CANDLE_SECONDS <= now_ts]
    forming = [c for c in candles if c.ts + CANDLE_SECONDS > now_ts and c.ts <= now_ts]
    return closed, (forming[-1] if forming else None)


def window_slots(now_ts: int, n: int = 5):
    """The n eligible slot open-times, newest first: forming slot, then n-1 closed slots."""
    f = floor_slot(now_ts)
    return [f - i * CANDLE_SECONDS for i in range(n)]


def validate_candles(candles, now_ts=None, instrument=None):
    """Return (clean_sorted_candles, warnings). Raises DataQualityError on unusable data.
    Checks: finite/positive OHLC, OHLC consistency, 15-min alignment, duplicates, future-dated candles
    (catches wrong timezone offsets), timeframe identity, and a price-band sanity check per instrument."""
    if not candles:
        raise DataQualityError("no candles returned")
    warnings, by_ts = [], {}
    dup = 0
    for c in candles:
        vals = (c.o, c.h, c.l, c.c)
        if any((v is None) or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))) for v in vals):
            raise DataQualityError(f"non-finite OHLC at {c.ts}")
        if min(vals) <= 0:
            raise DataQualityError(f"non-positive price at {utc_dt(c.ts)}")
        if c.h < max(c.o, c.c, c.l) - 1e-12 or c.l > min(c.o, c.c, c.h) + 1e-12:
            raise DataQualityError(f"OHLC inconsistency at {utc_dt(c.ts)}")
        if c.ts % CANDLE_SECONDS != 0:
            raise DataQualityError(f"candle not aligned to 15-minute grid: {utc_dt(c.ts)}")
        if c.ts in by_ts:
            dup += 1
        by_ts[c.ts] = c  # later duplicate wins (provider revision)
    if dup:
        warnings.append(f"{dup} duplicate timestamps collapsed")
    out = [by_ts[k] for k in sorted(by_ts)]
    if now_ts is not None and out[-1].ts > floor_slot(now_ts):
        raise DataQualityError(f"candle dated in the future ({utc_dt(out[-1].ts)} > now {utc_dt(now_ts)}): "
                               "timestamp/timezone problem")
    w = out[-301:]
    deltas = [b.ts - a.ts for a, b in zip(w, w[1:])]
    if len(deltas) >= 20 and sum(1 for d in deltas if d == CANDLE_SECONDS) < 0.5 * len(deltas):
        raise DataQualityError("timeframe mismatch: series is not 15-minute data")
    if instrument in SYMBOL_SANITY:
        lo, hi = SYMBOL_SANITY[instrument]
        bad = [c for c in out[-300:] if not (lo <= c.c <= hi)]
        if bad:
            raise DataQualityError(f"{instrument} price {bad[0].c} outside plausible band {lo}-{hi}: wrong symbol?")
    return out, warnings


def merge_candles(old, new):
    d = {c.ts: c for c in old}
    for c in new:
        d[c.ts] = c
    return [d[k] for k in sorted(d)]


# ---------------- market sessions ----------------
def is_market_open(inst_class: str, now_ts: int) -> bool:
    """Approximate expected-open check. FX/metals: Sun 17:00 -> Fri 17:00 New York time
    (handles US DST), daily break 17:00-18:00 NY for the CFD index class. Holidays are NOT
    modelled; they surface as 'stale data' instead."""
    ny = utc_dt(now_ts).astimezone(NY)
    wd, hm = ny.weekday(), ny.hour * 60 + ny.minute  # Mon=0
    if wd == 5:
        return False
    if wd == 6 and hm < 17 * 60:
        return False
    if wd == 4 and hm >= 17 * 60:
        return False
    if inst_class == "index" and 17 * 60 <= hm < 18 * 60:
        return False
    return True


def missing_expected_slots(candles, inst_class, start_ts, end_ts):
    """Slots in [start_ts, end_ts] (inclusive, open times) where the market was expected open but NO candle exists.
    Missing data is never filled in; it is reported."""
    have = {c.ts for c in candles}
    out, t = [], floor_slot(start_ts)
    while t <= end_ts:
        if t not in have and is_market_open(inst_class, t):
            out.append(t)
        t += CANDLE_SECONDS
    return out
