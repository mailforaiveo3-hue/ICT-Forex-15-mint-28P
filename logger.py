"""Logging with automatic secret redaction (API keys, bot token)."""
import logging, os, re, sys

_SECRET_ENV = ["TELEGRAM_BOT_TOKEN", "TWELVE_DATA_API_KEY_1", "TWELVE_DATA_API_KEY_2", "TWELVE_DATA_API_KEY_3",
               "TWELVE_DATA_API_KEY_4", "TWELVE_DATA_API_KEY_GOLD", "OANDA_API_TOKEN", "GITHUB_TOKEN"]


def redact(text) -> str:
    s = str(text)
    for name in _SECRET_ENV:
        v = os.environ.get(name)
        if v and len(v) > 6:
            s = s.replace(v, f"<{name}>")
    s = re.sub(r"(apikey=)[^&\s]+", r"\1<redacted>", s, flags=re.I)
    s = re.sub(r"(bot)\d+:[A-Za-z0-9_-]+", r"\1<redacted>", s)
    s = re.sub(r"(x-access-token:)[^@\s]+", r"\1<redacted>", s)
    s = re.sub(r"(Bearer )[A-Za-z0-9._-]+", r"\1<redacted>", s)
    return s


class _Redactor(logging.Filter):
    def filter(self, record):
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


def get_logger(name="po3"):
    lg = logging.getLogger(name)
    if not lg.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter("%(asctime)s UTC | %(levelname)s | %(message)s"))
        h.addFilter(_Redactor())
        lg.addHandler(h)
        lg.setLevel(logging.INFO)
        lg.propagate = False
        logging.Formatter.converter = __import__("time").gmtime
    return lg
