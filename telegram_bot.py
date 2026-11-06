"""Telegram delivery with retry. The bot token is never logged."""
import time

import requests

from logger import get_logger, redact

log = get_logger()


class TelegramNotifier:
    def __init__(self, token, chat_id, session=None, sleep=time.sleep, attempts=3):
        self.token, self.chat_id = token, chat_id
        self.http, self.sleep, self.attempts = session or requests, sleep, attempts
        self.last_ambiguous = False   # True when the last failure may still have delivered (timeout / 5xx)

    @property
    def configured(self):
        return bool(self.token and self.chat_id)

    def send(self, text: str):
        """Return (ok: bool, error: str|None)."""
        if not self.configured:
            return False, "Telegram credentials not configured"
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        err = None
        self.last_ambiguous = False
        for n in range(1, self.attempts + 1):
            try:
                r = self.http.post(url, json={"chat_id": self.chat_id, "text": text,
                                              "disable_web_page_preview": True}, timeout=20)
                if r.status_code == 200 and r.json().get("ok"):
                    return True, None
                body = {}
                try:
                    body = r.json()
                except ValueError:
                    pass
                err = f"HTTP {r.status_code}: {str(body.get('description', ''))[:100]}"
                if r.status_code == 429:
                    self.last_ambiguous = False
                    wait = float(body.get("parameters", {}).get("retry_after", 5))
                elif 400 <= r.status_code < 500:
                    self.last_ambiguous = False
                    break  # bad token/chat id: retrying will not help
                else:
                    self.last_ambiguous = True
                    wait = 2 ** n
            except requests.RequestException as e:
                err, wait = f"{type(e).__name__}", 2 ** n
                self.last_ambiguous = True
            if n < self.attempts:
                self.sleep(min(wait, 30))
        log.error(f"Telegram delivery failed: {redact(err)}")
        return False, redact(err)
