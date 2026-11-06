"""Persistent duplicate prevention on top of the signals ledger.

Status machine:  pending --(about to send)--> sending --(Telegram OK)--> delivered
A failed send goes back to 'pending' (definite failure) or stays 'sending' (ambiguous: timeout /
5xx - Telegram may or may not have delivered). A 'sending' record found on a later run means a
previous run may have been killed after sending: the signal is re-sent once, flagged as a possible repeat.
"""


class DuplicateManager:
    def __init__(self, db):
        self.db = db

    def status(self, signal_id):
        return self.db.get_signal_status(signal_id)

    def is_delivered(self, signal_id: str) -> bool:
        return self.status(signal_id) == "delivered"

    def possible_repeat(self, signal_id: str) -> bool:
        return self.status(signal_id) == "sending"

    def register_pending(self, sig):
        self.db.register_pending(sig)      # INSERT OR IGNORE: idempotent

    def mark_sending(self, signal_id):
        self.db.set_status(signal_id, "sending")

    def mark_failed(self, signal_id, ambiguous: bool):
        if not ambiguous:
            self.db.set_status(signal_id, "pending")

    def mark_delivered(self, signal_id: str):
        self.db.mark_delivered(signal_id)  # only after Telegram confirmed delivery
