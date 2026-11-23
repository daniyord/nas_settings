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
""",
)
activity = Activity(db, lock)
log, event = activity.log, activity.event

# In memory only: rebuilt from Jellyfin on every run
status = {}
ready_since = {}
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


def record(item, library, name, state, detail, target=None, confirm=False):
    with lock:
        status[item["Id"]] = {
            "id": item["Id"],
            "path": item["Path"],
            "library": library,
            "file": name,
            "state": state,
            "detail": detail,
            "target": target,
            "tag": id_tag(item),
            "confirm": confirm,
            "checked": time.time(),
        }
