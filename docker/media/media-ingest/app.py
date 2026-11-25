"""media-ingest: brings new movies and series into the Jellyfin libraries (movies.py, series.py) and finished
qBittorrent downloads into them (downloads.py). The history of every action and the confirmations live in a SQLite
database in DATA_DIR; the status page is on PORT."""

import json
import os
import threading
import time
import urllib.error

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from common import nfo, tmdb
from common.jellyfin import jellyfin, reread
from common.web import NO_CACHE, add_page
from config import INTERVAL, LIBRARIES, PEOPLE_DIR, PORT, QBIT_URL, SERIES_LIBRARIES, STALE_RESCAN
from boxsets import boxsets_loop
from downloads import download_loop
from metadata import as_item, details, from_link, own
from movies import check
from names import bg_sort, id_tag, target_name
from metadata import confirmed_names, names_file
from people import catalog, catalog_loop, last_finished, names_loop, names_wake, people, people_loop
from people import wake as people_wake
from cast import cast_loop
from cast import progress as cast_progress
from cast import wake as cast_wake
from migrate import migrate_loop
from migrate import summary as migration
from migrate import wake as migrate_wake
from series import check_series, reread_new_episodes
from userdata import export_loop
from state import activity, db, event, last_rescan, last_run, lock, log, missing_since, status, step, wake

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
        try:
            reread_new_episodes(library, admin)
        except (urllib.error.URLError, TimeoutError) as e:
            log(f"error {name} new episodes: {e!r}; tried again on the next run")

    with lock:
        for gone in set(status) - seen:
            del status[gone]
    for gone in set(missing_since) - {i for i, s in status.items() if s["detail"] == "Jellyfin още показва стария път"}:
        del missing_since[gone]
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
    return {
        "run": last_run,
        "items": items,
        "people": {**people, "finished": last_finished()},
        "migration": migration(),
        "cast": cast_progress,
        "actor_names": actor_names(),
    }


@app.get("/api/activity")
def api_activity(after: int = 0):
    return {"run": last_run, "lines": activity.since(after), "boot": activity.boot}


@app.post("/api/run")
def run_now():
    wake.set()
    return {"run": last_run}


@app.post("/api/migrate/run")
def run_migration():
    migrate_wake.set()
    return {"ok": True}


@app.post("/api/cast/run")
def run_cast():
    cast_wake.set()
    return {"ok": True}


def actor_names():
    with lock:
        rows = db.execute("SELECT tmdb_id, latin, suggestion, title FROM actor_names ORDER BY title, latin").fetchall()
    return [dict(r) for r in rows]


class Name(BaseModel):
    name: str


@app.post("/api/actors/{tmdb_id}/name")
def set_actor_name(tmdb_id: int, body: Name):
    """The name an actor gets in every NFO (Cyrillic, or the Latin one to keep it); applied in the background."""
    name = " ".join(body.name.split())
    if not name:
        raise HTTPException(400, "името е празно")
    known = (catalog["actors"] or {}).get(tmdb_id)
    with lock:
        row = db.execute("SELECT latin FROM actor_names WHERE tmdb_id = ?", (tmdb_id,)).fetchone()
        if row:
            before = row[0]
        elif known:
            before = known["name"]
        else:
            raise HTTPException(404, "unknown actor")
        names = {str(k): v for k, v in confirmed_names().items()}
        names[str(tmdb_id)] = name
        path = names_file()
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(names, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(path + ".tmp", path)
        own(path, os.path.dirname(path))
        db.execute("DELETE FROM actor_names WHERE tmdb_id = ?", (tmdb_id,))
        event(path, "people", f"{before} → {name}", "Актьори")
    if known:
        known["name"] = name
    names_wake.set()
    return {"ok": True}


@app.get("/people")
def people_page():
    return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), "people.html"), headers=NO_CACHE)


@app.get("/api/actors")
def list_actors():
    """Every actor of the NFOs as [id, name, number of titles, whether there is a photo], most titles first."""
    actors = catalog["actors"]
    if actors is None:
        return {"building": True, "built": None, "actors": []}
    photos = set(os.listdir(PEOPLE_DIR)) if os.path.isdir(PEOPLE_DIR) else set()
    rows = sorted(actors.values(), key=lambda a: (-len(a["titles"]), a["name"].casefold()))
    return {
        "building": catalog["building"],
        "built": catalog["built"],
        "actors": [[a["id"], a["name"], len(a["titles"]), f"{a['id']}.jpg" in photos] for a in rows],
    }


@app.get("/api/actors/{tmdb_id}")
def actor(tmdb_id: int):
    found = (catalog["actors"] or {}).get(tmdb_id)
    if not found:
        raise HTTPException(404, "unknown actor")
    titles = sorted(found["titles"], key=lambda t: (t["year"] or "9999", t["title"]), reverse=True)
    return {**found, "titles": titles, "confirmed": tmdb_id in confirmed_names()}


@app.get("/api/actors/{tmdb_id}/photo")
def actor_photo(tmdb_id: int):
    path = os.path.join(PEOPLE_DIR, f"{tmdb_id}.jpg")
    if not os.path.isfile(path):
        raise HTTPException(404, "no photo")
    return FileResponse(path, headers={"Cache-Control": "max-age=86400"})


@app.post("/api/people/run")
def run_people():
    people_wake.set()
    return {"people": people}


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


class Link(BaseModel):
    link: str


@app.post("/api/items/{item_id}/match")
def match(item_id: str, body: Link):
    """Another TMDB title for a release, from a pasted TMDB or IMDb link; taken as confirmed."""
    with lock:
        entry = status.get(item_id)
    if not entry:
        raise HTTPException(404, "unknown item")
    if not entry["choose"]:
        raise HTTPException(409, f"item is {entry['state']}")
    kind = entry["kind"]
    what = "филм" if kind == "movie" else "сериал"
    tmdb_id = from_link(kind, body.link)
    if not tmdb_id:
        raise HTTPException(400, f"това не е линк към {what} в TMDB или IMDb")
    try:
        found = as_item({"Id": item_id}, kind, details(kind, tmdb_id))
    except tmdb.NotFound:
        raise HTTPException(400, f"TMDB няма {what} {tmdb_id}") from None
    if not found["ProductionYear"]:
        raise HTTPException(400, f"{what}ът в TMDB е без година")
    tag, target = id_tag(found), target_name(found)
    with lock:
        db.execute("INSERT OR REPLACE INTO chosen VALUES (?, ?, ?)", (entry["path"], tmdb_id, time.time()))
        db.execute("INSERT OR REPLACE INTO confirmed VALUES (?, ?, ?)", (entry["path"], tag, time.time()))
        event(entry["path"], "chosen", f"{tag} → {target}", f"{entry['library']}/{entry['file']}")
        entry.update(state="pending", detail="избран, чака реда си", target=target, confirm=False, choose=False)
    wake.set()
    return entry


class Title(BaseModel):
    title: str = ""
    keep: bool = False


@app.post("/api/items/{item_id}/title")
def set_title(item_id: str, body: Title):
    """The Bulgarian title of an organized movie or series TMDB has none for, into its NFO and Jellyfin; or keep
    the original one."""
    with lock:
        entry = status.get(item_id)
    if not entry:
        raise HTTPException(404, "unknown item")
    if not entry["title"]:
        raise HTTPException(409, f"item is {entry['state']}")
    folder = entry["path"].rstrip("/") if entry["kind"] == "tv" else os.path.dirname(entry["path"])
    kind = "tvshow" if entry["kind"] == "tv" else "movie"
    title = " ".join(body.title.split())
    label = f"{entry['library']}/{entry['file']}"
    if body.keep:
        event(folder, "titled", "остава оригиналното заглавие", label)
    elif not title:
        raise HTTPException(400, "заглавието е празно")
    else:
        nfo.save(
            kind,
            folder,
            [nfo.element("title", title), nfo.element("sorttitle", bg_sort(title))],
            replace={"title", "sorttitle"},
        )
        own(nfo.path_for(kind, folder), folder)
        admin = next(u for u in jellyfin("/Users") if u["Policy"]["IsAdministrator"])
        reread(item_id, admin["Id"])
        event(folder, "titled", title, label)
    with lock:
        db.execute("DELETE FROM untitled WHERE path = ?", (folder,))
        status.pop(item_id, None)
    return {"ok": True}


if __name__ == "__main__":
    threading.Thread(target=ingest_loop, daemon=True).start()
    threading.Thread(target=people_loop, daemon=True).start()
    threading.Thread(target=names_loop, daemon=True).start()
    threading.Thread(target=catalog_loop, daemon=True).start()
    threading.Thread(target=boxsets_loop, daemon=True).start()
    threading.Thread(target=migrate_loop, daemon=True).start()
    threading.Thread(target=cast_loop, daemon=True).start()
    threading.Thread(target=export_loop, args=(log, event), daemon=True).start()
    if QBIT_URL:
        threading.Thread(target=download_loop, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
