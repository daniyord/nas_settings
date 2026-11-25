"""Every actor our NFOs name (PLAN_media_services.md, stage 3): a film's or series' first actors in TMDB (MAX_CAST)
and the guest stars of every episode, each with its TMDB id and photo, so a Jellyfin started empty has the same
people. Walks the libraries' NFOs and adds only the actors missing (metadata.merge_cast); Jellyfin then reads the
changed ones again. Nothing is kept but the NFOs: a pass over titles that have all their actors costs only TMDB's
cached answers. Runs at start, weekly and when the status page asks; the page shows how far it is."""

import os
import tarfile
import threading
import time
import xml.etree.ElementTree as ET

from common import nfo, tmdb
from common.jellyfin import jellyfin, reread
from common.media import VIDEO_EXTS
from config import DATA_DIR, LIBRARIES, SERIES_LIBRARIES
from metadata import animated, cast, details, guest_cast, merge_cast, own
from migrate import tmdb_id, titles
from state import event, log

AGAIN_AFTER = 7 * 86400
BACKUP_DIR = os.path.join(DATA_DIR, "nfo-backup")
MAX_PROBLEMS = 200

wake = threading.Event()
progress = {"running": False, "current": None, "started": None, "finished": None, "libraries": [], "problems": []}


def episode_nfos(folder):
    """(NFO path, season, first episode, last episode or None) of each episode under a series' folder."""
    for root_dir, _, names in os.walk(folder):
        videos = {os.path.splitext(n)[0] for n in names if os.path.splitext(n)[1].lower() in VIDEO_EXTS}
        for name in sorted(names):
            base, ext = os.path.splitext(name)
            if ext.lower() != ".nfo" or base not in videos:
                continue
            path = os.path.join(root_dir, name)
            try:
                root = nfo.read(path)
            except (ET.ParseError, UnicodeDecodeError):
                continue
            if root is None or root.tag != "episodedetails":
                continue
            season, first, last = (
                (root.findtext(tag) or "").strip() for tag in ("season", "episode", "episodenumberend")
            )
            if season.isdigit() and first.isdigit():
                yield path, int(season), int(first), int(last) if last.isdigit() and last != first else None


def backup(library, locations):
    """A tar.gz of the library's NFOs before the first change to them; written under another name first, so one
    cut short (a restart) is never taken for a backup."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR, f"cast-{library}-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz")
    with tarfile.open(path + ".part", "w:gz") as tar:
        for location in locations:
            for root_dir, _, names in os.walk(location):
                for name in names:
                    if name.lower().endswith(".nfo"):
                        tar.add(os.path.join(root_dir, name))
    os.replace(path + ".part", path)
    return path


def problem(path, detail):
    log(f"error cast {path}: {detail}")
    progress["problems"] = (progress["problems"] + [{"path": path, "detail": detail}])[-MAX_PROBLEMS:]


def write(path, root, before, folder):
    if nfo.write(path, root, before):
        own(path, folder)
        return True
    return False


def run_title(kind, folder, nfo_file, item, row, admin, backed_up):
    root = nfo.read(nfo_file)
    found = tmdb_id(kind, folder, root)
    if found is None and root is not None:
        # The folder's IMDb id may be one TMDB doesn't know, while the NFO has its TMDB id
        found = tmdb_id(kind, "", root)
    if root is None or found is None:
        row["skipped"] += 1
        row["episodes_done"] += sum(1 for _ in episode_nfos(folder)) if kind == "tv" else 0
        return
    d = details(kind, found)
    before = nfo.serialize(root)
    added = merge_cast(root, d, cast(kind, d))
    if added:
        backed_up()
        if write(nfo_file, root, before, folder):
            row["changed"] += 1
            row["actors"] += added
            if item:
                reread(item["Id"], admin)
    if kind != "tv":
        return
    episodes = list(episode_nfos(folder))
    if animated(d):
        row["episodes_done"] += len(episodes)
        return
    ids = {}
    if item:
        found_items = jellyfin(
            "/Items", ParentId=item["Id"], Recursive="true", IncludeItemTypes="Episode", Fields="Path", Limit=100000
        )["Items"]
        ids = {os.path.splitext(e.get("Path") or "")[0]: e["Id"] for e in found_items}
    for path, season, first, last in episodes:
        progress["current"] = os.path.relpath(path, os.path.dirname(folder))
        try:
            episode_root = nfo.read(path)
            if episode_root is not None:
                episode_before = nfo.serialize(episode_root)
                episode_added = merge_cast(episode_root, d, guest_cast(d, season, first, last), "GuestStar")
                if episode_added:
                    backed_up()
                    if write(path, episode_root, episode_before, os.path.dirname(path)):
                        row["episodes_changed"] += 1
                        row["actors"] += episode_added
                        episode_id = ids.get(os.path.splitext(path)[0])
                        if episode_id:
                            reread(episode_id, admin)
        except (OSError, ET.ParseError, tmdb.NotFound) as e:
            problem(path, repr(e))
        row["episodes_done"] += 1


def run_library(library, folder_info, items, row, admin):
    saved = []

    def backed_up():
        if not saved:
            saved.append(backup(library, folder_info["Locations"]))

    for number, (kind, folder, nfo_file, item) in enumerate(items, 1):
        progress["current"] = f"{library}/{os.path.basename(folder)}"
        before = dict(row)
        try:
            run_title(kind, folder, nfo_file, item, row, admin, backed_up)
        except (OSError, ET.ParseError, tmdb.NotFound) as e:
            problem(folder, repr(e))
            row["errors"] += 1
        row["titles_done"] += 1
        # A line in the page's log for every title passed
        added, episodes = row["actors"] - before["actors"], row["episodes_changed"] - before["episodes_changed"]
        if row["errors"] > before["errors"]:
            result = "грешка"
        elif row["skipped"] > before["skipped"]:
            result = "без TMDB id, пропуснат"
        elif added:
            result = f"добавени {added} актьора" + (f" (в {episodes} епизода)" if episodes else "")
        else:
            result = "всички актьори са вече в NFO"
        event(folder, "cast", f"{number} от {len(items)}: {result}", library)


def run():
    folders = {f["Name"]: f for f in jellyfin("/Library/VirtualFolders")}
    admin = next(u for u in jellyfin("/Users") if u["Policy"]["IsAdministrator"])["Id"]
    rows = []
    for library in LIBRARIES + SERIES_LIBRARIES:
        if library not in folders:
            log(f"error cast: library {library} not found in Jellyfin")
            continue
        rows.append(
            {
                "name": library,
                "titles": None,
                "titles_done": 0,
                "episodes": None,
                "episodes_done": 0,
                "changed": 0,
                "episodes_changed": 0,
                "actors": 0,
                "skipped": 0,
                "errors": 0,
            }
        )
    progress.update(libraries=rows, problems=[], started=time.time(), finished=None)
    # Every library's size first, so the page shows how far the whole pass is
    items = {}
    for row in rows:
        progress["current"] = f"{row['name']}: преброяване"
        items[row["name"]] = list(titles(row["name"], folders[row["name"]]))
        row["titles"] = len(items[row["name"]])
        row["episodes"] = sum(
            1 for kind, folder, _, _ in items[row["name"]] if kind == "tv" for _ in episode_nfos(folder)
        )
    for row in rows:
        run_library(row["name"], folders[row["name"]], items[row["name"]], row, admin)
    progress["finished"] = time.time()


def cast_loop():
    while True:
        wake.clear()
        progress["running"] = True
        try:
            run()
        except Exception as e:
            log(f"error cast: {e!r}")
        finally:
            progress.update(running=False, current=None)
        wake.wait(AGAIN_AFTER)
