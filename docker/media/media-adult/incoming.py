"""New movies for „Порно“ (PLAN_media_services.md, stage 4): a finished torrent in the qBittorrent category CATEGORY,
or anything copied into a download folder by hand. qBittorrent lets go of the torrent and the download stays where
it is, out of Jellyfin's sight, until it is known which movie it is: then its folder is made in the library on the
same disk, under its studio, with our NFO and artwork, and the video moves in (a rename). What can't be matched with
confidence waits on the status page. A scene (the category SCENES_CATEGORY, in the download folder's SCENES_SUBDIR)
isn't looked up: it waits there for its studio, and goes under it in „Порно - сцени“."""

import json
import os
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET

import match
import metadata
import people
from common import qbit as qbittorrent
from common import tmdb, tpdb
from common.jellyfin import jellyfin
from common.media import VIDEO_EXTS
from common.qbit import qbit, translate
from config import (
    CATEGORY,
    DOWNLOAD_DIRS,
    LIBRARY,
    MERGE_STUDIOS,
    QBIT_PATHS,
    SCENES_CATEGORY,
    SCENES_LIBRARY,
    SCENES_SUBDIR,
    SETTLE_AFTER,
)
from state import db, downloads, event, lock, log

# A folder whose other videos are bigger than this keeps them: they may be a second movie rather than a sample
LEFTOVER_VIDEO = 100 * 1024 * 1024


def clean(title):
    title = re.sub(r"\s*:\s*", " - ", title).replace("/", "-")
    return re.sub(r"\s+", " ", re.sub(r'[\\?*"<>|]', "", title)).strip(" .")


def library_locations(name=LIBRARY):
    folders = {f["Name"]: f for f in jellyfin("/Library/VirtualFolders")}
    return folders[name]["Locations"] if name in folders else []


def studios(locations):
    """Every studio folder of the library, over all its folders."""
    return sorted(
        {
            name
            for loc in locations
            if os.path.isdir(loc)
            for name in os.listdir(loc)
            if not name.startswith(".") and os.path.isdir(os.path.join(loc, name))
        }
    )


def download_folders():
    return [local for path in DOWNLOAD_DIRS if (local := translate(path, QBIT_PATHS)) and os.path.isdir(local)]


def scene_folders():
    return [path for folder in downloads if os.path.isdir(path := os.path.join(folder, SCENES_SUBDIR))]


def choose_save_path(category, subdir=""):
    """Points the category at the download folder with the most free space (at its subfolder `subdir`)."""
    options = [
        (shutil.disk_usage(local).free, path)
        for path in DOWNLOAD_DIRS
        if (local := translate(path, QBIT_PATHS)) and os.path.isdir(local)
    ]
    if not options:
        return
    free, best = max(options)
    best = f"{best.rstrip('/')}/{subdir}" if subdir else best
    current = qbit("torrents/categories").get(category)
    if current and current.get("savePath", "").rstrip("/") == best.rstrip("/"):
        return
    qbit("torrents/editCategory" if current else "torrents/createCategory", category=category, savePath=best)
    log(f"qBittorrent: {category} се сваля в {best} ({free / 1e12:.1f} TB свободни)")


def take_finished(torrents):
    """qBittorrent lets go of the category's finished torrents; their files stay for the matcher."""
    for torrent in torrents:
        if (
            torrent["category"] not in (CATEGORY, SCENES_CATEGORY)
            or torrent["progress"] < 1
            or torrent["state"] == "moving"
        ):
            continue
        qbit("torrents/delete", hashes=torrent["hash"], deleteFiles="false")
        log(f"qBittorrent: {torrent['name']} е свален")


def main_video(path):
    """The biggest video of a download (a file or a folder), or None."""
    if os.path.isfile(path):
        return path if os.path.splitext(path)[1].lower() in VIDEO_EXTS else None
    videos = [
        os.path.join(root, f)
        for root, _, files in os.walk(path)
        for f in files
        if os.path.splitext(f)[1].lower() in VIDEO_EXTS
    ]
    return max(videos, key=os.path.getsize) if videos else None


def runtime(video):
    """The video's length in minutes, or None."""
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", video],
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
        return round(float(probe.stdout.strip()) / 60) or None
    except (subprocess.SubprocessError, ValueError):
        return None


def scan(torrents):
    """Adds what is new in the download folders (not a torrent still downloading, nor a file still being copied)
    and forgets what is gone from them."""
    downloading = {translate(t["content_path"], QBIT_PATHS) for t in torrents if t["progress"] < 1}
    present = set()
    for folder, kind in [(f, "movie") for f in downloads] + [(f, "scene") for f in scene_folders()]:
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if name.startswith(".") or name.endswith((".part", ".!qB")) or path in downloading:
                continue
            if kind == "movie" and name == SCENES_SUBDIR:
                continue
            present.add(path)
            if time.time() - max(os.stat(path).st_ctime, os.stat(path).st_mtime) < SETTLE_AFTER:
                continue
            with lock:
                db.execute(
                    "INSERT OR IGNORE INTO incoming (path, kind, state, added_at) VALUES (?, ?, 'new', ?)",
                    (path, kind, time.time()),
                )
    with lock:
        for row in db.execute("SELECT id, path FROM incoming WHERE state != 'done'").fetchall():
            if row["path"] not in present:
                db.execute("DELETE FROM incoming WHERE id = ?", (row["id"],))


def update(row_id, **fields):
    for key in ("keywords", "candidates"):
        if key in fields:
            fields[key] = json.dumps(fields[key], ensure_ascii=False)
    with lock:
        db.execute(
            f"UPDATE incoming SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", (*fields.values(), row_id)
        )


def row(row_id):
    with lock:
        return db.execute("SELECT * FROM incoming WHERE id = ?", (row_id,)).fetchone()


def tagged(locations, tag):
    """The library folder named with this id tag, or None."""
    for location in locations:
        if not os.path.isdir(location):
            continue
        for studio in os.listdir(location):
            studio_dir = os.path.join(location, studio)
            if os.path.isdir(studio_dir):
                for name in os.listdir(studio_dir):
                    if f"[{tag}]" in name:
                        return os.path.join(studio_dir, name)
    return None


def name_of(path):
    """A download's name without the extension of a file."""
    return os.path.basename(path) if os.path.isdir(path) else os.path.splitext(os.path.basename(path))[0]


def scene_waits(item):
    """A scene isn't looked up: it waits on the status page for its studio (and title)."""
    path = item["path"]
    video = main_video(path)
    if not video:
        update(item["id"], state="failed", detail="няма видео", checked_at=time.time())
        return
    update(
        item["id"],
        video=video,
        title=match.guess(name_of(path))[0],
        minutes=runtime(video),
        state="doubt",
        detail="сцена: избери студио",
        checked_at=time.time(),
    )
    event(path, "doubt", "сцена", os.path.basename(path))


def identify(item):
    """Matches a new download: a link or id tag pins it, otherwise ThePornDB (and TMDB for candidates) is searched."""
    path = item["path"]
    video = main_video(path)
    if not video:
        update(item["id"], state="failed", detail="няма видео", checked_at=time.time())
        return
    title, year = match.guess(name_of(path))
    minutes = runtime(video)
    keywords = json.loads(item["keywords"])
    update(item["id"], video=video, title=title, year=year, minutes=minutes, checked_at=time.time())
    pin = next((p for p in (match.pinned(k) for k in [os.path.basename(path), *keywords]) if p), None)
    if pin:
        place(item["id"], pin, None)
        return
    found, why, candidates = match.search_tpdb(title, year, minutes, [k for k in keywords if not match.pinned(k)])
    if not candidates:
        candidates = match.search_tmdb(title, year)
        why = why if not candidates else "има го само в TMDB"
    if found:
        site = (found.get("site") or {}).get("name") or ""
        studio = match.studio_folder(site, studios(library_locations()), MERGE_STUDIOS)
        if studio:
            place(item["id"], ("tpdb", found["id"]), studio)
            return
        why = f"студиото {site} още няма папка"
        candidates = [match.candidate(found, 1.0, round(int(found.get("duration") or 0) / 60))] + [
            c for c in candidates if c["id"] != found["id"]
        ]
    update(item["id"], state="doubt", detail=why or "", candidates=candidates)
    event(path, "doubt", why or "", os.path.basename(path))


def target_location(video, locations):
    """The library folder on the video's disk, so the move is a rename."""
    device = os.stat(video).st_dev
    return next((loc for loc in locations if os.path.isdir(loc) and os.stat(loc).st_dev == device), None)


def make_dir(path, like):
    os.mkdir(path)
    parent = os.stat(like)
    os.chown(path, parent.st_uid, parent.st_gid)
    os.chmod(path, parent.st_mode & 0o7777)


def place(row_id, pin, studio, title=None, at=0.25):
    """Makes the movie's folder in the library and moves the video in. pin: ("tpdb", ref) or ("tmdb", id), or None
    with the title of a movie no database knows (and the moment of its video for the poster); studio: its folder,
    None to take it from ThePornDB."""
    item = row(row_id)
    video = item["video"] or main_video(item["path"]) if item else None
    if not item or not video:
        return
    label = os.path.basename(item["path"])
    locations = library_locations()
    location = target_location(video, locations)
    if not location:
        update(row_id, state="failed", detail="библиотеката няма папка на диска, на който е свалено")
        return
    found, date, tag = None, "", None
    try:
        if pin and pin[0] == "tpdb":
            found = tpdb.movie(pin[1])
            title, date, tag = found["title"].strip(), found.get("date") or "", f"tpdbid-{found['id']}"
            if not studio:
                site = (found.get("site") or {}).get("name") or ""
                studio = match.studio_folder(site, studios(locations), MERGE_STUDIOS) or site
        elif pin:
            found = metadata.tmdb_details(pin[1])
            title, date = found.get("title") or found["original_title"], found.get("release_date") or ""
            tag = f"tmdbid-{found['id']}"
    except (tpdb.NotFound, tmdb.NotFound):
        update(row_id, state="doubt", detail=f"няма такъв филм: {pin[1] if pin else ''}")
        return
    if not title:
        update(row_id, state="doubt", detail="няма заглавие")
        return
    if not studio:
        update(row_id, state="doubt", detail="не е избрано студио")
        return
    if tag:
        existing = tagged(locations, tag)
        if existing:
            update(row_id, state="duplicate", detail=f"вече е в библиотеката: {os.path.relpath(existing, location)}")
            event(item["path"], "duplicate", os.path.relpath(existing, location), label)
            return
    studio_dir = os.path.join(location, clean(studio))
    year = date[:4]
    name = clean(title) + (f" ({year})" if year else "") + (f" [{tag}]" if tag else "")
    folder = os.path.join(studio_dir, name)
    if os.path.exists(folder):
        update(row_id, state="duplicate", detail=f"вече е в библиотеката: {os.path.relpath(folder, location)}")
        return
    if not os.path.isdir(studio_dir):
        make_dir(studio_dir, location)
    make_dir(folder, studio_dir)
    moved = os.path.join(folder, clean(title) + os.path.splitext(video)[1].lower())
    try:
        os.rename(video, moved)
        if pin and pin[0] == "tpdb":
            metadata.write_movie(folder, found, os.path.basename(studio_dir))
        elif pin:
            metadata.write_tmdb_movie(folder, found, os.path.basename(studio_dir))
        else:
            metadata.write_unknown(moved, title, os.path.basename(studio_dir), at)
        metadata.own(moved, folder)
    except (OSError, ET.ParseError, tpdb.NotFound, tmdb.NotFound, subprocess.SubprocessError) as e:
        # Back where it was, so nothing half made stays in the library
        if os.path.exists(moved):
            os.rename(moved, video)
        shutil.rmtree(folder, ignore_errors=True)
        update(row_id, state="failed", detail=repr(e))
        log(f"error {label}: {e!r}")
        return
    if os.path.isdir(item["path"]):
        rest = [
            os.path.join(r, f)
            for r, _, files in os.walk(item["path"])
            for f in files
            if os.path.splitext(f)[1].lower() in VIDEO_EXTS and os.path.getsize(os.path.join(r, f)) > LEFTOVER_VIDEO
        ]
        if not rest:
            shutil.rmtree(item["path"])
    jellyfin("/Library/Media/Updated", "POST", {"Updates": [{"Path": folder, "UpdateType": "Created"}]})
    update(row_id, state="done", placed=folder, detail="", candidates=[])
    people.catalog_wake.set()
    event(folder, "placed", f"{label} → {os.path.relpath(folder, location)}", label)


def scene_videos(path):
    """A download's scenes: each video too big to be a sample, or its one video."""
    if os.path.isfile(path):
        return [path] if main_video(path) else []
    big = sorted(
        os.path.join(r, f)
        for r, _, files in os.walk(path)
        for f in files
        if os.path.splitext(f)[1].lower() in VIDEO_EXTS and os.path.getsize(os.path.join(r, f)) > LEFTOVER_VIDEO
    )
    return big or [v for v in [main_video(path)] if v]


def place_scene(row_id, studio, title="", at=0.25):
    """Moves a download's scenes under their studio in „Порно - сцени“, each with its NFO and poster. title: the
    scene's when the download is one; otherwise, or when empty, each is named after its file."""
    item = row(row_id)
    videos = scene_videos(item["path"]) if item else []
    if not item or not videos:
        return
    label = os.path.basename(item["path"])
    locations = library_locations(SCENES_LIBRARY)
    location = target_location(videos[0], locations)
    if not location:
        update(row_id, state="failed", detail="сцените нямат папка на диска, на който е свалено")
        return
    studio_dir = os.path.join(location, clean(studio))
    if len(videos) == 1 and clean(title):
        titles = [clean(title)]
    else:
        titles = [clean(match.guess(os.path.splitext(os.path.basename(v))[0])[0]) for v in videos]
    targets = [
        os.path.join(studio_dir, t + os.path.splitext(v)[1].lower()) for t, v in zip(titles, videos, strict=True)
    ]
    taken = [t for t in targets if os.path.exists(t)]
    if taken or len(set(targets)) < len(targets):
        where = os.path.relpath(taken[0], location) if taken else "две сцени с едно име"
        update(row_id, state="duplicate", detail=f"вече е в библиотеката: {where}")
        event(item["path"], "duplicate", where, label)
        return
    if not os.path.isdir(studio_dir):
        make_dir(studio_dir, location)
    moved = []
    try:
        for video, target, scene_title in zip(videos, targets, titles, strict=True):
            os.rename(video, target)
            moved.append((video, target))
            metadata.write_unknown(target, scene_title, os.path.basename(studio_dir), at, alone=False)
            metadata.own(target, studio_dir)
    except (OSError, ET.ParseError, subprocess.SubprocessError) as e:
        # Back where they were, without what was written for them
        for video, target in moved:
            os.rename(target, video)
            stem = os.path.splitext(target)[0]
            for extra in (stem + ".nfo", stem + "-poster.jpg"):
                if os.path.exists(extra):
                    os.remove(extra)
        update(row_id, state="failed", detail=repr(e))
        log(f"error {label}: {e!r}")
        return
    if os.path.isdir(item["path"]):
        shutil.rmtree(item["path"])
    jellyfin("/Library/Media/Updated", "POST", {"Updates": [{"Path": t, "UpdateType": "Created"} for t in targets]})
    update(row_id, state="done", placed=studio_dir, detail=f"{len(targets)} сцени", candidates=[])
    people.catalog_wake.set()
    event(studio_dir, "placed", f"{label} → {os.path.relpath(studio_dir, location)} ({len(targets)} сцени)", label)


def pass_once():
    downloads[:] = download_folders()
    if not downloads:
        return
    torrents = []
    if qbittorrent.URL:
        torrents = qbit("torrents/info")
        choose_save_path(CATEGORY)
        choose_save_path(SCENES_CATEGORY, SCENES_SUBDIR)
        take_finished(torrents)
    scan(torrents)
    with lock:
        waiting = [r["id"] for r in db.execute("SELECT id FROM incoming WHERE state = 'new' ORDER BY added_at")]
    for row_id in waiting:
        item = row(row_id)
        if item and item["kind"] == "scene":
            scene_waits(item)
        elif item:
            identify(item)
