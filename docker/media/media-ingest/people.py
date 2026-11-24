"""The actors' photos: one per actor in PEOPLE_DIR, <tmdb-id>.jpg, which our NFOs point to. The NFOs are the list of
who is needed (each actor carries its TMDB id), so a lost folder is rebuilt from them; photos no NFO points to go.
Names entered on the status page for actors TMDB has only in Latin are kept there too (names.json) and put into
every NFO that names those actors."""

import os
import re
import threading
import time
import xml.etree.ElementTree as ET

from common import nfo, tmdb
from common.jellyfin import jellyfin, library_locations, reread
from metadata import confirmed_names
from config import LIBRARIES, PEOPLE_DIR, SERIES_LIBRARIES
from state import db, event, lock, log

WEEK = 7 * 86400
ACTOR = re.compile(rb"<actor>(.*?)</actor>", re.S)
TMDB_ID = re.compile(rb"<tmdbid>\s*(\d+)\s*</tmdbid>")

wake = threading.Event()
# What the status page shows: how many actors the NFOs name, how many have a photo, how many TMDB has none for
# and how many it doesn't know
people = {"running": False, "finished": None, "actors": None, "photos": None, "no_photo": None, "unknown": None}


def actor_ids(locations):
    """TMDB ids of every actor in the NFOs under the folders."""
    ids = set()
    for location in locations:
        for root, _, names in os.walk(location):
            for name in names:
                if not name.lower().endswith(".nfo"):
                    continue
                try:
                    with open(os.path.join(root, name), "rb") as f:
                        data = f.read()
                except OSError:
                    continue
                for actor in ACTOR.findall(data):
                    m = TMDB_ID.search(actor)
                    if m:
                        ids.add(int(m.group(1)))
    return ids


def update_photos(ids, people_dir):
    """Downloads the missing photos and deletes those of no one in `ids`; returns (photos, no_photo, unknown)."""
    have = {int(n[:-4]) for n in os.listdir(people_dir) if re.fullmatch(r"\d+\.jpg", n)}
    no_photo = unknown = 0
    for tmdb_id in sorted(ids - have):
        try:
            profile = tmdb.person(tmdb_id).get("profile_path")
        except tmdb.NotFound:
            unknown += 1
            continue
        if not profile:
            no_photo += 1
            continue
        tmdb.download_image(profile, os.path.join(people_dir, f"{tmdb_id}.jpg"), "h632")
        have.add(tmdb_id)
    for tmdb_id in have - ids:
        os.remove(os.path.join(people_dir, f"{tmdb_id}.jpg"))
        have.discard(tmdb_id)
    return len(have), no_photo, unknown


def run():
    folders = {f["Name"]: f for f in jellyfin("/Library/VirtualFolders")}
    locations = library_locations(folders, LIBRARIES + SERIES_LIBRARIES)
    missing = [loc for loc in locations if not os.path.isdir(loc)]
    if missing:
        # Without every library the list is short, and the photos of the missing ones would be deleted
        raise OSError(f"no library folder {', '.join(missing)}")
    ids = actor_ids(locations)
    photos, no_photo, unknown = update_photos(ids, PEOPLE_DIR)
    people.update(finished=time.time(), actors=len(ids), photos=photos, no_photo=no_photo, unknown=unknown)
    detail = f"{len(ids)} актьора, {photos} снимки, {no_photo} без снимка в TMDB, {unknown} непознати"
    event(PEOPLE_DIR, "people", detail, "Актьори")


def last_finished():
    with lock:
        row = db.execute("SELECT MAX(at) FROM events WHERE kind = 'people'").fetchone()
    return row[0] if row and row[0] else None


def people_loop():
    """Weekly, at once when the folder is empty, and when the status page asks."""
    while True:
        last = last_finished() or 0
        # An empty folder is filled at once, but not over and over while no NFO names an actor yet
        empty = os.path.isdir(PEOPLE_DIR) and not os.listdir(PEOPLE_DIR) and time.time() - last > 3600
        if not wake.is_set() and not empty and time.time() < last + WEEK:
            wake.wait(max(60, min(3600, last + WEEK - time.time())))
            continue
        wake.clear()
        if not os.path.isdir(PEOPLE_DIR):
            log(f"error people: {PEOPLE_DIR} is not mounted")
            wake.wait(WEEK)
            continue
        people["running"] = True
        try:
            run()
            catalog_wake.set()
        except Exception as e:
            log(f"error people: {e!r}")
            people["running"] = False
            # Not again right away when the folder is empty
            wake.wait(3600)
        finally:
            people["running"] = False


def rename_actors(path, names):
    """Gives the actors of one NFO with a confirmed name (by TMDB id) that name; True if the NFO changed."""
    try:
        root = nfo.read(path)
    # A release's own .nfo: text art in an old code page, or not XML
    except (ET.ParseError, UnicodeDecodeError):
        return False
    if root is None:
        return False
    before = nfo.serialize(root)
    for actor in root.findall("actor"):
        tmdb_id = (actor.findtext("tmdbid") or "").strip()
        name = actor.find("name")
        if tmdb_id.isdigit() and int(tmdb_id) in names and name is not None and name.text != names[int(tmdb_id)]:
            name.text = names[int(tmdb_id)]
    return nfo.write(path, root, before)


def apply_names():
    """Puts the names confirmed on the status page into every NFO that names those actors, and has Jellyfin read
    the changed ones again."""
    names = confirmed_names()
    folders = {f["Name"]: f for f in jellyfin("/Library/VirtualFolders")}
    changed = set()
    for name in LIBRARIES + SERIES_LIBRARIES:
        if name not in folders:
            continue
        for location in folders[name]["Locations"]:
            for root, _, files in os.walk(location):
                for file in files:
                    if file.lower().endswith(".nfo") and rename_actors(os.path.join(root, file), names):
                        changed.add(root)
        series = name in SERIES_LIBRARIES
        items = jellyfin(
            "/Items",
            ParentId=folders[name]["ItemId"],
            Recursive="true",
            IncludeItemTypes="Series" if series else "Movie",
            Fields="Path",
            Limit=100000,
        )["Items"]
        admin = next(u for u in jellyfin("/Users") if u["Policy"]["IsAdministrator"])
        for item in items:
            path = (item.get("Path") or "").rstrip("/")
            if (path if series else os.path.dirname(path)) in changed:
                reread(item["Id"], admin["Id"])
    if changed:
        event(PEOPLE_DIR, "people", f"имена на актьори в {len(changed)} папки", "Актьори")


names_wake = threading.Event()


def names_loop():
    """Applies confirmed names once the user has stopped entering them for a while."""
    while True:
        names_wake.wait()
        while names_wake.wait(30):
            names_wake.clear()
        try:
            apply_names()
            catalog_wake.set()
        except Exception as e:
            log(f"error people names: {e!r}")


# Who plays in which movie or series, for the actors page: from the movies' and series' own NFOs (not the episodes'),
# kept in memory and built again after the weekly run and after names change
catalog = {"actors": None, "built": None, "building": False}
catalog_wake = threading.Event()
catalog_wake.set()


def build_catalog():
    """{tmdb id: {"id", "name", "titles": [{"title", "year", "library", "item", "role"}]}}."""
    folders = {f["Name"]: f for f in jellyfin("/Library/VirtualFolders")}
    actors = {}
    for library in LIBRARIES + SERIES_LIBRARIES:
        if library not in folders:
            continue
        series = library in SERIES_LIBRARIES
        items = jellyfin(
            "/Items",
            ParentId=folders[library]["ItemId"],
            Recursive="true",
            IncludeItemTypes="Series" if series else "Movie",
            Fields="Path",
            Limit=100000,
        )["Items"]
        by_folder = {}
        for item in items:
            path = (item.get("Path") or "").rstrip("/")
            by_folder[path if series else os.path.dirname(path)] = item["Id"]
        for location in folders[library]["Locations"]:
            for root, _, files in os.walk(location):
                for file in files:
                    if (file != "tvshow.nfo") if series else not file.lower().endswith(".nfo"):
                        continue
                    try:
                        tree = nfo.read(os.path.join(root, file))
                    except (ET.ParseError, OSError, UnicodeDecodeError):
                        continue
                    if tree is None or tree.tag not in ("movie", "tvshow"):
                        continue
                    title = {
                        "title": (tree.findtext("title") or "").strip() or os.path.basename(root),
                        "year": (tree.findtext("year") or "").strip(),
                        "library": library,
                        "item": by_folder.get(root),
                    }
                    for actor in tree.findall("actor"):
                        tmdb_id = (actor.findtext("tmdbid") or "").strip()
                        if not tmdb_id.isdigit():
                            continue
                        entry = actors.setdefault(
                            int(tmdb_id),
                            {"id": int(tmdb_id), "name": (actor.findtext("name") or "").strip(), "titles": []},
                        )
                        # A movie folder may have both movie.nfo and <video>.nfo
                        if any(t["item"] == title["item"] and t["title"] == title["title"] for t in entry["titles"]):
                            continue
                        entry["titles"].append({**title, "role": (actor.findtext("role") or "").strip()})
    return actors


def catalog_loop():
    while True:
        catalog_wake.wait()
        catalog_wake.clear()
        catalog["building"] = True
        try:
            catalog.update(actors=build_catalog(), built=time.time())
        except Exception as e:
            log(f"error people catalog: {e!r}")
        finally:
            catalog["building"] = False
