"""A service's database and log: every line is printed and kept for the status page's live panel, and events
(what changed on disk) are also stored, so the panel starts from them after a restart."""

import collections
import os
import sqlite3
import time

EVENTS = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    at REAL NOT NULL,
    path TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_at ON events(at);
"""


def open_db(data_dir, schema):
    """DATA_DIR/state.db with the events table and the service's own tables."""
    os.makedirs(data_dir, exist_ok=True)
    db = sqlite3.connect(os.path.join(data_dir, "state.db"), check_same_thread=False, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.executescript("PRAGMA journal_mode = WAL;" + EVENTS + schema)
    return db


class Activity:
    def __init__(self, db, lock):
        self.db, self.lock = db, lock
        self.lines = collections.deque(maxlen=500)
        self.seq = 0
        # The numbering starts over with every start; the page compares this to know its last line is stale
        self.boot = str(time.time())
        for row in reversed(
            db.execute("SELECT at, path, kind, detail FROM events ORDER BY at DESC, id DESC LIMIT 200").fetchall()
        ):
            self._add(row["at"], f"{row['kind']} {os.path.basename(os.path.dirname(row['path']))}: {row['detail']}")

    def _add(self, at, text):
        self.seq += 1
        self.lines.append({"seq": self.seq, "at": at, "text": text})

    def log(self, text):
        print(text, flush=True)
        with self.lock:
            self._add(time.time(), text)

    def event(self, path, kind, detail, label):
        self.log(f"{kind} {label}: {detail}")
        with self.lock:
            self.db.execute(
                "INSERT INTO events (at, path, kind, detail) VALUES (?, ?, ?, ?)", (time.time(), path, kind, detail)
            )

    def since(self, after):
        with self.lock:
            return [line for line in self.lines if line["seq"] > after]
