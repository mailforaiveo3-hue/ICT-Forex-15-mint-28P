"""OANDA v20 candles (optional). XAU_USD and NAS100_USD (a Nasdaq-100 CFD). Needs OANDA_API_TOKEN.
Format per OANDA's public v20 docs; NOT live-tested here. CFD availability depends on the account's region
(e.g. not offered to US residents)."""
from datetime import datetime, timezone
from typing import List, Optional

import requests

from candle_manager import Candle
from config import CANDLE_SECONDS, oanda_token
from data_providers.base import Backend, ProviderError, RateLimitError

OANDA_SYMBOLS = {"XAUUSD": "XAU_USD", "NAS100": "NAS100_USD"}
HOSTS = {"practice": "https://api-fxpractice.oanda.com", "live": "https://api-fxtrade.oanda.com"}


def parse_oanda(payload, expect_instrument: str) -> List[Candle]:
    if not isinstance(payload, dict) or "candles" not in payload:
        raise ProviderError(f"OANDA: unexpected payload ({str(payload)[:100]})")
    if payload.get("instrument") != expect_instrument:
        raise ProviderError(f"OANDA: instrument mismatch (asked {expect_instrument}, got {payload.get('instrument')})")
    if payload.get("granularity") != "M15":
        raise ProviderError(f"OANDA: timeframe mismatch (got {payload.get('granularity')})")
    out = []
    for c in payload["candles"]:
        try:
            t = c["time"][:19]
            ts = int(datetime.strptime(t, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
            m = c["mid"]
            out.append(Candle(ts, float(m["o"]), float(m["h"]), float(m["l"]), float(m["c"])))
        except (KeyError, ValueError, TypeError) as e:
            raise ProviderError(f"OANDA: malformed candle ({e})")
    if not out:
        raise ProviderError("OANDA: no candles")
    return out


class OandaBackend(Backend):
    name = "oanda"

    def __init__(self, settings, session=None, token=None):
        self.s, self.http = settings, session or requests
        self._token = token

    @property
    def token(self):
        return self._token or oanda_token()

    def available(self, instrument):
        if instrument not in OANDA_SYMBOLS:
            return False, "instrument not offered"
        if not self.token:
            return False, "OANDA_API_TOKEN not configured"
        return True, ""

    def fetch(self, instrument, now_ts, since_ts: Optional[int]):
        sym = OANDA_SYMBOLS[instrument]
        count = self.s.history_candles if since_ts is None else \
            max(5, min(5000, (now_ts - since_ts) // CANDLE_SECONDS + 4))
        url = f"{HOSTS.get(self.s.oanda_env, HOSTS['practice'])}/v3/instruments/{sym}/candles"
        try:
            r = self.http.get(url, params={"granularity": "M15", "price": "M", "count": count},
                              headers={"Authorization": f"Bearer {self.token}"}, timeout=30)
        except requests.RequestException as e:
            raise ProviderError(f"OANDA network error: {type(e).__name__}")
        if r.status_code == 429:
            raise RateLimitError("OANDA HTTP 429")
        if r.status_code != 200:
            raise ProviderError(f"OANDA HTTP {r.status_code}")
        try:
            return parse_oanda(r.json(), sym)
        except ValueError:
            raise ProviderError("OANDA: invalid JSON")
