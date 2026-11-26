"""media-adult: the adult libraries, „Порно“ and „Порно - сцени“ (PLAN_media_services.md, stage 4). Their NFOs and
artwork come from ThePornDB for the movies and from the file for the scenes; Jellyfin reads them offline. What it did
lives in a SQLite database in DATA_DIR; the status page is on PORT."""

import json
import os
import shutil
import threading
import time
import urllib.error

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response

import incoming
import match
import poster
import scenes
from people import PERFORMER_ID, catalog, catalog_loop, people, people_loop, people_wake
from common.jellyfin import jellyfin
from common.web import NO_CACHE, add_page
from config import DOWNLOAD_INTERVAL, INTERVAL, LIBRARY, PEOPLE_DIR, PORT, SCENES_LIBRARY, SCENES_SUBDIR
from library import studios
from state import activity, db, downloads, libraries, lock, log, run, wake

# Wakes the download loop before its rest is over (an answer on the status page)
incoming_wake = threading.Event()


def pass_once():
    folders = {f["Name"]: f for f in jellyfin("/Library/VirtualFolders")}
    found = []
    for name in (LIBRARY, SCENES_LIBRARY):
        if name not in folders:
            log(f"error {name}: no such library in Jellyfin")
            continue
        run["current"] = f"{name}: преброяване"
        counted = studios(folders[name]["Locations"])
        found.append({"name": name, "locations": folders[name]["Locations"], "studios": counted})
        if name == SCENES_LIBRARY:
            run["current"] = f"{name}: NFO и постери"
            scenes.fill(folders[name]["Locations"], folders[name]["ItemId"])
    with lock:
        libraries[:] = found


def loop():
    while True:
        wake.clear()
        run["started"] = time.time()
        try:
            pass_once()
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            log(f"error pass: {e!r}")
        run.update(current=None, finished=time.time())
        wake.wait(INTERVAL)


def incoming_loop():
    while True:
        incoming_wake.clear()
        try:
            incoming.pass_once()
        except Exception as e:
            log(f"error downloads: {e!r}")
        incoming_wake.wait(DOWNLOAD_INTERVAL)


WAITING = ("doubt", "duplicate", "failed")


def summary():
    """Downloads that wait for an answer."""
    with lock:
        count = db.execute(
            f"SELECT COUNT(*) FROM incoming WHERE state IN ({','.join('?' * len(WAITING))})", WAITING
        ).fetchone()[0]
    return {"waiting": count}


app = FastAPI(title="media-adult")
add_page(app, os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"), summary)


@app.get("/api/status")
def api_status():
    with lock:
        rows = db.execute(
            "SELECT * FROM incoming ORDER BY state = 'done', COALESCE(checked_at, added_at) DESC LIMIT 200"
        ).fetchall()
        by_name = {library["name"]: library for library in libraries}
        return {
            "run": run,
            "libraries": list(libraries),
            "people": people,
            "downloads": list(downloads),
            "studios": sorted(by_name[LIBRARY]["studios"]) if LIBRARY in by_name else [],
            "scene_studios": sorted(by_name[SCENES_LIBRARY]["studios"]) if SCENES_LIBRARY in by_name else [],
            "incoming": [
                {**dict(r), "keywords": json.loads(r["keywords"]), "candidates": json.loads(r["candidates"])}
                for r in rows
            ],
        }


def waiting_row(row_id):
    item = incoming.row(row_id)
    if not item or item["state"] not in WAITING + ("new",):
        raise HTTPException(404, "няма такова изтегляне, което да чака")
    return item


@app.post("/api/incoming/{row_id}/choose")
def choose(row_id: int, body: dict):
    """A candidate or a ThePornDB / TMDB link, with the studio picked (or typed for a new one)."""
    waiting_row(row_id)
    pin = match.pinned(body.get("link") or "") or (
        (body["source"], str(body["id"])) if body.get("source") in ("tpdb", "tmdb") else None
    )
    if not pin:
        raise HTTPException(400, "не е линк към ThePornDB или TMDB")
    studio = (body.get("studio") or "").strip() or None
    if pin[0] == "tmdb" and not studio:
        raise HTTPException(400, "изберете студио")
    incoming.place(row_id, pin, studio)
    return dict(incoming.row(row_id) or {})


@app.post("/api/incoming/{row_id}/scene")
def scene(row_id: int, body: dict):
    """A scene's studio (picked, or typed for a new one) and its title, empty for its file's name."""
    if waiting_row(row_id)["kind"] != "scene":
        raise HTTPException(400, "не е сцена")
    studio = (body.get("studio") or "").strip()
    if not studio:
        raise HTTPException(400, "изберете студио")
    incoming.place_scene(row_id, studio, (body.get("title") or "").strip(), float(body.get("at", 0.25)))
    return dict(incoming.row(row_id) or {})


@app.post("/api/incoming/{row_id}/keywords")
def keywords(row_id: int, body: dict):
    """Other titles to search with, and the download is matched again."""
    waiting_row(row_id)
    words = [w.strip() for w in body.get("keywords", []) if w.strip()]
    incoming.update(row_id, keywords=words, state="new", detail="търси отново")
    incoming_wake.set()
    return {"ok": True}


@app.post("/api/incoming/{row_id}/by-hand")
def by_hand(row_id: int, body: dict):
    """A movie no database knows: its title, studio and the moment of the video for its poster."""
    waiting_row(row_id)
    title, studio = (body.get("title") or "").strip(), (body.get("studio") or "").strip()
    if not title or not studio:
        raise HTTPException(400, "заглавие и студио")
    incoming.place(row_id, None, studio, title, float(body.get("at", 0.25)))
    return dict(incoming.row(row_id) or {})


@app.get("/api/incoming/{row_id}/poster")
def poster_preview(row_id: int, title: str, studio: str, at: float = 0.25):
    item = waiting_row(row_id)
    video = item["video"] or incoming.main_video(item["path"])
    if not video or not os.path.exists(video):
        raise HTTPException(404, "няма видео")
    image = poster.make(poster.video_frame(video, at), title, studio)
    return Response(image, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.delete("/api/incoming/{row_id}")
def delete_download(row_id: int):
    """Deletes a download from its download folder (a duplicate, or one that isn't wanted), never from a library."""
    item = waiting_row(row_id)
    path = item["path"]
    inside = [
        f for f in downloads if os.path.commonpath([f, path]) == f.rstrip("/") and path.rstrip("/") != f.rstrip("/")
    ]
    if not inside or os.path.basename(path) == SCENES_SUBDIR:
        raise HTTPException(400, "не е в папка за сваляне")
    if os.path.isdir(path):
        shutil.rmtree(path)
    elif os.path.exists(path):
        os.remove(path)
    with lock:
        db.execute("DELETE FROM incoming WHERE id = ?", (row_id,))
    log(f"deleted {os.path.basename(path)}: изтрито от папката за сваляне")
    return {"ok": True}


@app.get("/performers")
def performers_page():
    return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), "performers.html"), headers=NO_CACHE)


@app.get("/api/performers")
def list_performers():
    """Every performer of the NFOs as [id, name, number of titles, whether there is a photo, studios], most titles first; the
    movies with none; the performers ThePornDB has no photo of."""
    performers = catalog["performers"]
    if performers is None:
        return {"building": True, "built": None, "performers": [], "empty": [], "no_photo": []}
    photos = set(os.listdir(PEOPLE_DIR)) if os.path.isdir(PEOPLE_DIR) else set()
    rows = sorted(performers.values(), key=lambda p: (-len(p["titles"]), p["name"].casefold()))
    return {
        "building": catalog["building"],
        "built": catalog["built"],
        "performers": [
            [
                p["id"],
                p["name"],
                len(p["titles"]),
                f"{p['id']}.jpg" in photos,
                sorted({t["studio"] for t in p["titles"]}),
            ]
            for p in rows
        ],
        "empty": catalog["empty"],
        "no_photo": sorted(
            (performers[f"tpdb-{i}"]["name"], f"tpdb-{i}") for i in people["missing"] if f"tpdb-{i}" in performers
        ),
    }


@app.get("/api/performers/{performer_id}")
def performer(performer_id: str):
    found = (catalog["performers"] or {}).get(performer_id)
    if not found:
        raise HTTPException(404, "няма такъв изпълнител")
    titles = sorted(found["titles"], key=lambda t: (t["year"] or "9999", t["title"]), reverse=True)
    return {**found, "titles": titles}


@app.get("/api/performers/{performer_id}/photo")
def performer_photo(performer_id: str):
    path = os.path.join(PEOPLE_DIR, f"{performer_id}.jpg")
    if not PERFORMER_ID.fullmatch(performer_id) or not os.path.isfile(path):
        raise HTTPException(404, "няма снимка")
    return FileResponse(path, headers={"Cache-Control": "max-age=86400"})


@app.post("/api/people/run")
def run_people():
    people_wake.set()
    return {"ok": True}


@app.get("/api/activity")
def api_activity(after: int = 0):
    return {"run": run, "lines": activity.since(after), "boot": activity.boot}


@app.post("/api/run")
def run_now():
    wake.set()
    incoming_wake.set()
    return {"ok": True}


if __name__ == "__main__":
    threading.Thread(target=loop, daemon=True).start()
    threading.Thread(target=incoming_loop, daemon=True).start()
    threading.Thread(target=people_loop, daemon=True).start()
    threading.Thread(target=catalog_loop, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
