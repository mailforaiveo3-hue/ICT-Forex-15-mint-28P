"""Provider interfaces, exceptions and the ordered-fallback router."""
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from candle_manager import Candle, split_closed_forming, is_market_open, floor_slot
from config import CANDLE_SECONDS, instrument_class


class ProviderError(Exception):
    """Generic provider failure (network, HTTP, malformed payload)."""

class RateLimitError(ProviderError):
    """Per-minute throttle; retry after backoff."""

class QuotaExceeded(ProviderError):
    """Daily quota exhausted; do not retry in this run."""

class StaleDataError(ProviderError):
    pass


@dataclass
class FetchResult:
    candles: List[Candle]
    provider: str
    note: str = ""
    attempts: List[str] = field(default_factory=list)


class Backend:
    name = "base"

    def available(self, instrument: str):
        """-> (usable: bool, reason). Unavailable backends are skipped and the reason is reported."""
        return True, ""

    def fetch(self, instrument: str, now_ts: int, since_ts: Optional[int]) -> List[Candle]:
        raise NotImplementedError


def freshness_problem(instrument, candles, now_ts, max_stale) -> Optional[str]:
    """Reason string if data is stale while the market is expected to be open."""
    if not candles:
        return "empty response"
    closed, _ = split_closed_forming(candles, now_ts)
    if not closed:
        return "no closed candle in response"
    if not is_market_open(instrument_class(instrument), now_ts):
        return None
    lag = (floor_slot(now_ts) - CANDLE_SECONDS - closed[-1].ts) // CANDLE_SECONDS
    if lag > max_stale:
        return f"newest closed candle is {lag} candles old (limit {max_stale})"
    return None


class FallbackRouter:
    """Try backends in order; first one returning fresh data wins."""
    def __init__(self, backends: List[Backend], max_stale: int = 3, notes: Optional[dict] = None):
        self.backends, self.max_stale, self.notes = backends, max_stale, notes or {}

    def fetch(self, instrument, now_ts, since_lookup: Callable[[str, str], Optional[int]]) -> FetchResult:
        attempts, quota_err = [], None
        for b in self.backends:
            ok, why = b.available(instrument)
            if not ok:
                attempts.append(f"{b.name}: not available ({why})")
                continue
            try:
                candles = b.fetch(instrument, now_ts, since_lookup(instrument, b.name))
                prob = freshness_problem(instrument, candles, now_ts, self.max_stale)
                if prob:
                    attempts.append(f"{b.name}: stale/unusable ({prob})")
                    continue
                attempts.append(f"{b.name}: ok ({len(candles)} candles)")
                return FetchResult(candles, b.name, self.notes.get(b.name, ""), attempts)
            except QuotaExceeded as e:
                attempts.append(f"{b.name}: quota ({e})")
                quota_err = e
            except ProviderError as e:
                attempts.append(f"{b.name}: error ({e})")
        if quota_err is not None and all(("quota" in a or "not available" in a) for a in attempts):
            quota_err.attempts = attempts
            raise quota_err
        err = ProviderError("; ".join(attempts) or "no provider configured")
        err.attempts = attempts
        raise err
