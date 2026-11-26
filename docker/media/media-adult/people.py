"""The performers' photos: one per performer in PEOPLE_DIR, tpdb-<id>.jpg, which our NFOs point to, as media-ingest's
people.py keeps the TMDB actors' <id>.jpg in the same folder. The NFOs are the list of who is needed (each performer
carries its ThePornDB id), so a lost folder is rebuilt from them; photos no NFO points to go. Weekly, at start and
from the status page. The catalog of the performers page is built from the same NFOs."""

import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from typing import Any

from common import nfo, tpdb
from common.jellyfin import jellyfin, library_locations
from common.media import VIDEO_EXTS
from config import LIBRARY, PEOPLE_DIR, SCENES_LIBRARY
from state import event, log

WEEK = 7 * 86400
ACTOR = re.compile(rb"<actor>(.*?)</actor>", re.S)
TPDB_ID = re.compile(rb"<tpdbid>\s*([\w-]+)\s*</tpdbid>")
PHOTO = re.compile(r"tpdb-([\w-]+)\.jpg")

# What the status page shows
people: dict[str, Any] = {"finished": None, "performers": None, "photos": None, "no_photo": None, "missing": []}
people_wake = threading.Event()
# Every performer of the NFOs with their titles, and the titles with no performer; rebuilt when something changed
catalog: dict[str, Any] = {"performers": None, "empty": [], "built": None, "building": False}
catalog_wake = threading.Event()
catalog_wake.set()
# A performer's id on the page: tpdb-<id>, or the TMDB id of one from a movie TMDB knows; the photo is <id>.jpg
PERFORMER_ID = re.compile(r"tpdb-[\w-]+|\d+")


def performer_ids(locations):
    """ThePornDB ids of every performer in the NFOs under the folders."""
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
                    m = TPDB_ID.search(actor)
                    if m:
                        ids.add(m.group(1).decode())
    return ids


def update_photos(ids, people_dir):
    """Downloads the missing photos and deletes those of no one in `ids`; returns (photos, ids without one)."""
    have = {m.group(1) for n in os.listdir(people_dir) if (m := PHOTO.fullmatch(n))}
    no_photo = []
    for performer_id in sorted(ids - have):
        try:
            image = tpdb.performer(performer_id).get("image")
        except tpdb.NotFound:
            image = None
        if not image:
            no_photo.append(performer_id)
            continue
        tpdb.download(image, os.path.join(people_dir, f"tpdb-{performer_id}.jpg"))
        have.add(performer_id)
    for performer_id in have - ids:
        os.remove(os.path.join(people_dir, f"tpdb-{performer_id}.jpg"))
        have.discard(performer_id)
    return len(have), no_photo


def run():
    folders = {f["Name"]: f for f in jellyfin("/Library/VirtualFolders")}
    locations = library_locations(folders, [LIBRARY, SCENES_LIBRARY])
    missing = [loc for loc in locations if not os.path.isdir(loc)]
    if missing or not locations or not os.path.isdir(PEOPLE_DIR):
        # Without every library the list is short, and the photos of the missing ones would be deleted
        raise OSError(f"no folder {', '.join(missing) or PEOPLE_DIR}")
    ids = performer_ids(locations)
    photos, missing = update_photos(ids, PEOPLE_DIR)
    people.update(
        finished=time.time(), performers=len(ids), photos=photos, no_photo=len(missing), missing=sorted(missing)
    )
    event(PEOPLE_DIR, "people", f"{len(ids)} изпълнители, {photos} снимки, {len(missing)} без снимка", "Изпълнители")


def people_loop():
    while True:
        people_wake.clear()
        try:
            run()
        except Exception as e:
            log(f"error people: {e!r}")
        catalog_wake.set()
        people_wake.wait(WEEK)


def performer_key(actor):
    """The page's id of an <actor>: by its ThePornDB id, else its TMDB id; None for a name alone."""
    tpdb_id = (actor.findtext("tpdbid") or "").strip()
    if tpdb_id:
        return f"tpdb-{tpdb_id}"
    tmdb_id = (actor.findtext("tmdbid") or "").strip()
    return tmdb_id if tmdb_id.isdigit() else None


def build_catalog():
    """({id: {"id", "name", "titles": [{"title", "year", "studio", "library", "item"}]}}, [titles with no performer])."""
    folders = {f["Name"]: f for f in jellyfin("/Library/VirtualFolders")}
    performers, empty = {}, []
    for library in (LIBRARY, SCENES_LIBRARY):
        if library not in folders:
            continue
        scenes = library == SCENES_LIBRARY
        items = jellyfin(
            "/Items",
            ParentId=folders[library]["ItemId"],
            Recursive="true",
            IncludeItemTypes="Video,Movie",
            Fields="Path",
            Limit=100000,
        )["Items"]
        # A movie by its folder; a scene by its file without the extension, as its NFO is named
        by_nfo = {}
        for item in items:
            path = item.get("Path") or ""
            by_nfo[os.path.splitext(path)[0] if scenes else os.path.dirname(path)] = item["Id"]
        for location in folders[library]["Locations"]:
            for root, _, files in os.walk(location):
                videos = {os.path.splitext(f)[0] for f in files if os.path.splitext(f)[1].lower() in VIDEO_EXTS}
                for file in files:
                    stem = os.path.splitext(file)[0]
                    if not file.lower().endswith(".nfo") or (scenes and stem not in videos):
                        continue
                    if not scenes and file != "movie.nfo" and "movie.nfo" in files:
                        continue
                    try:
                        tree = nfo.read(os.path.join(root, file))
                    except (ET.ParseError, OSError, UnicodeDecodeError):
                        continue
                    if tree is None:
                        continue
                    title = {
                        "title": (tree.findtext("title") or "").strip() or stem,
                        "year": (tree.findtext("year") or "").strip(),
                        "studio": (tree.findtext("studio") or "").strip(),
                        "library": library,
                        "item": by_nfo.get(os.path.join(root, stem) if scenes else root),
                    }
                    keyed = [(performer_key(a), a) for a in tree.findall("actor")]
                    keyed = [(k, a) for k, a in keyed if k]
                    if not keyed and not scenes:
                        empty.append(title)
                    for key, actor in keyed:
                        entry = performers.setdefault(
                            key, {"id": key, "name": (actor.findtext("name") or "").strip(), "titles": []}
                        )
                        if title not in entry["titles"]:
                            entry["titles"].append(title)
    return performers, sorted(empty, key=lambda t: (t["studio"], t["title"]))


def catalog_loop():
    while True:
        catalog_wake.wait(3600)
        catalog_wake.clear()
        catalog["building"] = True
        try:
            performers, empty = build_catalog()
            catalog.update(performers=performers, empty=empty, built=time.time())
        except Exception as e:
            log(f"error performers catalog: {e!r}")
        finally:
            catalog["building"] = False
