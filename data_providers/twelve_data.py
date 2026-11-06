"""Twelve Data /time_series client (15min, UTC). Format per public docs (meta + values[]);
NOT live-tested in this build (no network access to api.twelvedata.com from the build sandbox)."""
from datetime import datetime, timezone
from typing import List, Optional

import requests

from candle_manager import Candle
from config import CANDLE_SECONDS, GOLD_KEY_ID
from data_providers.base import Backend, ProviderError, RateLimitError, QuotaExceeded

URL = "https://api.twelvedata.com/time_series"


def td_symbol(instrument: str) -> str:
    return f"{instrument[:3]}/{instrument[3:]}"


def parse_time_series(payload, expect_symbol: Optional[str] = None) -> List[Candle]:
    if not isinstance(payload, dict):
        raise ProviderError("Twelve Data: unexpected response type")
    if payload.get("status") == "error" or ("code" in payload and "values" not in payload):
        code, msg = payload.get("code"), str(payload.get("message", ""))
        low = msg.lower()
        if code == 429 or "api credits" in low or "rate limit" in low:
            if "day" in low:
                raise QuotaExceeded(f"Twelve Data daily quota: {msg[:120]}")
            raise RateLimitError(f"Twelve Data rate limit: {msg[:120]}")
        raise ProviderError(f"Twelve Data error {code}: {msg[:160]}")
    meta = payload.get("meta") or {}
    if expect_symbol and meta.get("symbol") and meta["symbol"].upper().replace(" ", "") != expect_symbol.upper():
        raise ProviderError(f"Twelve Data: symbol mismatch (asked {expect_symbol}, got {meta['symbol']})")
    if meta.get("interval") and meta["interval"] != "15min":
        raise ProviderError(f"Twelve Data: timeframe mismatch (got {meta['interval']})")
    vals = payload.get("values")
    if not isinstance(vals, list) or not vals:
        raise ProviderError("Twelve Data: 'values' missing or empty")
    out = []
    for v in vals:
        try:
            dt = datetime.strptime(v["datetime"][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            out.append(Candle(int(dt.timestamp()), float(v["open"]), float(v["high"]),
                              float(v["low"]), float(v["close"])))
        except (KeyError, ValueError, TypeError) as e:
            raise ProviderError(f"Twelve Data: malformed row ({e})")
    return out


class TwelveDataBackend(Backend):
    name = "twelvedata"

    def __init__(self, api_manager, settings, session=None):
        self.api, self.s = api_manager, settings
        self.http = session or requests

    def available(self, instrument):
        try:
            kid, _ = self.api.route(instrument)
        except ProviderError as e:
            return False, str(e)
        if not self.api.keys.get(kid):
            return False, "Twelve Data key for this instrument is not configured"
        return True, ""

    def fetch(self, instrument, now_ts, since_ts: Optional[int]):
        kid, bucket, key = self.api.key_for(instrument)
        size = self.s.history_candles if since_ts is None else \
            max(5, min(5000, (now_ts - since_ts) // CANDLE_SECONDS + 4))
        params = {"symbol": td_symbol(instrument), "interval": "15min", "outputsize": size,
                  "timezone": "UTC", "order": "ASC", "apikey": key}

        def do():
            r = self.http.get(URL, params=params, timeout=25)
            left = getattr(r, "headers", {}).get("api-credits-left") if hasattr(r, "headers") else None
            if left is not None:
                self.api.note_credits_left(kid, left)
            if r.status_code == 429:
                raise RateLimitError("HTTP 429")
            if r.status_code >= 500:
                raise ProviderError(f"HTTP {r.status_code}")
            try:
                data = r.json()
            except ValueError:
                raise ProviderError("invalid JSON")
            return parse_time_series(data, td_symbol(instrument))
        return self.api.call(kid, do, bucket=bucket)
