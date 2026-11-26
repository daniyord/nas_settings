"""The service's database, log and what it is doing."""

import threading
from typing import Any

from common.activity import Activity, open_db
from config import DATA_DIR

lock = threading.RLock()
db = open_db(
    DATA_DIR,
    """
-- What came into the download folders (a torrent's file or folder, or one copied in by hand) and what became of it:
-- new (waits for the matcher), doubt (waits on the status page), duplicate (that movie is in the library already),
-- failed, or done (placed: its folder in the library)
CREATE TABLE IF NOT EXISTS incoming (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    -- movie, or scene: a scene isn't looked up, it waits on the status page for its studio
    kind TEXT NOT NULL DEFAULT 'movie',
    -- The studio folder of the download folder it came in (Downloads/<Studio>/...): searched there, placed under it
    studio TEXT,
    video TEXT,
    state TEXT NOT NULL,
    title TEXT,
    year INTEGER,
    minutes INTEGER,
    -- Typed on the status page: other titles to search, or a ThePornDB / TMDB link
    keywords TEXT NOT NULL DEFAULT '[]',
    candidates TEXT NOT NULL DEFAULT '[]',
    detail TEXT NOT NULL DEFAULT '',
    placed TEXT,
    added_at REAL NOT NULL,
    checked_at REAL
);
""",
)
# Columns added since the table was made
columns = {r["name"] for r in db.execute("PRAGMA table_info(incoming)")}
if "kind" not in columns:
    db.execute("ALTER TABLE incoming ADD COLUMN kind TEXT NOT NULL DEFAULT 'movie'")
if "studio" not in columns:
    db.execute("ALTER TABLE incoming ADD COLUMN studio TEXT")
activity = Activity(db, lock)
log, event = activity.log, activity.event

# Wakes the loop before its rest is over
wake = threading.Event()
run: dict[str, Any] = {"current": None, "started": None, "finished": None}
# Where the movies come in: the download folders as seen here, from the last pass
downloads: list[str] = []
# Per library, from the last pass: its folders in Jellyfin, studios and videos
libraries = []
