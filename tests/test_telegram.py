import requests
from telegram_bot import TelegramNotifier


class R:
    def __init__(self, code, js): self.status_code, self._j = code, js
    def json(self): return self._j


class Http:
    def __init__(self, seq): self.seq, self.calls = list(seq), []
    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        x = self.seq.pop(0)
        if isinstance(x, Exception): raise x
        return x


def n(seq, **k):
    h = Http(seq); sl = []
    return TelegramNotifier("123:TOKEN", "42", h, sl.append, **k), h, sl


def test_success():
    t, h, _ = n([R(200, {"ok": True})])
    assert t.send("hi") == (True, None) and h.calls[0][1]["chat_id"] == "42"


def test_retry_on_server_error_then_ok():
    t, h, sl = n([R(500, {}), R(200, {"ok": True})])
    assert t.send("x")[0] and len(h.calls) == 2 and sl


def test_429_honours_retry_after():
    t, h, sl = n([R(429, {"parameters": {"retry_after": 3}}), R(200, {"ok": True})])
    assert t.send("x")[0] and sl[0] == 3


def test_client_error_not_retried_and_token_not_leaked(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:TOKEN")
    t, h, _ = n([R(401, {"description": "Unauthorized"})])
    ok, err = t.send("x")
    assert not ok and len(h.calls) == 1 and "TOKEN" not in err


def test_network_failure_exhausts_attempts():
    t, h, _ = n([requests.ConnectionError()] * 3)
    assert t.send("x")[0] is False and len(h.calls) == 3


def test_not_configured():
    assert TelegramNotifier(None, None).send("x")[0] is False
