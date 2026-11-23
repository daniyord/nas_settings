"""A movie that isn't laid out like the rest (<Title> (<Year>) [imdbid-tt...]/<same name>.<ext>) is renamed after
the match once Jellyfin has identified it and written its NFO: the folder, the video, the artwork and the subtitles,
which also get a language when they have none. Every identified movie without one gets the Bulgarian ForcedSortName
of the other movies. A match whose title or year doesn't look like the file's name waits for a confirmation."""

import os
import time

from common.jellyfin import jellyfin
from common.media import ORGANIZED, SETTLE_AFTER, SUBTITLE, VIDEO_EXTS, Conflict, with_language
from config import ORGANIZE_AFTER
from names import bg_sort, doubt, foreign_rest, generic_name, id_tag, is_xml, locked, owner, padded, target_name
from state import db, event, lock, log, missing_since, ready_since, record, status, step


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


def ingest(item, parts, location, label):
    folder, filename = os.path.split(item["Path"])
    stem = os.path.splitext(filename)[0]
    target = target_name(item)
    own = len(parts) == 2
    target_dir = os.path.join(location, target)
    if os.path.exists(target_dir) and target_dir != folder:
        raise Conflict(f"{target}/ вече съществува")
    renames, deletions = plan(folder, stem, target, own)

    if not own:
        make_dir(target_dir, location)
    for name in deletions:
        os.remove(os.path.join(folder, name))
    for old, new in renames:
        os.rename(os.path.join(folder, old), os.path.join(folder if own else target_dir, new))
    if own and folder != target_dir:
        os.rename(folder, target_dir)

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
    identified = bool(id_tag(item) and item.get("ProductionYear"))
    nfo = any(is_xml(os.path.join(folder, n)) for n in ("movie.nfo", stem + ".nfo"))

    if len(parts) == 2 and ORGANIZED.fullmatch(parts[0]) and (stem == parts[0] or stem.startswith(parts[0] + " - ")):
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
    if not identified and any(locked(os.path.join(folder, n)) for n in ("movie.nfo", stem + ".nfo")):
        # Entered and locked by hand: a movie TMDB doesn't know stays where it is
        with lock:
            status.pop(item["Id"], None)
        return float("inf")
    if not identified:
        detail = "Jellyfin не е разпознал филма (или няма година): Identify в Jellyfin"
        record(item, library["Name"], name, "unidentified", detail)
        return float("inf")
    target = target_name(item)
    if len(parts) > 2:
        record(item, library["Name"], name, "review", "видеото е в подпапка: подреди го на ръка", target)
        return float("inf")
    if len(parts) == 2 and sum(os.path.splitext(n)[1].lower() in VIDEO_EXTS for n in os.listdir(folder)) > 1:
        record(item, library["Name"], name, "review", "в папката има няколко видеа: подреди ги на ръка", target)
        return float("inf")
    if not nfo:
        record(item, library["Name"], name, "pending", "чака Jellyfin да запише NFO файла", target)
        return float("inf")
    due = ready_since.setdefault(item["Id"], time.time()) + ORGANIZE_AFTER
    if time.time() < due:
        record(item, library["Name"], name, "pending", "изчаква Jellyfin да запише картинките", target)
        return due
    reason = doubt(item, [parts[0], stem] if len(parts) == 2 else [stem])
    with lock:
        confirmed = db.execute(
            "SELECT 1 FROM confirmed WHERE path = ? AND tag = ?", (item["Path"], id_tag(item))
        ).fetchone()
    if reason and not confirmed:
        detail = f"{reason}: провери match-а в Jellyfin и потвърди"
        record(item, library["Name"], name, "review", detail, target, confirm=True)
        return float("inf")
    step(f"{label}: преименува на {target}")
    try:
        ingest(item, parts, location, label)
    except (Conflict, OSError) as e:
        log(f"error {label}: {e}")
        record(item, library["Name"], name, "review", str(e), target)
        return float("inf")
    record(item, library["Name"], name, "pending", "преименуван, чака Jellyfin да види новата папка", target)
    return time.time() + 60
