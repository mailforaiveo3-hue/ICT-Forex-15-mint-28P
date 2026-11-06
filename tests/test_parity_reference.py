"""Reproducible cross-check of the production engine against an independent transcription of the Pine script
(tests/reference_pine.py) on identical OHLC data. This is NOT TradingView parity (see STRATEGY_PARITY.md)."""
import itertools
import random
import pytest
from candle_manager import Candle
from po3_strategy import PO3Engine, Params
import reference_pine as ref

T0 = 1_760_000_100 - (1_760_000_100 % 900)


def synth(seed, n=1600):
    """Calm ranges punctuated by bursts so every code path (both directions, TP, SL, same-bar TP+SL) occurs."""
    rnd = random.Random(seed)
    price, out, i = 100.0, [], 0
    while i < n:
        calm = rnd.randint(30, 90)
        vol = rnd.uniform(0.0015, 0.004)
        for _ in range(calm):
            o = price
            c = o * (1 + rnd.uniform(-vol, vol))
            h = max(o, c) * (1 + rnd.uniform(0, vol)); l = min(o, c) * (1 - rnd.uniform(0, vol))
            out.append((o, h, l, c)); price = c; i += 1
        for _ in range(rnd.randint(1, 4)):                   # burst
            o = price; d = rnd.choice([-1, 1]) * rnd.uniform(0.01, 0.045)
            c = o * (1 + d)
            h = max(o, c) * (1 + rnd.uniform(0, 0.012)); l = min(o, c) * (1 - rnd.uniform(0, 0.012))
            out.append((o, h, l, c)); price = c; i += 1
    return [(T0 + k * 900, *b) for k, b in enumerate(out[:n])]


def engine_trades(bars, **kw):
    cs = [Candle(t, o, h, l, c) for t, o, h, l, c in bars]
    e = PO3Engine(Params(**kw))
    e.process(cs)
    return [dict(type=x["type"], ts=x["candle_ts"], entry=x["entry"], sl=x["sl"], tp=x["tp"], exit_ts=x["exit_ts"])
            for x in e.events]


def norm(trades):
    return [(t["type"], t["ts"], round(t["entry"], 9), round(t["sl"], 9), round(t["tp"], 9), t["exit_ts"]) for t in trades]


def ref_kwargs(kw):
    m = dict(algorithm_mode="algorithmMode", breakout_method="breakoutMethod", tpsl_method="tpslMethod", risk="riskAmount")
    return {m[k]: v for k, v in kw.items()}


COMBOS = [dict(algorithm_mode=a, breakout_method=b, tpsl_method=t, risk=r)
          for a, b, t, r in itertools.product(["Small Manipulation", "Short Accumulation", "Big Manipulation"],
                                              ["Wick", "Close"], ["Dynamic", "Fixed"], ["Normal", "Lowest"])]


@pytest.mark.parametrize("kw", COMBOS, ids=lambda k: "-".join(v[:5] for v in k.values()))
def test_engine_matches_independent_reference_on_random_regimes(kw):
    total = 0
    for seed in range(6):
        bars = synth(seed)
        a, b = norm(engine_trades(bars, **kw)), norm(ref.run(bars, **ref_kwargs(kw)))
        assert a == b, f"seed {seed}: engine/reference disagree"
        total += len(a)
    assert total > 0, "fuzz data produced no trades - test would be vacuous"


def test_fuzz_exercises_both_directions_and_exits():
    longs = shorts = 0
    for seed in range(6):
        for t in engine_trades(synth(seed)):
            longs += t["type"] == "Long"; shorts += t["type"] == "Short"
    assert longs > 3 and shorts > 3


def test_persisted_incremental_runs_equal_one_shot_over_regimes():
    bars = synth(3, 1500)
    cs = [Candle(*[bars[i][0], *bars[i][1:]]) for i in range(len(bars))]
    whole = PO3Engine(Params()); whole.process(cs)
    st, got = None, []
    for end in (700, 701, 900, 1100, 1101, 1300, 1500):        # new 'runner' each time, state restored from JSON
        e = PO3Engine(Params(), st); e.process(cs[:end]); st = e.to_state()
    assert [x["candle_ts"] for x in e.events] == [x["candle_ts"] for x in whole.events]
    assert e.to_state()["po3"] == whole.to_state()["po3"]


def test_max_distance_window_limits_processing_like_pine():
    bars = synth(1, 1000)
    a = norm(engine_trades(bars, ))
    cs = [Candle(*b) for b in bars]
    e = PO3Engine(Params(max_distance_to_last_bar=300)); e.process(cs)
    r = ref.run(bars, maxDistanceToLastBar=300)
    assert [(x["type"], x["candle_ts"]) for x in e.events] == [(t["type"], t["ts"]) for t in r]
