import math
from candle_manager import Candle
from po3_strategy import PO3Engine, Params, compute_indicators
from conftest import make_series, LAST_CLOSED, STEP


def run(candles, **kw):
    e = PO3Engine(Params(**kw))
    ev = e.process(candles)
    return e, ev


def test_indicators_na_warmup_and_atr_seed():
    cs = make_series(LAST_CLOSED, 80)
    atr, hi, lo = compute_indicators(cs, 50, 40)
    assert math.isnan(atr[48]) and not math.isnan(atr[49])
    assert math.isnan(hi[38]) and not math.isnan(hi[39])
    tr = [cs[0].h - cs[0].l] + [max(c.h - c.l, abs(c.h - p.c), abs(c.l - p.c)) for p, c in zip(cs, cs[1:])]
    assert abs(atr[49] - sum(tr[:50]) / 50) < 1e-12            # RMA seeded with SMA
    assert abs(atr[50] - (tr[50] / 50 + atr[49] * 49 / 50)) < 1e-12


def test_buy_signal_on_bearish_manipulation_dynamic_levels():
    cs = make_series(LAST_CLOSED, 120, signal_from_end=0, direction="down")
    e, ev = run(cs)
    assert len(ev) == 1 and ev[0]["type"] == "Long" and ev[0]["candle_ts"] == LAST_CLOSED
    atr = compute_indicators(cs, 50, 40)[0][-1]
    assert ev[0]["entry"] == 95.0
    assert abs(ev[0]["sl"] - (95 - atr * 5)) < 1e-9                  # Normal risk = 5 x ATR
    assert abs(ev[0]["tp"] - (95 + (atr * 5) * 0.86)) < 1e-9         # RR 0.86


def test_sell_signal_on_bullish_manipulation():
    cs = make_series(LAST_CLOSED, 120, signal_from_end=0, direction="up")
    _, ev = run(cs)
    assert len(ev) == 1 and ev[0]["type"] == "Short"
    assert ev[0]["sl"] > ev[0]["entry"] > ev[0]["tp"]


def test_no_signal_in_calm_market_and_before_atr_warmup():
    _, ev = run(make_series(LAST_CLOSED, 200))
    assert ev == []
    # signal bar before ATR exists (na) cannot trigger anything
    cs = make_series(LAST_CLOSED, 40, signal_from_end=0)
    _, ev = run(cs)
    assert ev == []


def test_tp_exit_then_new_po3_created():
    cs = make_series(LAST_CLOSED, 125, signal_from_end=3)
    cs[-2] = Candle(cs[-2].ts, 96, 110.0, 95.5, 100)                 # reaches TP, never SL
    e, ev = run(cs)
    assert len(ev) == 1 and e.events[0]["outcome"] == "Take Profit"
    assert e.po3.start_time > int(e.events[0]["event_id"])           # new PO3 after exit


def test_exit_not_checked_on_entry_bar():
    # entry bar itself spans beyond TP (high 100.5 >= tp ~ 100.x): must not exit on the entry bar
    cs = make_series(LAST_CLOSED, 120, signal_from_end=0)
    e, _ = run(cs)
    assert e.events[0]["outcome"] is None and e.po3.exit_price is None


def test_sl_overrides_tp_when_both_hit_same_bar():
    cs = make_series(LAST_CLOSED, 120, signal_from_end=1)
    nxt = Candle(LAST_CLOSED, 95, 120.0, 50.0, 95)                    # hits TP and SL
    e, _ = run(cs[:-1] + [nxt])
    assert e.events[0]["outcome"] == "Stop Loss"


def test_fixed_tpsl_percent_mode():
    cs = make_series(LAST_CLOSED, 120, signal_from_end=0)
    _, ev = run(cs, tpsl_method="Fixed")
    assert abs(ev[0]["sl"] - 95 * (1 - 0.4 / 100)) < 1e-9 and abs(ev[0]["tp"] - 95 * 1.003) < 1e-9


def test_incremental_equals_batch():
    cs = make_series(LAST_CLOSED, 140, signal_from_end=2)
    whole, ev_whole = run(cs)
    inc = PO3Engine(Params())
    evs = []
    for k in (60, 100, 138, 140):
        evs += inc.process(cs[:k])
        inc = PO3Engine(Params(), inc.to_state())   # persist/restore between runs
    assert [e["candle_ts"] for e in evs] == [e["candle_ts"] for e in ev_whole]
    assert inc.to_state()["po3"] == whole.to_state()["po3"]


def test_algorithm_mode_derived_inputs():
    p = Params(algorithm_mode="Short Accumulation")
    assert (p.accumulation_length, p.accumulation_atr_mult, p.manipulation_atr_mult) == (11, 2.0, 1.0)
    p = Params(algorithm_mode="Big Manipulation")
    assert (p.accumulation_length, p.accumulation_atr_mult, p.manipulation_atr_mult) == (11, 5.0, 1.0)
    p = Params()
    assert (p.accumulation_length, p.accumulation_atr_mult, p.manipulation_atr_mult) == (40, 5.0, 0.6)
    assert Params(risk="Highest").sl_atr_mult == 9.5 and Params(risk="Lowest").sl_atr_mult == 1.5


def test_close_breakout_method_needs_close_beyond_box():
    cs = make_series(LAST_CLOSED, 120, signal_from_end=0)
    cs[-1] = Candle(cs[-1].ts, 100, 100.5, 90.0, 99.9)               # long wick but closes inside
    _, ev_wick = run(cs, breakout_method="Wick")
    _, ev_close = run(cs, breakout_method="Close")
    assert len(ev_wick) == 1 and ev_close == []
