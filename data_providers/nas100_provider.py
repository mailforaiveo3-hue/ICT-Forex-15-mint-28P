"""NAS100 = Nasdaq-100 CFD ONLY.

Accepted sources: AlgoSpecial `USTEC` (documented as a Nasdaq 100 CFD broker feed, but a historical
dataset - normally fails the freshness gate) and OANDA `NAS100_USD` (CFD, optional token).
Yahoo ^NDX (cash index), QQQ, NQ futures etc. are NOT accepted, automatically or via config
(unknown provider names are dropped in config.Settings.from_env). If neither source returns valid fresh
data the instrument is reported as UNAVAILABLE and no signal is produced.
"""
from data_providers.base import FallbackRouter
from data_providers.gold_provider import AlgoSpecialBackend
from data_providers.oanda_provider import OandaBackend


def build_nas100_router(settings, session=None) -> FallbackRouter:
    avail = {"algospecial": AlgoSpecialBackend(settings, session), "oanda": OandaBackend(settings, session)}
    backends = [avail[n] for n in settings.nas100_provider_order if n in avail]
    return FallbackRouter(backends, settings.max_stale_candles,
                          {"algospecial": "USTEC = Nasdaq 100 CFD (AlgoSpecial broker feed)",
                           "oanda": "NAS100_USD = Nasdaq 100 CFD (OANDA)"})
