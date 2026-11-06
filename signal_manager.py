"""Signal objects, IDs, PKT formatting and Telegram message text."""
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from config import TIMEFRAME, PKT_OFFSET_HOURS

PKT = timezone(timedelta(hours=PKT_OFFSET_HOURS), "PKT")


def to_pkt(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(PKT)


def fmt_pkt(ts: int) -> str:
    return to_pkt(ts).strftime("%d-%b-%Y %I:%M %p PKT")


def decimals(instrument: str) -> int:
    if instrument.endswith("JPY"):
        return 3
    if instrument in ("XAUUSD", "NAS100"):
        return 2
    return 5


def make_signal_id(instrument, direction, candle_ts, event_id) -> str:
    c = datetime.fromtimestamp(candle_ts, timezone.utc).strftime("%Y%m%dT%H%M")
    return f"{instrument}|{direction}|{TIMEFRAME}|{c}|PO3-{event_id}"


@dataclass
class Signal:
    signal_id: str
    instrument: str
    direction: str       # BUY | SELL
    candle_ts: int
    entry: float
    sl: float
    tp: float
    provider: str
    provider_note: str = ""
    provisional: bool = False
    detection_ts: int = 0
    possible_repeat: bool = False

    @property
    def rr(self) -> float:
        risk = abs(self.entry - self.sl)
        return abs(self.tp - self.entry) / risk if risk else float("nan")

    def to_dict(self):
        return asdict(self)


def signal_from_event(ev, instrument, provider, note, detection_ts) -> Signal:
    direction = "BUY" if ev["type"] == "Long" else "SELL"
    return Signal(make_signal_id(instrument, direction, ev["candle_ts"], ev["event_id"]), instrument, direction,
                  ev["candle_ts"], ev["entry"], ev["sl"], ev["tp"], provider, note,
                  bool(ev.get("provisional")), detection_ts)


PROVIDER_LABEL = {"twelvedata": "Twelve Data", "algospecial": "AlgoSpecial", "oanda": "OANDA"}


def format_message(s: Signal) -> str:
    d = decimals(s.instrument)
    bar = "━" * 20
    prov = PROVIDER_LABEL.get(s.provider, s.provider)
    lines = [bar, "ICT POWER OF THREE SIGNAL", bar, "",
             f"Symbol: {s.instrument}", f"Direction: {s.direction}", "Timeframe: 15 Minutes", "",
             f"Entry: {s.entry:.{d}f}", f"Stop Loss: {s.sl:.{d}f}", f"Take Profit: {s.tp:.{d}f}",
             f"Risk/Reward: 1:{s.rr:.2f}", "",
             f"Signal Candle (open): {fmt_pkt(s.candle_ts)}", f"Detection Time: {fmt_pkt(s.detection_ts)}", "",
             f"Provider: {prov}"]
    if s.provider_note:
        lines.append(f"Instrument: {s.provider_note}")
    lines += ["", f"PO3 Signal ID: {s.signal_id}", "", "PO3 Confirmation:",
              "PROVISIONAL (candle not yet closed) - original strategy conditions met on the forming candle."
              if s.provisional else "Original strategy conditions satisfied on a closed candle.",
              "SL/TP are the original script's TP/SL levels.", bar]
    if s.possible_repeat:
        lines.insert(0, "NOTE: possible repeat - a previous delivery attempt could not be confirmed.")
    return "\n".join(lines)
