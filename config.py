"""Central configuration. All secrets come from environment variables (GitHub Secrets)."""
import os
from dataclasses import dataclass

TIMEFRAME = "M15"
CANDLE_SECONDS = 900
PKT_OFFSET_HOURS = 5  # Pakistan Standard Time, UTC+5, no DST

FOREX_PAIRS_BY_KEY = {
    1: ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD"],
    2: ["EURGBP", "EURJPY", "EURCHF", "EURCAD", "EURAUD", "EURNZD", "GBPJPY"],
    3: ["GBPCHF", "GBPCAD", "GBPAUD", "GBPNZD", "AUDJPY", "AUDCHF", "AUDCAD"],
    4: ["AUDNZD", "NZDJPY", "NZDCHF", "NZDCAD", "CADJPY", "CADCHF", "CHFJPY"],
}
FOREX_KEY_IDS = (1, 2, 3, 4)
GOLD_KEY_ID = 5            # optional DEDICATED Twelve Data key for XAU/USD (secret TWELVE_DATA_API_KEY_GOLD)
FOREX_PAIRS = [p for k in FOREX_KEY_IDS for p in FOREX_PAIRS_BY_KEY[k]]
PAIR_TO_KEY = {p: k for k, pairs in FOREX_PAIRS_BY_KEY.items() for p in pairs}
OTHER_INSTRUMENTS = ["XAUUSD", "NAS100"]
ALL_INSTRUMENTS = FOREX_PAIRS + OTHER_INSTRUMENTS  # exactly 30

assert len(FOREX_PAIRS) == 28 and len(set(FOREX_PAIRS)) == 28, "need exactly 28 unique forex pairs"
assert all(len(v) == 7 for v in FOREX_PAIRS_BY_KEY.values()), "each key must own exactly 7 pairs"
assert len(ALL_INSTRUMENTS) == 30 and len(set(ALL_INSTRUMENTS)) == 30

# Plausible price bands used as a symbol-identity sanity check (not a substitute for provider verification)
SYMBOL_SANITY = {"XAUUSD": (300.0, 20000.0), "NAS100": (2000.0, 100000.0)}
NAS100_ALLOWED_PROVIDERS = ("algospecial", "oanda")   # CFD sources only; no index/ETF/futures substitutes
GOLD_ALLOWED_PROVIDERS = ("algospecial", "twelvedata", "oanda")

# Instrument class drives market-session rules
def instrument_class(symbol: str) -> str:
    if symbol == "NAS100":
        return "index"
    return "fx"  # forex pairs and gold share the weekly FX/metal session


def _env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _env_bool(name, default):
    v = _env(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # --- Pine Script inputs (defaults copied from the original script) ---
    algorithm_mode: str = "Small Manipulation"   # Small Manipulation | Short Accumulation | Big Manipulation
    breakout_method: str = "Wick"                # Close | Wick
    tpsl_method: str = "Dynamic"                 # Dynamic | Fixed
    risk: str = "Normal"                         # Highest|High|Normal|Low|Lowest
    tp_percent: float = 0.3
    sl_percent: float = 0.4
    rr: float = 0.86
    max_distance_to_last_bar: int = 4800
    atr_len: int = 50
    accumulation_expand_mult: float = 0.5

    # --- scanner behaviour (not part of the Pine script) ---
    evaluate_forming: bool = False   # Pine only acts on confirmed bars; see STRATEGY_PARITY.md
    window_candles: int = 5          # forming + 4 closed
    history_candles: int = 4800      # initial download size = max_distance_to_last_bar (Twelve Data max 5000)
    cache_max_candles: int = 5000
    min_history_candles: int = 500   # below this the PO3 ATR/state cannot be trusted -> instrument rejected
    max_window_gaps_reject: bool = True
    max_stale_candles: int = 3       # market open & newest closed candle older than this => stale
    db_path: str = "state/state.db"
    report_dir: str = "reports"
    dry_run: bool = False            # no Telegram send, no marking delivered
    twelve_rpm: int = 8              # free plan = 8 requests/min per key
    twelve_daily_budget: int = 800   # ALWAYS enforced per physical key (Basic plan cap = 800/day)
    twelve_retry_reserve: int = 80   # credits only retries may use (first attempts stop at budget-reserve)
    gold_twelve_share_key: int = 0   # 0 = use dedicated gold key; 1-4 = explicitly share that forex key (separate accounting)
    oanda_env: str = "practice"
    algospecial_utc_offset_hours: object = None  # None = timezone unverified -> AlgoSpecial disabled
    gold_provider_order: tuple = ("algospecial", "twelvedata", "oanda")
    nas100_provider_order: tuple = ("algospecial", "oanda")

    @staticmethod
    def from_env() -> "Settings":
        s = Settings()
        s.algorithm_mode = _env("PO3_ALGORITHM_MODE", s.algorithm_mode)
        s.breakout_method = _env("PO3_BREAKOUT_METHOD", s.breakout_method)
        s.tpsl_method = _env("PO3_TPSL_METHOD", s.tpsl_method)
        s.risk = _env("PO3_RISK", s.risk)
        s.evaluate_forming = _env_bool("EVALUATE_FORMING_CANDLE", s.evaluate_forming)
        s.db_path = _env("STATE_DB_PATH", s.db_path)
        s.dry_run = _env_bool("DRY_RUN", s.dry_run)
        s.twelve_rpm = int(_env("TWELVE_DATA_RPM", s.twelve_rpm))
        s.twelve_daily_budget = max(1, int(_env("TWELVE_DATA_DAILY_BUDGET", s.twelve_daily_budget)))
        s.twelve_retry_reserve = max(0, int(_env("TWELVE_DATA_RETRY_RESERVE", s.twelve_retry_reserve)))
        s.gold_twelve_share_key = int(_env("GOLD_TWELVE_SHARE_KEY", 0))
        s.oanda_env = _env("OANDA_ENV", s.oanda_env)
        off = _env("ALGOSPECIAL_UTC_OFFSET_HOURS")
        s.algospecial_utc_offset_hours = float(off) if off is not None else None
        g = _env("GOLD_PROVIDER_ORDER"); n = _env("NAS100_PROVIDER_ORDER")
        if g: s.gold_provider_order = tuple(x.strip() for x in g.split(",") if x.strip() in GOLD_ALLOWED_PROVIDERS)
        if n: s.nas100_provider_order = tuple(x.strip() for x in n.split(",") if x.strip() in NAS100_ALLOWED_PROVIDERS)
        return s


def twelve_keys() -> dict:
    d = {k: _env(f"TWELVE_DATA_API_KEY_{k}") for k in FOREX_KEY_IDS}
    d[GOLD_KEY_ID] = _env("TWELVE_DATA_API_KEY_GOLD")
    return d


def oanda_token():
    return _env("OANDA_API_TOKEN")

def telegram_creds():
    return _env("TELEGRAM_BOT_TOKEN"), _env("TELEGRAM_CHAT_ID")

