"""The service's database, log, the library folders and what it is doing."""

import hashlib
import os
import threading
import time
import urllib.error

from common.activity import Activity, open_db
from common.jellyfin import jellyfin, library_locations
from config import DATA_DIR, LIBRARIES, SERIES_LIBRARIES, SUBTITLE_CACHE, SYNCED_DIR

lock = threading.RLock()
db = open_db(
    DATA_DIR,
    """
-- The last check of each subtitle; a file whose size or mtime differs is checked again
CREATE TABLE IF NOT EXISTS subtitles (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    video TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    checked_at REAL NOT NULL,
    -- ok, offset (waits on the status page), failed, fixed, dismissed, restored (the original put back)
    state TEXT NOT NULL,
    offset REAL,
    scale REAL,
    detail TEXT NOT NULL
);
-- The last search for Bulgarian subtitles for a video: found, none (nothing listed), nomatch (none lined up),
-- more (tried MAX_TRIES, the rest on the next pass) or failed; tried holds every file tried, so none is tried twice
CREATE TABLE IF NOT EXISTS downloads (
    video TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    subtitle TEXT,
    tried TEXT NOT NULL,
    checked_at REAL NOT NULL,
    detail TEXT NOT NULL
);
-- What the user said about a movie's or series' folder whose sound names no language: bg (dubbed) or subs (wants
-- Bulgarian subtitles); tagged is no longer used: the tracks set to Bulgarian are kept in the folder's .media.json
CREATE TABLE IF NOT EXISTS audio (
    folder TEXT PRIMARY KEY,
    decision TEXT NOT NULL,
    decided_at REAL NOT NULL,
    tagged TEXT NOT NULL DEFAULT '[]'
);
""",
)
# A failed check is made again only once the code that checks has changed
here = os.path.dirname(os.path.abspath(__file__))
checker = hashlib.sha256(b"".join(open(os.path.join(here, n), "rb").read() for n in ("sync.py", "sync_check.py")))
db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
checked_with = db.execute("SELECT value FROM meta WHERE key = 'checker'").fetchone()
if not checked_with or checked_with[0] != checker.hexdigest():
    db.execute("DELETE FROM subtitles WHERE state = 'failed'")
    db.execute("INSERT OR REPLACE INTO meta VALUES ('checker', ?)", (checker.hexdigest(),))
os.makedirs(SYNCED_DIR, exist_ok=True)
os.makedirs(SUBTITLE_CACHE, exist_ok=True)
activity = Activity(db, lock)
log, event = activity.log, activity.event

# Wakes the loop before its rest is over (an answer about a video's sound)
wake = threading.Event()
sync_run = {"current": None, "queued": 0}
# The videos that want Bulgarian subtitles, from Jellyfin every WANTED_REFRESH; the site searches by IMDb id
downloads_run = {"wanted": [], "review": {}, "listed_at": 0.0, "paused_until": 0.0}
# Videos the user asked other subtitles for: searched before the rest, even before their retry time
search_first = []
searches = {}
# Jellyfin's item id of every movie and episode path, for refresh_video
item_ids = {"at": 0.0, "ids": {}}
# Alignments auto_fix couldn't apply; left on the status page until a restart
not_fixed = set()
# The library folders, from Jellyfin on every pass of the loop
locations = []
series_locations = []


def read_locations():
    folders = {f["Name"]: f for f in jellyfin("/Library/VirtualFolders")}
    locations[:] = library_locations(folders, LIBRARIES)
    series_locations[:] = library_locations(folders, SERIES_LIBRARIES)


def subtitle_label(path):
    """The folder of a subtitle within its library: the movie, or the series and season."""
    folder = os.path.dirname(path)
    location = next((loc for loc in locations + series_locations if folder.startswith(loc.rstrip("/") + "/")), None)
    return os.path.relpath(folder, location) if location else os.path.basename(folder)


def known_items(reload=False):
    """Jellyfin's item id of every movie and episode path; listing every item takes seconds, so at most every 10 min
    unless asked for."""
    if reload or time.time() - item_ids["at"] > 600:
        items = jellyfin("/Items", Recursive="true", IncludeItemTypes="Movie,Episode", Fields="Path", Limit=100000)[
            "Items"
        ]
        item_ids.update(at=time.time(), ids={i["Path"]: i["Id"] for i in items if i.get("Path")})
    return item_ids["ids"]


def refresh_video(video, label):
    # Jellyfin keeps an external subtitle's stream from the last scan and doesn't notice a rewrite or a new file
    try:
        ids = known_items(reload=video not in item_ids["ids"])
        if video in ids:
            jellyfin(f"/Items/{ids[video]}/Refresh", "POST", MetadataRefreshMode="Default", ImageRefreshMode="Default")
        else:
            log(f"error {label}: no Jellyfin item for {os.path.basename(video)}, refresh it by hand")
    except (urllib.error.URLError, TimeoutError) as e:
        log(f"error {label}: Jellyfin refresh failed: {e!r}")
