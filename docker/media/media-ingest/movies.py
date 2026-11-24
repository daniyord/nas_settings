"""A movie that isn't laid out like the rest (<Title> (<Year>) [imdbid-tt...]/<same name>.<ext>) is looked up in
TMDB and renamed after the match: the folder, the video, the artwork and the subtitles, which also get a language when
they have none. Its NFO and artwork from TMDB are in the folder before Jellyfin sees it at its new path. A match whose
title or year doesn't look like the file's name waits for a confirmation, or for another TMDB title to be picked."""

import os
import time

from boxsets import wake as boxsets_wake
from common.jellyfin import jellyfin
from common.media import ORGANIZED, SETTLE_AFTER, SUBTITLE, VIDEO_EXTS, Conflict, with_language
from common.tmdb import NotFound
from metadata import as_item, clear_leftovers, identify, untitled, write_movie
from names import bg_sort, doubt, foreign_rest, generic_name, id_tag, is_xml, locked, owner, padded, target_name
from state import db, event, lock, log, missing_since, record, status, step


def make_dir(path, like):
    os.mkdir(path)
    parent = os.stat(like)
    os.chown(path, parent.st_uid, parent.st_gid)
    # UGOS grants access to the library folders through ACLs, so their mode reads 000 or 770 from here; every folder
    # in them is 777
    os.chmod(path, 0o777)


def plan(folder, stem, target, own):
    """What happens to each file next to the video: (renames as (old, new)), (deletions).

    own: the video has its own folder, so everything in it belongs to the movie; otherwise only the
    files named after it move along.
    """
    names = sorted(n for n in os.listdir(folder) if os.path.isfile(os.path.join(folder, n)))
    videos = [os.path.splitext(n)[0] for n in names if os.path.splitext(n)[1].lower() in VIDEO_EXTS]
    renames, deletions = [], []
    for name in names:
        path = os.path.join(folder, name)
        base, ext = os.path.splitext(name)
        if ext.lower() in VIDEO_EXTS:
            if base == stem:
                renames.append((name, target + ext))
            continue
        mine = owner(name, videos) == stem
        if not (own or mine):
            continue
        if ext.lower() == ".nfo" and not is_xml(path):
            # Release notes of the download; Jellyfin would try to read them as the movie's NFO
            deletions.append(name)
        elif mine:
            rest = name[len(stem) :]
            if SUBTITLE.fullmatch(rest):
                renames.append((name, target + with_language(path, rest)))
            else:
                renames.append((name, (not own and generic_name(rest)) or target + rest))
        elif SUBTITLE.fullmatch(ext):
            renames.append((name, target + with_language(path, foreign_rest(name))))
    renames = [(old, new) for old, new in renames if old != new or not own]
    news = [new.lower() for _, new in renames]
    if len(set(news)) != len(news):
        raise Conflict(f"две неща биха се казали еднакво: {', '.join(sorted(n for n in news if news.count(n) > 1))}")
    if own:
        moving = {old for old, _ in renames} | set(deletions)
        taken = [new for _, new in renames if new in names and new not in moving]
        if taken:
            raise Conflict(f"вече има {', '.join(taken)}")
    return renames, deletions


def ingest(item, parts, location, label, details):
    """item: Jellyfin's, with TMDB's title, year and ids (metadata.as_item); details: TMDB's."""
    folder, filename = os.path.split(item["Path"])
    stem = os.path.splitext(filename)[0]
    target = target_name(item)
    own = len(parts) == 2
    target_dir = os.path.join(location, target)
    if os.path.exists(target_dir) and target_dir != folder:
        raise Conflict(f"{target}/ вече съществува")
    clear_leftovers(folder, stem, whole=own)
    renames, deletions = plan(folder, stem, target, own)

    if not own:
        make_dir(target_dir, location)
    for name in deletions:
        os.remove(os.path.join(folder, name))
    write_movie(folder if own else target_dir, details, created=not own)
    for old, new in renames:
        os.rename(os.path.join(folder, old), os.path.join(folder if own else target_dir, new))
    if own and folder != target_dir:
        os.rename(folder, target_dir)
    untitled(target_dir, "movie", details)
    # Its collection, if TMDB names one
    boxsets_wake.set()

    new_path = os.path.join(target_dir, target + os.path.splitext(filename)[1])
    renamed_folder = own and folder != target_dir
    updates = [
        {"Path": folder if renamed_folder else item["Path"], "UpdateType": "Deleted"},
        {"Path": new_path if own and not renamed_folder else target_dir, "UpdateType": "Created"},
    ]
    jellyfin("/Library/Media/Updated", "POST", {"Updates": updates})
    changes = [f"{old} → {new}" for old, new in renames if old != filename] + [f"изтрит {name}" for name in deletions]
    moved = f"{'/'.join(parts[:-1] or parts)} → {target}/"
    event(new_path, "moved", moved + (f" ({'; '.join(changes)})" if changes else ""), label)
    return new_path


def set_sort_name(item, admin, label):
    if item.get("SortName") == padded(bg_sort(item["Name"])):
        return
    full = jellyfin(f"/Items/{item['Id']}", userId=admin["Id"])
    sort_name = bg_sort(full["Name"])
    # Never over one set by hand; a name that is the folder's means TMDB had no title in the metadata language
    if full.get("ForcedSortName") or not sort_name or ORGANIZED.fullmatch(full["Name"]):
        return
    full["ForcedSortName"] = sort_name
    jellyfin(f"/Items/{item['Id']}", "POST", full)
    event(item["Path"], "sorted", f"{full['Name']} → {sort_name}", label)


def check(item, library, location, admin):
    """Looks at one movie and ingests it when it's ready; returns when it wants to be looked at again."""
    parts = item["Path"][len(location.rstrip("/")) + 1 :].split("/")
    name = parts[0]
    label = f"{library['Name']}/{name}"
    folder = os.path.dirname(item["Path"])
    stem = os.path.splitext(parts[-1])[0]

    if len(parts) == 2 and ORGANIZED.fullmatch(parts[0]) and (stem == parts[0] or stem.startswith(parts[0] + " - ")):
        with lock:
            waiting = db.execute("SELECT 1 FROM untitled WHERE path = ?", (folder,)).fetchone()
        if waiting:
            detail = "TMDB няма заглавие на български: въведи го или остави оригиналното"
            record(item, library["Name"], name, "review", detail, title=True)
            return float("inf")
        identified = bool(id_tag(item) and item.get("ProductionYear"))
        nfo = any(is_xml(os.path.join(folder, n)) for n in ("movie.nfo", stem + ".nfo"))
        if identified and nfo and ORGANIZED.fullmatch(item["Name"]):
            detail = "Jellyfin го показва с името на папката (TMDB няма заглавие на български): сложи заглавие в Jellyfin и го заключи"
            record(item, library["Name"], name, "review", detail)
            return float("inf")
        with lock:
            status.pop(item["Id"], None)
        # Only once the metadata is in, as the name is the filename's until then
        if identified and nfo:
            set_sort_name(item, admin, label)
        return float("inf")

    if not os.path.isfile(item["Path"]):
        record(item, library["Name"], name, "pending", "Jellyfin още показва стария път")
        missing_since.setdefault(item["Id"], time.time())
        return float("inf")
    settles = os.stat(item["Path"]).st_ctime + SETTLE_AFTER
    if time.time() < settles:
        record(item, library["Name"], name, "pending", "файлът още се копира")
        return settles
    if any(locked(os.path.join(folder, n)) for n in ("movie.nfo", stem + ".nfo")):
        # Entered and locked by hand: a movie TMDB doesn't know stays where it is
        with lock:
            status.pop(item["Id"], None)
        return float("inf")
    names = [parts[0], stem] if len(parts) == 2 else [stem]
    try:
        details = identify("movie", item["Path"], names)
    except NotFound:
        details = None
    if not details:
        detail = "TMDB не го намира: постави линк към филма в TMDB или IMDb"
        record(item, library["Name"], name, "unidentified", detail, choose=True)
        return float("inf")
    item = as_item(item, "movie", details)
    if not item["ProductionYear"]:
        detail = "в TMDB е без година: постави линк към друг филм в TMDB или IMDb"
        record(item, library["Name"], name, "unidentified", detail, choose=True)
        return float("inf")
    target = target_name(item)
    if len(parts) > 2:
        record(item, library["Name"], name, "review", "видеото е в подпапка: подреди го на ръка", target)
        return float("inf")
    if len(parts) == 2 and sum(os.path.splitext(n)[1].lower() in VIDEO_EXTS for n in os.listdir(folder)) > 1:
        record(item, library["Name"], name, "review", "в папката има няколко видеа: подреди ги на ръка", target)
        return float("inf")
    reason = doubt(item, names)
    with lock:
        confirmed = db.execute(
            "SELECT 1 FROM confirmed WHERE path = ? AND tag = ?", (item["Path"], id_tag(item))
        ).fetchone()
    if reason and not confirmed:
        detail = f"{reason}: провери в TMDB и потвърди, или постави линк към друг филм"
        record(item, library["Name"], name, "review", detail, target, confirm=True, choose=True)
        return float("inf")
    step(f"{label}: преименува на {target}")
    try:
        ingest(item, parts, location, label, details)
    except (Conflict, OSError, NotFound) as e:
        log(f"error {label}: {e}")
        record(item, library["Name"], name, "review", str(e), target)
        return float("inf")
    record(item, library["Name"], name, "pending", "преименуван, чака Jellyfin да види новата папка", target)
    return time.time() + 60
