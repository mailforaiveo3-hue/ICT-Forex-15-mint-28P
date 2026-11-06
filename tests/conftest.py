import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone
import pytest
from candle_manager import Candle
from config import Settings

# Wednesday 2026-11-04 12:07:00 UTC -> forming slot 12:00, newest closed candle 11:45
NOW = int(datetime(2026, 11, 4, 12, 7, tzinfo=timezone.utc).timestamp())
LAST_CLOSED = int(datetime(2026, 11, 4, 11, 45, tzinfo=timezone.utc).timestamp())
STEP = 900


def calm_bar(ts, i, base=100.0):
    return Candle(ts, base, base + 0.5 + (i % 3) * 0.1, base - 0.5 - (i % 2) * 0.1, base)


def make_series(last_ts, n=120, signal_from_end=None, direction="down", forming=False, scale=1.0):
    """n calm candles ending at last_ts (closed). A big move bar is placed `signal_from_end`
    bars before the end (0 = last closed candle). 'down' -> bearish manipulation -> BUY (Long)."""
    out = []
    start = last_ts - (n - 1) * STEP
    for i in range(n):
        ts = start + i * STEP
        c = calm_bar(ts, i)
        if signal_from_end is not None and i == n - 1 - signal_from_end:
            c = Candle(ts, 100.0, 100.5, 90.0, 95.0) if direction == "down" else Candle(ts, 100.0, 110.0, 99.5, 105.0)
        out.append(c)
    if forming:
        out.append(calm_bar(last_ts + STEP, n))
    if scale != 1.0:
        out = [Candle(c.ts, c.o * scale, c.h * scale, c.l * scale, c.c * scale) for c in out]
    return out


@pytest.fixture
def settings():
    s = Settings()
    s.db_path = ":memory:"
    return s
