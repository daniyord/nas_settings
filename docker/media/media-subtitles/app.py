"""media-subtitles: checks and fixes the sync of the libraries' subtitles (sync.py), downloads Bulgarian ones where
there are none (download.py) and asks about sound that names no language (audio.py). What it found lives in a
SQLite database in DATA_DIR; the status page is on PORT."""

import os
import shutil
import threading
import time
import urllib.error

import uvicorn
from fastapi import FastAPI, HTTPException

import audio
import media_json
from common.media import Conflict
from common.web import add_page
from config import INTERVAL, PORT, WANTED_REFRESH
from download import download_step, wanted_subtitles
from state import (
    activity,
    db,
    downloads_run,
    event,
    item_ids,
    known_items,
    lock,
    log,
    read_locations,
    search_first,
    refresh_video,
    subtitle_label,
    sync_run,
    wake,
)
from sync import (
    apply_fix,
    auto_fix,
    check_subtitle,
    convert_microdvd_files,
    pending_subtitles,
    safe_to_fix,
    synced_path,
)


def subtitle_loop():
    while True:
        wake.clear()
        try:
            read_locations()
            convert_microdvd_files()
            auto_fix()
            todo = pending_subtitles()
            sync_run["queued"] = len(todo)
            # What the user asked for doesn't wait for the checks
            if search_first and download_step(asked_only=True):
                continue
            if todo:
                check_subtitle(*todo[0])
                continue
            # Only when every subtitle on disk is checked: a new download's own check comes first
            if download_step():
                continue
        except Exception as e:
            log(f"error subtitles: {e!r}")
            sync_run["current"] = None
        wake.wait(INTERVAL)


def summary():
    """Subtitles too far off to fix on their own, and titles whose sound waits for an answer."""
    with lock:
        offsets = db.execute("SELECT offset, scale FROM subtitles WHERE state = 'offset'").fetchall()
        unanswered = sum(group["decision"] is None for group in downloads_run["review"].values())
    return {"waiting": sum(not safe_to_fix(r["offset"], r["scale"]) for r in offsets) + unanswered}


def relist_soon():
    """The lists of videos wanting subtitles and of sound to ask about are made again a minute after the last answer:
    listing every video takes Jellyfin a while, and answers come in runs. The page shows an answer right away."""
    downloads_run["listed_at"] = time.time() - WANTED_REFRESH + 60


app = FastAPI(title="media-subtitles")
add_page(app, os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"), summary)


@app.get("/api/status")
def api_status():
    try:
        ids = known_items()
    except (urllib.error.URLError, TimeoutError):
        ids = item_ids["ids"]
    with lock:
        subtitles = [
            {
                **dict(r),
                "label": subtitle_label(r["path"]),
                "item": ids.get(r["video"]),
                "searching": r["video"] in search_first,
            }
            for r in db.execute(
                "SELECT s.id, s.path, s.video, s.checked_at, s.state, s.offset, s.scale, s.detail,"
                " d.state AS search_state, d.detail AS search_detail, d.checked_at AS searched_at"
                " FROM subtitles s LEFT JOIN downloads d ON d.video = s.video WHERE s.state != 'ok'"
                " ORDER BY s.checked_at DESC"
            )
        ]
        counts = dict(db.execute("SELECT state, COUNT(*) FROM subtitles GROUP BY state").fetchall())
        downloads = [
            {**dict(r), "label": subtitle_label(r["video"]), "item": ids.get(r["video"])}
            for r in db.execute(
                "SELECT video, state, checked_at, detail FROM downloads WHERE state != 'none' ORDER BY checked_at DESC LIMIT 200"
            )
        ]
        download_counts = dict(db.execute("SELECT state, COUNT(*) FROM downloads GROUP BY state").fetchall())
        review = sorted(
            downloads_run["review"].values(), key=lambda g: (g["decision"] is not None, g["library"], g["title"])
        )
        # How far the search for subtitles got in each folder answered with "needs subtitles"
        review = [
            {
                **g,
                "search": {
                    "wanted": sum(w["video"].startswith(g["folder"] + "/") for w in downloads_run["wanted"]),
                    **dict(
                        db.execute(
                            "SELECT state, COUNT(*) FROM downloads WHERE video LIKE ? ESCAPE '\\' GROUP BY state",
                            (g["folder"].replace("%", r"\%").replace("_", r"\_") + "/%",),
                        ).fetchall()
                    ),
                },
            }
            if g["decision"] == "subs"
            else g
            for g in review
        ]
    return {
        "review": review,
        "subtitles": {"run": sync_run, "counts": counts, "items": subtitles},
        "downloads": {"counts": download_counts, "wanted": len(downloads_run["wanted"]), "items": downloads},
    }


@app.get("/api/activity")
def api_activity(after: int = 0):
    return {"sync": sync_run, "lines": activity.since(after), "boot": activity.boot}


def subtitle_row(sub_id, *states):
    row = db.execute("SELECT * FROM subtitles WHERE id = ?", (sub_id,)).fetchone()
    if not row:
        raise HTTPException(404, "unknown subtitle")
    if row["state"] not in states:
        raise HTTPException(409, f"subtitle is {row['state']}")
    return row


@app.post("/api/subtitles/{sub_id}/fix")
def fix_subtitle(sub_id: int):
    with lock:
        row = subtitle_row(sub_id, "offset")
        try:
            apply_fix(row)
        except Conflict as e:
            raise HTTPException(409, str(e)) from None
    refresh_video(row["video"], subtitle_label(row["path"]))
    return {"ok": True}


@app.post("/api/subtitles/{sub_id}/dismiss")
def dismiss_subtitle(sub_id: int):
    with lock:
        # A failed check too, when the user finds the subtitle fine
        row = subtitle_row(sub_id, "offset", "failed")
        db.execute("UPDATE subtitles SET state = 'dismissed' WHERE id = ?", (sub_id,))
        media_json.remember_dismissed(row["path"])
        if os.path.exists(synced_path(row["path"])):
            os.remove(synced_path(row["path"]))
        event(
            row["path"], "dismissed", f"{os.path.basename(row['path'])}: {row['detail']}", subtitle_label(row["path"])
        )
    # Off the lists of videos wanting subtitles and of sound to ask about
    relist_soon()
    return {"ok": True}


@app.post("/api/subtitles/{sub_id}/search")
def search_other(sub_id: int):
    with lock:
        row = subtitle_row(sub_id, "offset")
    video = row["video"]
    if not downloads_run["listed_at"]:
        downloads_run["wanted"], downloads_run["review"] = wanted_subtitles()
        downloads_run["listed_at"] = time.time()
    group = next((g for f, g in downloads_run["review"].items() if video.startswith(f + "/")), None)
    if group and group["decision"] == "bg":
        raise HTTPException(409, "аудиото е отбелязано като български дублаж: смени отговора, ако трябват субтитри")
    if group and not group["decision"]:
        # Asking for other subtitles says the sound without a language isn't a Bulgarian dub
        audio.answer(group["folder"], "subs", group)
        downloads_run["listed_at"] = 0
    elif not group and not any(w["video"] == video for w in downloads_run["wanted"]):
        raise HTTPException(409, "видеото не е в списъка за теглене (има вградени български субтитри или аудио)")
    if video not in search_first:
        search_first.append(video)
        event(row["path"], "search", f"{os.path.basename(row['path'])}: търси други", subtitle_label(row["path"]))
    wake.set()
    return {"ok": True}


@app.post("/api/subtitles/{sub_id}/restore")
def restore_subtitle(sub_id: int):
    with lock:
        row = subtitle_row(sub_id, "fixed")
        path, orig = row["path"], row["path"] + ".orig"
        if not os.path.isfile(orig):
            raise HTTPException(409, f"{os.path.basename(orig)} is gone")
        # Rewritten in place for the same reason as in apply_fix
        shutil.copyfile(orig, path)
        os.remove(orig)
        st = os.stat(path)
        # Recorded as checked, so the restored file isn't aligned again
        db.execute(
            "UPDATE subtitles SET state = 'restored', size = ?, mtime = ?, checked_at = ? WHERE id = ?",
            (st.st_size, st.st_mtime, time.time(), sub_id),
        )
        event(path, "restored", f"{os.path.basename(path)}: върнат оригиналът", subtitle_label(path))
    refresh_video(row["video"], subtitle_label(path))
    return {"ok": True}


@app.post("/api/audio/subtitles-ok")
def audio_subtitles_ok(body: dict):
    # Only folders from the list, so no request names an arbitrary path
    group = downloads_run["review"].get(body.get("folder"))
    if not group or not group["bad"]:
        raise HTTPException(404, "no subtitles to accept for this folder")
    with lock:
        for path in group["bad"]:
            done = db.execute(
                "UPDATE subtitles SET state = 'dismissed' WHERE path = ? AND state IN ('failed', 'offset')", (path,)
            ).rowcount
            if done:
                media_json.remember_dismissed(path)
                event(path, "dismissed", f"{os.path.basename(path)}: приети за наред", subtitle_label(path))
    # Off the question right away; the next listing leaves it out too
    downloads_run["review"].pop(group["folder"], None)
    relist_soon()
    wake.set()
    return {"ok": True}


@app.post("/api/audio")
def audio_decision(body: dict):
    folder, decision = body.get("folder"), body.get("decision")
    if decision not in ("bg", "subs", "burned", None):
        raise HTTPException(400, "decision is bg, subs, burned or null")
    # Only folders from the list, so no request names an arbitrary path
    group = downloads_run["review"].get(folder)
    if not group:
        raise HTTPException(404, "unknown folder")
    audio.answer(folder, decision, group)
    relist_soon()
    wake.set()
    return {"ok": True}


if __name__ == "__main__":
    threading.Thread(target=subtitle_loop, daemon=True).start()
    threading.Thread(target=audio.resume, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
