"""media-subtitles: checks and fixes the sync of the libraries' subtitles (sync.py), downloads Bulgarian ones where
there are none (download.py) and asks about sound that names no language (audio.py). What it found lives in a
SQLite database in DATA_DIR; the status page is on PORT."""

import os
import shutil
import threading
import time

import uvicorn
from fastapi import FastAPI, HTTPException

import audio
from common.media import Conflict
from common.web import add_page
from config import INTERVAL, PORT
from download import download_step
from state import (
    activity,
    db,
    downloads_run,
    event,
    lock,
    log,
    read_locations,
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


app = FastAPI(title="media-subtitles")
add_page(app, os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"), summary)


@app.get("/api/status")
def api_status():
    with lock:
        subtitles = [
            {**dict(r), "label": subtitle_label(r["path"])}
            for r in db.execute(
                "SELECT id, path, checked_at, state, offset, scale, detail FROM subtitles WHERE state != 'ok' ORDER BY checked_at DESC"
            )
        ]
        counts = dict(db.execute("SELECT state, COUNT(*) FROM subtitles GROUP BY state").fetchall())
        downloads = [
            {**dict(r), "label": subtitle_label(r["video"])}
            for r in db.execute(
                "SELECT video, state, checked_at, detail FROM downloads WHERE state != 'none' ORDER BY checked_at DESC LIMIT 200"
            )
        ]
        download_counts = dict(db.execute("SELECT state, COUNT(*) FROM downloads GROUP BY state").fetchall())
        review = sorted(
            downloads_run["review"].values(), key=lambda g: (g["decision"] is not None, g["library"], g["title"])
        )
    return {
        "review": review,
        "subtitles": {"run": sync_run, "counts": counts, "items": subtitles},
        "downloads": {"counts": download_counts, "wanted": len(downloads_run["wanted"]), "items": downloads},
    }


@app.get("/api/activity")
def api_activity(after: int = 0):
    return {"sync": sync_run, "lines": activity.since(after), "boot": activity.boot}


def subtitle_row(sub_id, state):
    row = db.execute("SELECT * FROM subtitles WHERE id = ?", (sub_id,)).fetchone()
    if not row:
        raise HTTPException(404, "unknown subtitle")
    if row["state"] != state:
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
        row = subtitle_row(sub_id, "offset")
        db.execute("UPDATE subtitles SET state = 'dismissed' WHERE id = ?", (sub_id,))
        if os.path.exists(synced_path(row["path"])):
            os.remove(synced_path(row["path"]))
        event(
            row["path"], "dismissed", f"{os.path.basename(row['path'])}: {row['detail']}", subtitle_label(row["path"])
        )
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


@app.post("/api/audio")
def audio_decision(body: dict):
    folder, decision = body.get("folder"), body.get("decision")
    if decision not in ("bg", "subs", None):
        raise HTTPException(400, "decision is bg, subs or null")
    # Only folders from the list, so no request names an arbitrary path
    group = downloads_run["review"].get(folder)
    if not group:
        raise HTTPException(404, "unknown folder")
    audio.answer(folder, decision, group)
    # The list of videos to find subtitles for is made again on the next step
    downloads_run["listed_at"] = 0
    wake.set()
    return {"ok": True}


if __name__ == "__main__":
    threading.Thread(target=subtitle_loop, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
