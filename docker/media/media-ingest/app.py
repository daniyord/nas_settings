"""media-ingest: brings new movies and series into the Jellyfin libraries (movies.py, series.py) and finished
qBittorrent downloads into them (downloads.py). The history of every action and the confirmations live in a SQLite
database in DATA_DIR; the status page is on PORT."""

import os
import threading
import time
import urllib.error

import uvicorn
from fastapi import FastAPI, HTTPException

from common.jellyfin import jellyfin
from common.web import add_page
from config import INTERVAL, LIBRARIES, PORT, QBIT_URL, SERIES_LIBRARIES, STALE_RESCAN
from downloads import download_loop
from movies import check
from series import check_series
from state import activity, db, event, last_rescan, last_run, lock, log, missing_since, ready_since, status, step, wake

# The states that wait for the user rather than for Jellyfin or the files
WAITING = ("review", "unidentified")


def run_once():
    step("чете библиотеките от Jellyfin")
    admin = next(u for u in jellyfin("/Users") if u["Policy"]["IsAdministrator"])
    folders = {f["Name"]: f for f in jellyfin("/Library/VirtualFolders")}
    seen, next_wake = set(), float("inf")
    for name in LIBRARIES:
        library = folders.get(name)
        if not library:
            log(f"error library {name} not found in Jellyfin")
            continue
        items = jellyfin(
            "/Items",
            ParentId=library["ItemId"],
            Recursive="true",
            IncludeItemTypes="Movie",
            Fields="Path,ProviderIds,OriginalTitle,ProductionYear,SortName",
            Limit=100000,
        )["Items"]
        step(f"{name}: преглежда {len(items)} филма")
        for item in items:
            path = item.get("Path", "")
            location = next((loc for loc in library["Locations"] if path.startswith(loc.rstrip("/") + "/")), None)
            if not location:
                continue
            seen.add(item["Id"])
            try:
                next_wake = min(next_wake, check(item, library, location, admin))
            except (urllib.error.URLError, TimeoutError) as e:
                log(f"error {name}/{item['Name']}: {e!r}; tried again on the next run")
    for name in SERIES_LIBRARIES:
        library = folders.get(name)
        if not library:
            log(f"error library {name} not found in Jellyfin")
            continue
        items = jellyfin(
            "/Items",
            ParentId=library["ItemId"],
            Recursive="true",
            IncludeItemTypes="Series",
            Fields="Path,ProviderIds,OriginalTitle,ProductionYear,SortName",
            Limit=100000,
        )["Items"]
        step(f"{name}: преглежда {len(items)} сериала")
        for item in items:
            parent = os.path.dirname(item.get("Path", "").rstrip("/"))
            location = next((loc for loc in library["Locations"] if parent == loc.rstrip("/")), None)
            if not location:
                continue
            seen.add(item["Id"])
            try:
                next_wake = min(next_wake, check_series(item, library, location, admin))
            except (urllib.error.URLError, TimeoutError) as e:
                log(f"error {name}/{item['Name']}: {e!r}; tried again on the next run")

    with lock:
        for gone in set(status) - seen:
            del status[gone]
    for gone in set(missing_since) - {i for i, s in status.items() if s["detail"] == "Jellyfin още показва стария път"}:
        del missing_since[gone]
    for gone in set(ready_since) - set(status):
        del ready_since[gone]
    # Jellyfin sometimes keeps a moved movie at its old path until the library is rescanned
    stale = min(missing_since.values(), default=float("inf"))
    if time.time() - stale > STALE_RESCAN and time.time() - last_rescan[0] > STALE_RESCAN:
        last_rescan[0] = time.time()
        log("rescan: Jellyfin still lists moved files at their old paths")
        jellyfin("/Library/Refresh", "POST")
    if stale < float("inf"):
        next_wake = min(next_wake, max(stale, last_rescan[0]) + STALE_RESCAN)
    return next_wake


def ingest_loop():
    while True:
        wake.clear()
        last_run["started"] = time.time()
        next_wake = float("inf")
        try:
            next_wake = run_once()
            last_run["error"] = None
        except Exception as e:
            log(f"error: {e!r}")
            last_run["error"] = repr(e)
        last_run["finished"] = time.time()
        last_run["current"] = None
        wake.wait(max(1, min(INTERVAL, next_wake - time.time() + 1)))


def summary():
    with lock:
        return {"waiting": sum(item["state"] in WAITING for item in status.values())}


app = FastAPI(title="media-ingest")
add_page(app, os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"), summary)


@app.get("/api/status")
def api_status():
    with lock:
        items = list(status.values())
        recent = [
            dict(r)
            for r in db.execute(
                "SELECT at, path, detail FROM events WHERE kind = 'moved' ORDER BY at DESC, id DESC LIMIT 30"
            )
        ]
    return {"run": last_run, "items": items, "recent": recent}


@app.get("/api/activity")
def api_activity(after: int = 0):
    return {"run": last_run, "lines": activity.since(after), "boot": activity.boot}


@app.post("/api/run")
def run_now():
    wake.set()
    return {"run": last_run}


@app.post("/api/items/{item_id}/confirm")
def confirm(item_id: str):
    with lock:
        entry = status.get(item_id)
        if not entry:
            raise HTTPException(404, "unknown item")
        if not entry["confirm"]:
            raise HTTPException(409, f"item is {entry['state']}")
        db.execute("INSERT OR REPLACE INTO confirmed VALUES (?, ?, ?)", (entry["path"], entry["tag"], time.time()))
        event(entry["path"], "confirmed", f"{entry['tag']} → {entry['target']}", f"{entry['library']}/{entry['file']}")
        entry.update(state="pending", detail="потвърден, чака реда си", confirm=False)
    wake.set()
    return entry


if __name__ == "__main__":
    threading.Thread(target=ingest_loop, daemon=True).start()
    if QBIT_URL:
        threading.Thread(target=download_loop, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
