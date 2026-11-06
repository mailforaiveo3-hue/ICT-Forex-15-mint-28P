"""Compare the Python PO3 engine with a TradingView export - the way to DEMONSTRATE parity.

1. On TradingView, load the Flux Charts PO3 script on a 15m chart with default inputs and export chart data
   (Export chart data -> CSV with time,open,high,low,close). Use >= 5000 bars if possible.
2. Write signals.csv with the time (same format as the OHLC time column) of each *Buy*/*Sell* label's CANDLE
   and the side: header `time,side` (side = Buy/Sell or Long/Short).
3. python tools/compare_tradingview.py ohlc.csv signals.csv [--mode "Small Manipulation"] [--breakout Wick]
Exit code 0 = identical set of (candle time, side); 1 = differences (listed). Times may be epoch seconds or ISO-8601.
Note: TradingView's first trades can differ until the state machines synchronise, so use --skip-first N to ignore
signals in the first N bars (default 300).
"""
import argparse
import csv
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from candle_manager import Candle          # noqa: E402
from po3_strategy import PO3Engine, Params  # noqa: E402


def parse_time(v: str) -> int:
    v = v.strip()
    if v.replace(".", "", 1).isdigit():
        x = float(v)
        return int(x / 1000) if x > 1e11 else int(x)
    return int(datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()) \
        if "+" in v[10:] or v.endswith("Z") else int(datetime.fromisoformat(v).replace(tzinfo=timezone.utc).timestamp())


def load_ohlc(path):
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    low = lambda r: {k.strip().lower(): v for k, v in r.items()}
    out = [Candle(parse_time(low(r)["time"]), float(low(r)["open"]), float(low(r)["high"]),
                  float(low(r)["low"]), float(low(r)["close"])) for r in rows]
    return sorted(out, key=lambda c: c.ts)


def load_signals(path):
    with open(path, newline="") as f:
        rows = [{k.strip().lower(): v.strip() for k, v in r.items()} for r in csv.DictReader(f)]
    side = lambda s: "Long" if s.lower() in ("buy", "long") else "Short"
    return {(parse_time(r["time"]), side(r["side"])) for r in rows}


def compare(candles, tv_signals, params: Params, skip_first=300):
    eng = PO3Engine(params)
    eng.process(candles)
    cutoff = candles[min(skip_first, len(candles) - 1)].ts
    mine = {(e["candle_ts"], e["type"]) for e in eng.events if e["candle_ts"] >= cutoff}
    theirs = {s for s in tv_signals if s[0] >= cutoff}
    return {"matched": sorted(mine & theirs), "missing_in_python": sorted(theirs - mine),
            "extra_in_python": sorted(mine - theirs)}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("ohlc"); ap.add_argument("signals")
    ap.add_argument("--mode", default="Small Manipulation"); ap.add_argument("--breakout", default="Wick")
    ap.add_argument("--tpsl", default="Dynamic"); ap.add_argument("--risk", default="Normal")
    ap.add_argument("--skip-first", type=int, default=300)
    a = ap.parse_args(argv)
    res = compare(load_ohlc(a.ohlc), load_signals(a.signals),
                  Params(algorithm_mode=a.mode, breakout_method=a.breakout, tpsl_method=a.tpsl, risk=a.risk), a.skip_first)
    print(f"matched={len(res['matched'])} missing_in_python={len(res['missing_in_python'])} extra_in_python={len(res['extra_in_python'])}")
    for k in ("missing_in_python", "extra_in_python"):
        for ts, side in res[k]:
            print(f"  {k}: {datetime.fromtimestamp(ts, timezone.utc):%Y-%m-%d %H:%M} UTC {side}")
    return 0 if not res["missing_in_python"] and not res["extra_in_python"] else 1


if __name__ == "__main__":
    sys.exit(main())
