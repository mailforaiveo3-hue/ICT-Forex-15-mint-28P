import pytest
from datetime import datetime, timezone
from candle_manager import (Candle, DataQualityError, validate_candles, split_closed_forming, window_slots,
                            floor_slot, is_market_open, merge_candles)
from conftest import NOW, LAST_CLOSED, STEP, make_series


def ts(*a):
    return int(datetime(*a, tzinfo=timezone.utc).timestamp())


def test_five_slots_forming_plus_four_closed():
    s = window_slots(NOW, 5)
    assert s[0] == floor_slot(NOW) == LAST_CLOSED + STEP
    assert s[1] == LAST_CLOSED and len(s) == 5 and s[-1] == LAST_CLOSED - 3 * STEP


def test_closed_vs_forming_split():
    cs = make_series(LAST_CLOSED, 10, forming=True)
    closed, forming = split_closed_forming(cs, NOW)
    assert closed[-1].ts == LAST_CLOSED and forming.ts == LAST_CLOSED + STEP


def test_candle_closes_exactly_at_boundary():
    c = [Candle(LAST_CLOSED, 1, 1, 1, 1)]
    assert split_closed_forming(c, LAST_CLOSED + STEP)[0] == c
    assert split_closed_forming(c, LAST_CLOSED + STEP - 1)[0] == []


@pytest.mark.parametrize("bad", [
    Candle(900, 1, 0.5, 0.4, 1),           # high below open/close
    Candle(900, -1, 1, -2, 0.5),           # non-positive
    Candle(901, 1, 2, 0.5, 1),             # off-grid
    Candle(900, float("nan"), 2, 1, 1)])
def test_invalid_candles_rejected(bad):
    with pytest.raises(DataQualityError):
        validate_candles([bad])


def test_duplicates_collapsed_and_sorted():
    a, b = Candle(1800, 1, 2, 1, 1.5), Candle(900, 1, 2, 1, 1.5)
    out, w = validate_candles([a, b, a])
    assert [c.ts for c in out] == [900, 1800] and any("duplicate" in x for x in w)


def test_empty_rejected():
    with pytest.raises(DataQualityError):
        validate_candles([])


def test_merge_prefers_new():
    old = [Candle(900, 1, 2, 1, 1)]
    new = [Candle(900, 1, 3, 1, 1), Candle(1800, 1, 2, 1, 1)]
    assert merge_candles(old, new)[0].h == 3


def test_market_sessions_fx_and_index():
    assert is_market_open("fx", NOW)                              # Wed noon
    assert not is_market_open("fx", ts(2026, 11, 7, 12))          # Saturday
    assert not is_market_open("fx", ts(2026, 11, 6, 22, 30))      # Fri after 17:00 NY (EST, UTC-5 => 22:00Z)
    assert is_market_open("fx", ts(2026, 11, 6, 21, 30))
    assert not is_market_open("fx", ts(2026, 11, 8, 21, 0))       # Sunday before 17:00 NY
    assert is_market_open("fx", ts(2026, 11, 8, 22, 30))
    assert not is_market_open("index", ts(2026, 11, 4, 22, 30))   # daily break 17-18 NY
    assert is_market_open("index", ts(2026, 11, 4, 23, 30))
    # DST: 2026-07-08 summer -> Fri close at 21:00Z
    assert not is_market_open("fx", ts(2026, 7, 10, 21, 30)) and is_market_open("fx", ts(2026, 7, 10, 20, 30))
