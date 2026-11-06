"""XAUUSD providers.

* AlgoSpecial (preferred per spec). VERIFIED FROM ITS DOCS (2026-11-05): a *historical dataset* API
  ("Updated 2026-11-02, data refreshed from the AlgoSpecial catalog", XAUUSD M15 ends 2026-10-30);
  timestamps are *broker server time, not UTC* with no stated offset. Therefore it is DISABLED unless
  ALGOSPECIAL_UTC_OFFSET_HOURS is set explicitly, and it must still pass the freshness gate (it normally will not).
* Twelve Data XAU/USD via a DEDICATED key (TWELVE_DATA_API_KEY_GOLD) - never silently uses forex credits.
* OANDA XAU_USD (optional, needs OANDA_API_TOKEN).
"""
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import requests

from candle_manager import Candle
from data_providers.base import Backend, ProviderError, RateLimitError, FallbackRouter
from data_providers.twelve_data import TwelveDataBackend

ALGO_URL = "https://www.algospecial.com/api/ohlc.php"
ALGO_SYMBOLS = {"XAUUSD": "XAUUSD", "NAS100": "USTEC"}


def parse_algospecial(payload, utc_offset_hours: float, expect_symbol: Optional[str] = None) -> List[Candle]:
    """Documented shape: {"data":[{"Timestamp":"YYYY.MM.DD HH:MM:SS","Open","High","Low","Close","TickVolume","Spread"}]}.
    Timestamp = BROKER SERVER TIME -> converted with the explicitly configured offset."""
    if not isinstance(payload, dict) or "data" not in payload:
        raise ProviderError(f"AlgoSpecial: unexpected payload ({str(payload)[:100]})")
    sym = payload.get("symbol")
    if expect_symbol and sym and str(sym).upper() != expect_symbol:
        raise ProviderError(f"AlgoSpecial: symbol mismatch (asked {expect_symbol}, got {sym})")
    tf = payload.get("tf") or payload.get("timeframe")
    if tf and str(tf).upper() != "M15":
        raise ProviderError(f"AlgoSpecial: timeframe mismatch (got {tf})")
    rows = payload["data"]
    if not isinstance(rows, list) or not rows:
        raise ProviderError("AlgoSpecial: empty data")
    out = []
    for r in rows:
        try:
            dt = datetime.strptime(r["Timestamp"], "%Y.%m.%d %H:%M:%S").replace(tzinfo=timezone.utc)
            dt -= timedelta(hours=utc_offset_hours)
            out.append(Candle(int(dt.timestamp()), float(r["Open"]), float(r["High"]),
                              float(r["Low"]), float(r["Close"])))
        except (KeyError, ValueError, TypeError) as e:
            raise ProviderError(f"AlgoSpecial: malformed row ({e})")
    return out


class AlgoSpecialBackend(Backend):
    name = "algospecial"

    def __init__(self, settings, session=None):
        self.s, self.http = settings, session or requests

    def available(self, instrument):
        if instrument not in ALGO_SYMBOLS:
            return False, "instrument not offered"
        if self.s.algospecial_utc_offset_hours is None:
            return False, "timestamps are broker server time; set ALGOSPECIAL_UTC_OFFSET_HOURS after verifying the offset"
        return True, ""

    def fetch(self, instrument, now_ts, since_ts: Optional[int]):
        sym = ALGO_SYMBOLS[instrument]
        now = datetime.fromtimestamp(now_ts, timezone.utc)
        start = (datetime.fromtimestamp(since_ts, timezone.utc) - timedelta(days=1)) if since_ts \
            else now - timedelta(days=120)      # 4800 M15 bars ~ 50 trading days; limit is 366 days / 50k rows
        params = {"symbol": sym, "tf": "M15", "from": start.strftime("%Y-%m-%d"),
                  "to": (now + timedelta(days=1)).strftime("%Y-%m-%d"), "format": "json"}
        try:
            r = self.http.get(ALGO_URL, params=params, timeout=30)
        except requests.RequestException as e:
            raise ProviderError(f"AlgoSpecial network error: {type(e).__name__}")
        if r.status_code == 429:
            raise RateLimitError("AlgoSpecial HTTP 429")
        if r.status_code != 200:
            raise ProviderError(f"AlgoSpecial HTTP {r.status_code}")
        try:
            return parse_algospecial(r.json(), float(self.s.algospecial_utc_offset_hours), sym)
        except ValueError:
            raise ProviderError("AlgoSpecial: invalid JSON")


def _oanda():
    from data_providers.oanda_provider import OandaBackend
    return OandaBackend


def build_gold_router(settings, api_manager, session=None) -> FallbackRouter:
    avail = {"algospecial": AlgoSpecialBackend(settings, session),
             "twelvedata": TwelveDataBackend(api_manager, settings, session),
             "oanda": _oanda()(settings, session)}
    backends = [avail[n] for n in settings.gold_provider_order if n in avail]
    return FallbackRouter(backends, settings.max_stale_candles,
                          {"twelvedata": "XAU/USD spot (Twelve Data)",
                           "algospecial": "XAUUSD (AlgoSpecial broker feed)",
                           "oanda": "XAU_USD (OANDA)"})
