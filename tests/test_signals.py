from signal_manager import Signal, make_signal_id, format_message, fmt_pkt, signal_from_event, decimals
from conftest import LAST_CLOSED, NOW


def ev(t="Long"):
    return {"type": t, "candle_ts": LAST_CLOSED, "event_id": "123", "entry": 1.085, "sl": 1.087, "tp": 1.081}


def test_pkt_conversion_utc_plus_5():
    assert fmt_pkt(LAST_CLOSED) == "04-Nov-2026 04:45 PM PKT"      # 11:45Z + 5h


def test_signal_id_components():
    s = signal_from_event(ev(), "EURUSD", "twelvedata", "", NOW)
    assert s.signal_id == "EURUSD|BUY|M15|20261104T1145|PO3-123"
    assert signal_from_event(ev("Short"), "EURUSD", "twelvedata", "", NOW).direction == "SELL"


def test_message_contents_and_rr():
    m = format_message(signal_from_event(ev("Short"), "EURUSD", "twelvedata", "", NOW))
    for t in ("ICT POWER OF THREE SIGNAL", "Symbol: EURUSD", "Direction: SELL", "Timeframe: 15 Minutes",
              "Entry: 1.08500", "Stop Loss: 1.08700", "Take Profit: 1.08100", "Risk/Reward: 1:2.00",
              "Signal Candle (open): 04-Nov-2026 04:45 PM PKT", "Detection Time: 04-Nov-2026 05:07 PM PKT",
              "Provider: Twelve Data", "PO3 Signal ID: EURUSD|SELL|M15|20261104T1145|PO3-123"):
        assert t in m
    assert "PROVISIONAL" not in m


def test_provisional_and_instrument_note():
    e = ev(); e["provisional"] = True
    m = format_message(signal_from_event(e, "NAS100", "oanda", "NAS100_USD = Nasdaq 100 CFD (OANDA)", NOW))
    assert "PROVISIONAL" in m and "Nasdaq 100 CFD" in m and "Provider: OANDA" in m


def test_decimals():
    assert decimals("USDJPY") == 3 and decimals("XAUUSD") == 2 and decimals("EURUSD") == 5
