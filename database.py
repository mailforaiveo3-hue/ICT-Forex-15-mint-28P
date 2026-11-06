"""SQLite persistence: candle cache, per-instrument engine state, signal ledger, API usage/stats, meta."""
import json
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS series(id INTEGER PRIMARY KEY, key TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS candles(sid INTEGER, ts INTEGER, o REAL, h REAL, l REAL, c REAL,
    PRIMARY KEY(sid, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS engine_state(key TEXT PRIMARY KEY, json TEXT);
CREATE TABLE IF NOT EXISTS signals(signal_id TEXT PRIMARY KEY, instrument TEXT, direction TEXT, candle_ts INTEGER,
    status TEXT, created_at INTEGER, delivered_at INTEGER, payload TEXT, attempts INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS api_stats(day TEXT, key_id INTEGER, metric TEXT, n INTEGER,
    PRIMARY KEY(day, key_id, metric));
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""

REQUEST_METRICS = ("requests_forex", "requests_gold")


class Database:
    def __init__(self, path):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def _x(self, sql, args=(), many=False):
        with self.lock:
            cur = self.conn.executemany(sql, args) if many else self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def _q1(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchone()

    # ---- candles ----
    def _sid(self, key, create=False):
        r = self._q1("SELECT id FROM series WHERE key=?", (key,))
        if r:
            return r[0]
        if not create:
            return None
        self._x("INSERT OR IGNORE INTO series(key) VALUES(?)", (key,))
        return self._q1("SELECT id FROM series WHERE key=?", (key,))[0]

    def latest_ts(self, key):
        sid = self._sid(key)
        return None if sid is None else self._q1("SELECT MAX(ts) FROM candles WHERE sid=?", (sid,))[0]

    def load_candles(self, key):
        from candle_manager import Candle
        sid = self._sid(key)
        if sid is None:
            return []
        with self.lock:
            rows = self.conn.execute("SELECT ts,o,h,l,c FROM candles WHERE sid=? ORDER BY ts", (sid,)).fetchall()
        return [Candle(*r) for r in rows]

    def save_candles(self, key, candles, max_keep):
        sid = self._sid(key, create=True)
        self._x("INSERT OR REPLACE INTO candles VALUES(?,?,?,?,?,?)",
                [(sid, c.ts, c.o, c.h, c.l, c.c) for c in candles], many=True)
        self._x("DELETE FROM candles WHERE sid=? AND ts < "
                "(SELECT MIN(ts) FROM (SELECT ts FROM candles WHERE sid=? ORDER BY ts DESC LIMIT ?))",
                (sid, sid, max_keep))

    # ---- engine state ----
    def load_state(self, key):
        r = self._q1("SELECT json FROM engine_state WHERE key=?", (key,))
        return json.loads(r[0]) if r else None

    def save_state(self, key, state):
        self._x("INSERT OR REPLACE INTO engine_state VALUES(?,?)", (key, json.dumps(state)))

    # ---- meta ----
    def get_meta(self, k, default=None):
        r = self._q1("SELECT v FROM meta WHERE k=?", (k,))
        return r[0] if r else default

    def set_meta(self, k, v):
        self._x("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, str(v)))

    # ---- signal ledger: pending -> sending -> delivered ----
    def get_signal_status(self, signal_id):
        r = self._q1("SELECT status FROM signals WHERE signal_id=?", (signal_id,))
        return r[0] if r else None

    def register_pending(self, sig):
        self._x("INSERT OR IGNORE INTO signals(signal_id,instrument,direction,candle_ts,status,created_at,payload) "
                "VALUES(?,?,?,?,?,?,?)",
                (sig.signal_id, sig.instrument, sig.direction, sig.candle_ts, "pending",
                 int(time.time()), json.dumps(sig.to_dict())))

    def set_status(self, signal_id, status):
        """Never downgrades a delivered signal."""
        self._x("UPDATE signals SET status=?, attempts=attempts+? WHERE signal_id=? AND status!='delivered'",
                (status, 1 if status == "sending" else 0, signal_id))

    def mark_delivered(self, signal_id):
        self._x("UPDATE signals SET status='delivered', delivered_at=? WHERE signal_id=?",
                (int(time.time()), signal_id))

    def count_signals(self, status=None):
        q, a = "SELECT COUNT(*) FROM signals", ()
        if status:
            q, a = q + " WHERE status=?", (status,)
        return self._q1(q, a)[0]

    # ---- API stats (per UTC day, per physical key) ----
    def stat_add(self, day, key_id, metric, n=1):
        self._x("INSERT INTO api_stats VALUES(?,?,?,?) ON CONFLICT(day,key_id,metric) DO UPDATE SET n=n+excluded.n",
                (day, key_id, metric, n))

    def stat_set(self, day, key_id, metric, n):
        self._x("INSERT OR REPLACE INTO api_stats VALUES(?,?,?,?)", (day, key_id, metric, n))

    def stat_get(self, day, key_id, metric):
        r = self._q1("SELECT n FROM api_stats WHERE day=? AND key_id=? AND metric=?", (day, key_id, metric))
        return r[0] if r else 0

    def stat_total_requests(self, day, key_id):
        return sum(self.stat_get(day, key_id, m) for m in REQUEST_METRICS)

    def prune(self, keep_days=14):
        cutoff = int(time.time()) - keep_days * 86400
        self._x("DELETE FROM signals WHERE created_at<? AND status='delivered'", (cutoff,))
        self._x("DELETE FROM api_stats WHERE day < date('now', ?)", (f"-{keep_days} days",))

    def snapshot_to(self, dest_path):
        """Consistent copy using SQLite's online backup API."""
        with self.lock:
            dst = sqlite3.connect(dest_path)
            try:
                self.conn.backup(dst)
            finally:
                dst.close()

    def close(self):
        with self.lock:
            self.conn.close()
