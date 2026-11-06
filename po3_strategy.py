"""Python port of the fluxchart 'ICT Power Of Three' Pine Script (v5).

The bar-by-bar state machine below mirrors the Pine source line by line, including the
cascading (non-else) `if` blocks, so that several state transitions can occur on one bar
exactly as they do in Pine. Pine-only code (boxes, labels, tables, alerts) is not ported.
"""
import copy
import math
from dataclasses import dataclass, asdict, field
from typing import List, Optional

from candle_manager import Candle

NAN = float("nan")

S_WAIT_ACC = "Waiting For Accumulation"
S_WAIT_ACC_END = "Waiting For Accumulation End"
S_WAIT_MANIP = "Waiting For Manipulation"
S_WAIT_DIST = "Waiting For Distribution"
S_ENTRY = "Entry Taken"
S_TP = "Take Profit"
S_SL = "Stop Loss"
S_DONE = "Done"

SL_ATR_MULT = {"Highest": 9.5, "High": 6.0, "Normal": 5.0, "Low": 4.0, "Lowest": 1.5}


@dataclass
class Params:
    algorithm_mode: str = "Small Manipulation"
    breakout_method: str = "Wick"
    tpsl_method: str = "Dynamic"
    risk: str = "Normal"
    tp_percent: float = 0.3
    sl_percent: float = 0.4
    rr: float = 0.86
    atr_len: int = 50
    accumulation_expand_mult: float = 0.5
    max_distance_to_last_bar: int = 4800

    # Derived exactly as in the Pine script
    @property
    def accumulation_length(self) -> int:
        return 40 if self.algorithm_mode == "Small Manipulation" else 11

    @property
    def accumulation_atr_mult(self) -> float:
        return 2.0 if self.algorithm_mode == "Short Accumulation" else 5.0

    @property
    def manipulation_atr_mult(self) -> float:
        return 0.6 if self.algorithm_mode == "Small Manipulation" else 1.0

    @property
    def sl_atr_mult(self) -> float:
        return SL_ATR_MULT.get(self.risk, 6.5)

    @staticmethod
    def from_settings(s) -> "Params":
        return Params(s.algorithm_mode, s.breakout_method, s.tpsl_method, s.risk,
                      s.tp_percent, s.sl_percent, s.rr, s.atr_len,
                      s.accumulation_expand_mult, s.max_distance_to_last_bar)


@dataclass
class PO3:
    state: str
    start_time: int
    acc_top: Optional[float] = None
    acc_bottom: Optional[float] = None
    acc_end_top: Optional[float] = None
    acc_end_bottom: Optional[float] = None
    acc_start_index: Optional[int] = None
    acc_end_index: Optional[int] = None
    manip_top: Optional[float] = None
    manip_bottom: Optional[float] = None
    manip_start_index: Optional[int] = None
    manip_end_index: Optional[int] = None
    manip_direction: Optional[str] = None
    sl_target: Optional[float] = None
    tp_target: Optional[float] = None
    entry_type: Optional[str] = None
    entry_time: Optional[int] = None
    exit_time: Optional[int] = None
    entry_price: Optional[float] = None
    exit_price: Optional[float] = None


# ---------------- indicators (Pine ta.* equivalents) ----------------
def compute_indicators(candles: List[Candle], atr_len: int, acc_len: int):
    """ta.atr(len) = RMA(ta.tr(true)); ta.highest/lowest(acc_len) over high/low incl. current bar.
    Values are NaN while Pine would return na (insufficient bars)."""
    n = len(candles)
    tr = [NAN] * n
    for i, c in enumerate(candles):
        if i == 0:
            tr[i] = c.h - c.l  # ta.tr(true): na previous close -> high-low
        else:
            pc = candles[i - 1].c
            tr[i] = max(c.h - c.l, abs(c.h - pc), abs(c.l - pc))
    atr = [NAN] * n
    alpha = 1.0 / atr_len
    for i in range(n):
        if i == atr_len - 1:
            atr[i] = sum(tr[:atr_len]) / atr_len  # RMA seed = SMA
        elif i >= atr_len:
            atr[i] = alpha * tr[i] + (1 - alpha) * atr[i - 1]
    hi = [NAN] * n
    lo = [NAN] * n
    for i in range(acc_len - 1, n):
        w = candles[i - acc_len + 1:i + 1]
        hi[i] = max(x.h for x in w)
        lo[i] = min(x.l for x in w)
    return atr, hi, lo


class PO3Engine:
    """Holds the persistent state that Pine keeps in `lastPO3` between bars."""

    def __init__(self, params: Params, state: Optional[dict] = None):
        self.p = params
        st = state or {}
        self.last_ts: Optional[int] = st.get("last_ts")
        self.bar_index: int = st.get("bar_index", -1)
        self.po3: Optional[PO3] = PO3(**st["po3"]) if st.get("po3") else None
        self.events: List[dict] = st.get("events", [])

    # ---- persistence ----
    def to_state(self) -> dict:
        return {"last_ts": self.last_ts, "bar_index": self.bar_index,
                "po3": asdict(self.po3) if self.po3 else None,
                "events": self.events[-200:]}

    def clone(self) -> "PO3Engine":
        return PO3Engine(self.p, copy.deepcopy(self.to_state()))

    # ---- processing ----
    def process(self, closed: List[Candle]) -> List[dict]:
        """Feed all not-yet-processed CLOSED candles (Pine: barstate.isconfirmed).
        Returns newly created entry events."""
        if not closed:
            return []
        atr, hi, lo = compute_indicators(closed, self.p.atr_len, self.p.accumulation_length)
        if self.last_ts is None:
            start = max(0, len(closed) - self.p.max_distance_to_last_bar)
        else:
            start = next((i for i, c in enumerate(closed) if c.ts > self.last_ts), len(closed))
        new_events = []
        for i in range(start, len(closed)):
            ev = self._step(closed[i], atr[i], hi[i], lo[i])
            if ev:
                new_events.append(ev)
            self.last_ts = closed[i].ts
        return new_events

    def _step(self, c: Candle, atr: float, highest: float, lowest: float) -> Optional[dict]:
        p = self.p
        self.bar_index += 1
        bi = self.bar_index
        hb = c.h if p.breakout_method == "Wick" else c.c   # highBreakout
        lb = c.l if p.breakout_method == "Wick" else c.c   # lowBreakout
        entry_event = None

        # ---- create new PO3 (Pine: createNewPO3) ----
        create = True
        if self.po3 is not None and self.po3.exit_price is None:
            create = False
        if create:
            self.po3 = PO3(state=S_WAIT_ACC, start_time=c.ts)
        po3 = self.po3

        # ---- Find Accumulation ----
        if po3.state == S_WAIT_ACC:
            if (highest - lowest) <= atr * p.accumulation_atr_mult:   # NaN -> False, like Pine na
                po3.state = S_WAIT_ACC_END
                po3.acc_start_index = bi - p.accumulation_length
                po3.acc_end_index = bi
                po3.acc_top = highest + atr * p.accumulation_expand_mult
                po3.acc_bottom = lowest - atr * p.accumulation_expand_mult
        if po3.state == S_WAIT_ACC_END:
            if hb > po3.acc_top or lb < po3.acc_bottom:
                po3.state = S_WAIT_MANIP
                po3.acc_end_top = hb
                po3.acc_end_bottom = lb
            else:
                po3.acc_end_index = bi

        # ---- Find Manipulation ----
        if po3.state == S_WAIT_MANIP:
            if bi > po3.acc_end_index:
                if hb > po3.acc_top + atr * p.manipulation_atr_mult:
                    po3.state = S_WAIT_DIST
                    po3.manip_direction = "Bullish"
                    po3.manip_start_index = po3.acc_end_index
                    po3.manip_end_index = bi
                    po3.manip_top = hb
                    po3.manip_bottom = po3.acc_end_bottom
                elif lb < po3.acc_bottom - atr * p.manipulation_atr_mult:
                    po3.state = S_WAIT_DIST
                    po3.manip_direction = "Bearish"
                    po3.manip_start_index = po3.acc_end_index
                    po3.manip_end_index = bi
                    po3.manip_top = po3.acc_end_top
                    po3.manip_bottom = lb

        # ---- Find Distribution -> entry at this bar's close ----
        if po3.state == S_WAIT_DIST:
            po3.state = S_ENTRY
            po3.entry_time = c.ts
            po3.entry_price = c.c
            if po3.manip_direction == "Bearish":
                po3.entry_type = "Long"
                if p.tpsl_method == "Fixed":
                    po3.sl_target = po3.entry_price * (1 - p.sl_percent / 100.0)
                    po3.tp_target = po3.entry_price * (1 + p.tp_percent / 100.0)
                else:
                    po3.sl_target = po3.entry_price - atr * p.sl_atr_mult
                    po3.tp_target = po3.entry_price + abs(po3.entry_price - po3.sl_target) * p.rr
            else:
                po3.entry_type = "Short"
                if p.tpsl_method == "Fixed":
                    po3.sl_target = po3.entry_price * (1 + p.sl_percent / 100.0)
                    po3.tp_target = po3.entry_price * (1 - p.tp_percent / 100.0)
                else:
                    po3.sl_target = po3.entry_price + atr * p.sl_atr_mult
                    po3.tp_target = po3.entry_price - abs(po3.entry_price - po3.sl_target) * p.rr
            entry_event = {
                "event_id": str(po3.start_time), "type": po3.entry_type, "candle_ts": c.ts,
                "entry": po3.entry_price, "sl": po3.sl_target, "tp": po3.tp_target,
                "atr": atr, "manip_direction": po3.manip_direction,
                "acc_top": po3.acc_top, "acc_bottom": po3.acc_bottom,
                "outcome": None, "exit_ts": None, "provisional": False}
            self.events.append(entry_event)

        # ---- Entry Taken: check exits (only from the bar after entry; Pine: time > entryTime) ----
        if po3.state == S_ENTRY and c.ts > po3.entry_time:
            def tp_hit(price):
                po3.exit_price, po3.exit_time, po3.state = price, c.ts, S_TP
            def sl_hit(price):
                po3.exit_price, po3.exit_time, po3.state = price, c.ts, S_SL
            et = po3.entry_type
            if p.tpsl_method == "Fixed":
                if et == "Long" and ((c.h / po3.entry_price) - 1) * 100 >= p.tp_percent:
                    tp_hit(po3.entry_price * (1 + p.tp_percent / 100.0))
                if et == "Short" and ((c.l / po3.entry_price) - 1) * 100 <= -p.tp_percent:
                    tp_hit(po3.entry_price * (1 - p.tp_percent / 100.0))
                if et == "Long" and ((c.l / po3.entry_price) - 1) * 100 <= -p.sl_percent:
                    sl_hit(po3.entry_price * (1 - p.sl_percent / 100.0))
                if et == "Short" and ((c.h / po3.entry_price) - 1) * 100 >= p.sl_percent:
                    sl_hit(po3.entry_price * (1 + p.sl_percent / 100.0))
            else:
                if et == "Long" and c.h >= po3.tp_target:
                    tp_hit(po3.tp_target)
                if et == "Short" and c.l <= po3.tp_target:
                    tp_hit(po3.tp_target)
                if et == "Long" and c.l <= po3.sl_target:   # SL evaluated after TP, overrides it (as Pine)
                    sl_hit(po3.sl_target)
                if et == "Short" and c.h >= po3.sl_target:
                    sl_hit(po3.sl_target)
            if po3.exit_price is not None:
                for ev in reversed(self.events):
                    if ev["event_id"] == str(po3.start_time):
                        ev["outcome"], ev["exit_ts"] = po3.state, c.ts
                        break

        # Pine render region flips Take Profit/Stop Loss -> Done (no effect on trading logic)
        if po3.state in (S_TP, S_SL):
            po3.state = S_DONE
        return entry_event
