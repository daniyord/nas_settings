"""The service's database, log and what it knows about the libraries between runs."""

import threading
import time

import config
from common.activity import Activity, open_db
from names import id_tag

lock = threading.RLock()
db = open_db(
    config.DATA_DIR,
    """
-- A doubtful match confirmed on the status page, for the file and the id Jellyfin had then
CREATE TABLE IF NOT EXISTS confirmed (
    path TEXT NOT NULL,
    tag TEXT NOT NULL,
    at REAL NOT NULL,
    PRIMARY KEY (path, tag)
);
-- A TMDB title picked on the status page for a release (a movie's video, a series' folder)
CREATE TABLE IF NOT EXISTS chosen (
    path TEXT PRIMARY KEY,
    tmdb_id INTEGER NOT NULL,
    at REAL NOT NULL
);
-- Actors who should be named in Cyrillic and TMDB has only a Latin name for; the status page asks for it
CREATE TABLE IF NOT EXISTS actor_names (
    tmdb_id INTEGER PRIMARY KEY,
    latin TEXT NOT NULL,
    suggestion TEXT NOT NULL,
    title TEXT NOT NULL,
    at REAL NOT NULL
);
-- Titles organized without a Bulgarian title in TMDB; the status page asks for one (kind: movie or tvshow)
CREATE TABLE IF NOT EXISTS untitled (
    path TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    at REAL NOT NULL
);
""",
)
activity = Activity(db, lock)
log, event = activity.log, activity.event

# In memory only: rebuilt from Jellyfin on every run
status = {}
missing_since = {}
last_rescan = [0.0]
wake = threading.Event()
last_run = {"started": None, "finished": None, "error": None, "interval": config.INTERVAL, "current": None}
# Torrents that couldn't be moved; tried again after a restart
failed_downloads = set()
# Downloading series episodes already looked up in Jellyfin
followed = set()


# Shown on the status page only, so in its language
def step(text):
    last_run["current"] = text


def record(item, library, name, state, detail, target=None, confirm=False, choose=False, title=False):
    """confirm: the match can be confirmed; choose: another TMDB title can be pasted; title: a Bulgarian title can
    be entered."""
    kind = "tv" if item.get("Type") == "Series" else "movie"
    tmdb_id = (item.get("ProviderIds") or {}).get("Tmdb")
    with lock:
        status[item["Id"]] = {
            "id": item["Id"],
            "path": item["Path"],
            "kind": kind,
            "library": library,
            "file": name,
            "state": state,
            "detail": detail,
            "target": target,
            "tag": id_tag(item),
            "tmdb": f"https://www.themoviedb.org/{kind}/{tmdb_id}" if tmdb_id and (confirm or choose) else None,
            "confirm": confirm,
            "choose": choose,
            "title": title,
            "checked": time.time(),
        }
