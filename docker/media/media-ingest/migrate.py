"""Stage 3 (PLAN_media_services.md): the folders organized before our NFOs are brought to them. What their NFO has
stays (titles, sort names, everything corrected by hand); what is missing comes from TMDB (ids, the actors' TMDB ids
and photos, downloaded before the NFO is written, the collection, ...); the NFO is locked, and missing artwork is added. A title or plot Jellyfin shows
differently from the NFO is reported rather than chosen: it may be a correction made in Jellyfin after it stopped
writing NFOs (2026-11-24), or one made in the NFO that Jellyfin hasn't read. Actors' names become the Latin ones where the NFO has
TMDB's Bulgarian spelling, so one actor is one person. Every NFO of a library is saved in a tar.gz before the first
change to it."""

import os
import re
import tarfile
import threading
import time
import xml.etree.ElementTree as ET

from common import nfo, tmdb
from common.jellyfin import jellyfin
from common.media import ORGANIZED, VIDEO_EXTS
from config import DATA_DIR, MIGRATE_LIBRARIES, SERIES_LIBRARIES
from common.jellyfin import reread
from config import MAX_CAST
from metadata import (
    CYRILLIC_LANGUAGES,
    ID_IN_NAME,
    actor_name,
    animated,
    art,
    details,
    drop_cast,
    local_names,
    movie_nfo,
    own,
    series_nfo,
    write_season_posters,
)
from state import db, event, lock, log

BACKUP_DIR = os.path.join(DATA_DIR, "nfo-backup")
AGAIN_AFTER = 6 * 3600
SEASON = re.compile(r"(?:season|сезон|s)[ ._-]?0*(\d+)|specials", re.I)

db.executescript(
    """
-- Folders brought to our NFOs: done, different (TMDB's title differs from the one kept), unidentified, error
CREATE TABLE IF NOT EXISTS migrated (
    path TEXT PRIMARY KEY,
    library TEXT NOT NULL,
    at REAL NOT NULL,
    result TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""
)
wake = threading.Event()
progress = {"running": False, "current": None}


def backup(library, locations):
    """A tar.gz of every NFO under the library's folders, as the paths are."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR, f"{library}-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz")
    count = 0
    with tarfile.open(path + ".part", "w:gz") as tar:
        for location in locations:
            for root, _, names in os.walk(location):
                for name in names:
                    if name.lower().endswith(".nfo"):
                        tar.add(os.path.join(root, name))
                        count += 1
    os.replace(path + ".part", path)
    return path, count


def tmdb_id(kind, folder, root):
    """The title's TMDB id: from the folder's [imdbid-…]/[tmdbid-…], else from the NFO; None if there is none."""
    m = ID_IN_NAME.search(os.path.basename(folder))
    if m and m.group(2):
        return int(m.group(2))
    if m:
        found = tmdb.find_imdb(m.group(1))
        return found[1] if found and found[0] == kind else None
    if root is not None:
        for e in root.findall("uniqueid"):
            if (e.get("type") or "").lower() == "tmdb" and (e.text or "").strip().isdigit():
                return int(e.text.strip())
        if (root.findtext("tmdbid") or "").strip().isdigit():
            return int(root.findtext("tmdbid").strip())
    return None


def mismatches(root, item):
    """What Jellyfin shows differently from the NFO: title and plot."""
    found = []
    if item:
        for tag, value, what in (("title", item.get("Name"), "заглавие"), ("plot", item.get("Overview"), "описание")):
            kept = (root.findtext(tag) or "").strip()
            if value and kept and kept != value.strip():
                found.append(f"{what} в Jellyfin „{value.strip()[:60]}“, в NFO „{kept[:60]}“")
    return found


def native_names(root, kind, d):
    """Renames the NFO's actors to the one name our NFOs give each person (metadata.actor_name), whichever of TMDB's
    spellings (Bulgarian or Latin) the NFO has."""
    local = local_names(kind, d)
    latin = {c["id"]: c["name"] for c in tmdb.credits(kind, d["id"]).get("cast", [])}
    cyrillic_title = d.get("original_language") in CYRILLIC_LANGUAGES
    title = d.get("title") or d.get("name")
    renames = {}
    for person_id, name in list(latin.items())[: MAX_CAST or None]:
        chosen = actor_name(person_id, name, local.get(person_id), cyrillic_title, title)
        for spelling in (name, local.get(person_id)):
            if spelling and spelling != chosen:
                renames[spelling.casefold()] = chosen
    for actor in root.findall("actor"):
        name = actor.find("name")
        if name is not None and (name.text or "").strip().casefold() in renames:
            name.text = renames[name.text.strip().casefold()]


def migrate_title(kind, folder, nfo_file, item):
    """Brings one title's NFO and artwork to ours; returns (result, detail, whether the NFO changed)."""
    root = nfo.read(nfo_file)
    before = None if root is None else nfo.serialize(root)
    if root is None:
        root = ET.Element("movie" if kind == "movie" else "tvshow")
    notes = mismatches(root, item)
    found = tmdb_id(kind, folder, root)
    d = None
    if found:
        try:
            d = details(kind, found)
        except tmdb.NotFound:
            d = None
    if d is None:
        changed = nfo.write(nfo_file, nfo.merge(root, []), before)
        if changed:
            own(nfo_file, folder)
        detail = "; ".join(["няма го в TMDB по ID-то в името или в NFO; само заключен", *notes])
        return "unidentified", detail, changed
    cartoon = animated(d)
    if cartoon:
        drop_cast(root)
    else:
        native_names(root, kind, d)
    # The photos before the NFO: a person Jellyfin makes from it takes its photo then or never
    ours = movie_nfo(d) if kind == "movie" else series_nfo(d)
    nfo.merge(root, ours, actor_replace={"thumb"})
    changed = nfo.write(nfo_file, root, before)
    if changed:
        own(nfo_file, folder)
    added = art(d, folder, missing_only=True)
    if kind == "tv":
        seasons = set()
        for name in os.listdir(folder):
            m = SEASON.fullmatch(name)
            if m and os.path.isdir(os.path.join(folder, name)):
                seasons.add(int(m.group(1)) if m.group(1) else 0)
        added += write_season_posters(folder, d, seasons, missing_only=True)
        lock_episodes(folder, cartoon)
    bulgarian, kept = tmdb.bulgarian_title(d), (root.findtext("title") or "").strip()
    if bulgarian and kept and kept != bulgarian:
        notes.append(f"заглавие „{kept}“, в TMDB „{bulgarian}“")
    result = "different" if notes else "done"
    return result, "; ".join(notes + ([f"добавени {', '.join(added)}"] if added else [])), changed


def lock_episodes(folder, no_cast=False):
    """Locks the episodes' NFOs, which have all they need already; no_cast takes their actors out (animation)."""
    for root_dir, _, names in os.walk(folder):
        videos = {os.path.splitext(n)[0] for n in names if os.path.splitext(n)[1].lower() in VIDEO_EXTS}
        for name in names:
            base, ext = os.path.splitext(name)
            if ext.lower() != ".nfo" or base not in videos:
                continue
            path = os.path.join(root_dir, name)
            try:
                root = nfo.read(path)
            except (ET.ParseError, UnicodeDecodeError):
                continue
            if root is not None and root.tag == "episodedetails":
                before = nfo.serialize(root)
                if no_cast:
                    drop_cast(root)
                if nfo.write(path, nfo.merge(root, []), before):
                    own(path, root_dir)


def titles(library, folder_info):
    """(kind, folder, nfo file, Jellyfin item) of every organized title in a library."""
    kind = "tv" if library in SERIES_LIBRARIES else "movie"
    items = jellyfin(
        "/Items",
        ParentId=folder_info["ItemId"],
        Recursive="true",
        IncludeItemTypes="Series" if kind == "tv" else "Movie",
        Fields="Path,Overview",
        Limit=100000,
    )["Items"]
    by_folder = {}
    for item in items:
        path = (item.get("Path") or "").rstrip("/")
        by_folder[path if kind == "tv" else os.path.dirname(path)] = item
    for location in folder_info["Locations"]:
        if not os.path.isdir(location):
            continue
        for name in sorted(os.listdir(location)):
            folder = os.path.join(location, name)
            if not ORGANIZED.fullmatch(name) or not os.path.isdir(folder):
                continue
            item = by_folder.get(folder)
            if kind == "tv":
                nfo_file = os.path.join(folder, "tvshow.nfo")
            else:
                videos = [n for n in os.listdir(folder) if os.path.splitext(n)[1].lower() in VIDEO_EXTS]
                stems = [os.path.splitext(n)[0] for n in sorted(videos)]
                candidates = [os.path.join(folder, "movie.nfo")] + [os.path.join(folder, s + ".nfo") for s in stems]
                nfo_file = next((c for c in candidates if os.path.exists(c)), candidates[0])
            yield kind, folder, nfo_file, item


def run_library(library, folder_info):
    with lock:
        seen = {r[0]: r[1] for r in db.execute("SELECT path, result FROM migrated WHERE library = ?", (library,))}
    if not seen:
        path, count = backup(library, folder_info["Locations"])
        event(path, "migrated", f"запазени {count} NFO преди миграцията", library)
    passed, changed = 0, []
    for kind, folder, nfo_file, item in titles(library, folder_info):
        if seen.get(folder) in ("done", "different", "unidentified"):
            continue
        passed += 1
        progress["current"] = f"{library}/{os.path.basename(folder)}"
        try:
            result, detail, written = migrate_title(kind, folder, nfo_file, item)
            if written and item:
                changed.append(item["Id"])
        except (OSError, ET.ParseError, tmdb.NotFound) as e:
            result, detail = "error", str(e)
            log(f"error migrate {folder}: {e}")
        with lock:
            db.execute(
                "INSERT OR REPLACE INTO migrated VALUES (?, ?, ?, ?, ?)", (folder, library, time.time(), result, detail)
            )
    if changed:
        # A locked item never reads its NFO again on its own (PLAN_media_services.md, 2.1)
        admin = next(u for u in jellyfin("/Users") if u["Policy"]["IsAdministrator"])
        for item_id in changed:
            # A slow answer for one (a series with many episodes) mustn't leave the rest unread
            try:
                reread(item_id, admin["Id"])
            except OSError as e:
                log(f"error migrate reread {item_id}: {e!r}")
    if passed:
        # Jellyfin doesn't read a changed NFO on its own; Default reads only what changed (the episodes'), and the
        # NFOs now locked keep the online providers out
        jellyfin(
            f"/Items/{folder_info['ItemId']}/Refresh",
            "POST",
            Recursive="true",
            MetadataRefreshMode="Default",
            ImageRefreshMode="Default",
            ReplaceAllMetadata="false",
            ReplaceAllImages="false",
        )
        event(library, "migrated", f"минати {passed} папки; Jellyfin ги чете наново", library)


def run():
    folders = {f["Name"]: f for f in jellyfin("/Library/VirtualFolders")}
    for library in MIGRATE_LIBRARIES:
        if library not in folders:
            log(f"error migrate: library {library} not found in Jellyfin")
            continue
        run_library(library, folders[library])


def summary():
    """Per library: how many titles it has been through and with what result, and the ones to look at."""
    with lock:
        rows = db.execute("SELECT path, library, at, result, detail FROM migrated ORDER BY library, path").fetchall()
    libraries = {
        name: {"name": name, "done": 0, "different": 0, "unidentified": 0, "error": 0} for name in MIGRATE_LIBRARIES
    }
    problems = []
    for path, library, at, result, detail in rows:
        libraries.setdefault(library, {"name": library, "done": 0, "different": 0, "unidentified": 0, "error": 0})
        libraries[library][result] += 1
        if result != "done":
            problems.append({"path": path, "library": library, "at": at, "result": result, "detail": detail})
    return {**progress, "libraries": list(libraries.values()), "problems": problems}


def migrate_loop():
    while True:
        wake.clear()
        if MIGRATE_LIBRARIES:
            progress["running"] = True
            try:
                run()
            except Exception as e:
                log(f"error migrate: {e!r}")
            finally:
                progress.update(running=False, current=None)
        wake.wait(AGAIN_AFTER)
