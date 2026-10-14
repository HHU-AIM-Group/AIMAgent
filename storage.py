"""Thread-safe SQLite persistence for reports, settings, HTTP cache, and snapshots."""
import json
import sqlite3
import threading
from pathlib import Path

class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.connect() as con:
            con.executescript('''
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reports (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cache (url TEXT PRIMARY KEY, fetched REAL NOT NULL, body BLOB NOT NULL);
            ''')
    def connect(self):
        con = sqlite3.connect(str(self.path), timeout=20)
        con.execute('PRAGMA journal_mode=WAL')
        return con
    def get(self, key, default=None):
        with self.lock, self.connect() as con:
            row = con.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default
    def set(self, key, value):
        with self.lock, self.connect() as con:
            con.execute('INSERT OR REPLACE INTO kv VALUES (?,?)', (key, json.dumps(value, ensure_ascii=False)))
    def save_report(self, report):
        with self.lock, self.connect() as con:
            con.execute('INSERT OR REPLACE INTO reports VALUES (?,?,?)', (report['id'], report['created_at'], json.dumps(report, ensure_ascii=False)))
    def report(self, report_id):
        with self.lock, self.connect() as con:
            row = con.execute('SELECT payload FROM reports WHERE id=?', (report_id,)).fetchone()
        return json.loads(row[0]) if row else None
    def reports(self, limit=100):
        with self.lock, self.connect() as con:
            rows = con.execute('SELECT payload FROM reports ORDER BY created_at DESC LIMIT ?', (limit,)).fetchall()
        return [json.loads(row[0]) for row in rows]
    def cache_get(self, url, max_age=3600):
        import time
        with self.lock, self.connect() as con:
            row = con.execute('SELECT fetched,body FROM cache WHERE url=?', (url,)).fetchone()
        return bytes(row[1]) if row and time.time() - row[0] < max_age else None
    def cache_set(self, url, body):
        import time
        with self.lock, self.connect() as con:
            con.execute('INSERT OR REPLACE INTO cache VALUES (?,?,?)', (url, time.time(), body))

    def cache_delete(self, url):
        with self.lock, self.connect() as con:
            con.execute('DELETE FROM cache WHERE url=?', (url,))
