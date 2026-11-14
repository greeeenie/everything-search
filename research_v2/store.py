import json
from pathlib import Path
import sqlite3
import time


class Store:
    """Short-lived cache plus immutable, bounded evidence history (no full HTML)."""

    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY, expires REAL NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS evidence (
                id INTEGER PRIMARY KEY, url TEXT NOT NULL, source TEXT NOT NULL,
                checked_at TEXT NOT NULL, status TEXT NOT NULL, error TEXT, payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS evidence_url ON evidence(url, checked_at);
        """)
        self.prune()

    def get(self, key):
        row = self.db.execute("SELECT expires, payload FROM cache WHERE key=?", (key,)).fetchone()
        if row and row[0] > time.time():
            return json.loads(row[1])
        return None

    def put(self, key, value, ttl):
        self.db.execute(
            "INSERT OR REPLACE INTO cache VALUES (?, ?, ?)",
            (key, time.time() + ttl, json.dumps(value, ensure_ascii=False)),
        )
        self.db.execute("DELETE FROM cache WHERE expires < ?", (time.time(),))
        self.db.commit()

    def record(self, value):
        item = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
        self.db.execute(
            "INSERT INTO evidence(url,source,checked_at,status,error,payload) VALUES (?,?,?,?,?,?)",
            (
                item["url"],
                item["source"],
                item["checked_at"],
                item["status"],
                item.get("error"),
                json.dumps(item, ensure_ascii=False),
            ),
        )
        self.prune()
        self.db.commit()

    def prune(self):
        from datetime import datetime, timedelta, timezone

        cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        self.db.execute("DELETE FROM evidence WHERE checked_at < ?", (cutoff,))
        self.db.execute("DELETE FROM cache WHERE expires < ?", (time.time(),))
        self.db.commit()

    def clear(self):
        self.db.executescript("DELETE FROM cache; DELETE FROM evidence;")
        self.db.commit()

    def close(self):
        self.db.close()
