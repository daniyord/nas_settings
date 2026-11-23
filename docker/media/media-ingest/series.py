"""A series in a release folder (one with a season in its name, or any series folder that came from qBittorrent) is
organized into <Title> (<Year>) [imdbid-tt...]/Season NN/SxxEyy - <Title>, or added to the series' folder when there
is one, once every episode's SxxEyy matches Jellyfin's."""

import collections
import os
import time

from common.jellyfin import jellyfin
from common.media import EPISODE, ORGANIZED, SETTLE_AFTER, SUBTITLE, VIDEO_EXTS, Conflict, with_language
from config import JELLYFIN_FILES, ORGANIZE_AFTER, SEASON_TOKEN
from movies import make_dir, set_sort_name
from names import doubt, episode_name, episode_numbers, id_tag, is_xml, owner, release_title, season_dir, target_name
from state import db, event, lock, log, missing_since, ready_since, record, status, step


def episode_plan(folder, stem, new_stem):
    """(old, new) names of an episode's video and the files named after it, and the release notes to delete."""
    names = sorted(n for n in os.listdir(folder) if os.path.isfile(os.path.join(folder, n)))
    videos = [os.path.splitext(n)[0] for n in names if os.path.splitext(n)[1].lower() in VIDEO_EXTS]
    renames, deletions = [], []
    for name in names:
        path = os.path.join(folder, name)
        base, ext = os.path.splitext(name)
        if ext.lower() in VIDEO_EXTS:
            if base == stem:
                renames.append((name, new_stem + ext))
        elif owner(name, videos) == stem:
            rest = name[len(stem) :]
            if ext.lower() == ".nfo" and not is_xml(path):
                deletions.append(name)
            elif SUBTITLE.fullmatch(rest):
                renames.append((name, new_stem + with_language(path, rest)))
            else:
                renames.append((name, new_stem + rest))
    return renames, deletions


def series_problem(folder, videos, episodes):
    """Why a release folder can't be organized on its own, or "" when it can."""
    paths = {e["Path"] for e in episodes}
    for video in videos:
        if video not in paths:
            return f"{os.path.relpath(video, folder)} не е епизод в Jellyfin: подреди го на ръка"
    for e in episodes:
        rel = os.path.relpath(e["Path"], folder)
        if rel.count("/") > 1:
            return f"{rel} е в подпапка на подпапка: подреди го на ръка"
        if e.get("ParentIndexNumber") is None or e.get("IndexNumber") is None:
            return f"{rel} е без номер на сезон или епизод в Jellyfin"
        # The file's name is the source of truth for the numbers (PLAN_jellyfin_library.md)
        m = EPISODE.search(os.path.basename(e["Path"]))
        if not m:
            return f"{rel} няма SxxEyy в името"
        found = (int(m.group(1)), int(m.group(2)), int(m.group(3)) if m.group(3) else None)
        if found != episode_numbers(e):
            return f"{rel}: Jellyfin го има като S{e['ParentIndexNumber']:02d}E{e['IndexNumber']:02d}"
    return ""


def organize_series(folder, episodes, target_dir, label):
    """Moves the episodes of a release folder to target_dir/Season NN as SxxEyy - Title, renaming the
    folder itself into target_dir when the series has no folder yet, otherwise adding to it."""
    merge = os.path.exists(target_dir)
    by_dir = collections.defaultdict(list)
    for e in episodes:
        by_dir[os.path.dirname(e["Path"])].append(e)
    seasons = {}
    dir_moves = {}

    def season_path(n):
        if n not in seasons:
            seasons[n] = os.path.join(target_dir, season_dir(target_dir if merge else folder, n))
        return seasons[n]

    def renamed(path):
        return path if merge else target_dir + path[len(folder) :]

    def before(path):
        """Where a path of the result is now."""
        for src, dst in dir_moves.items():
            if path == dst or path.startswith(dst + "/"):
                return src + path[len(dst) :]
        return path if merge else folder + path[len(target_dir) :]

    # A subfolder with the only episodes of one season becomes its folder, keeping what Jellyfin saved in it
    for d, eps in by_dir.items():
        numbers = {e["ParentIndexNumber"] for e in eps}
        if d == folder or len(numbers) != 1:
            continue
        n = numbers.pop()
        dest = season_path(n)
        shared = any(o != d and any(e["ParentIndexNumber"] == n for e in other) for o, other in by_dir.items())
        if not shared and renamed(d) != dest and not os.path.exists(before(dest)):
            dir_moves[d] = dest

    moves, deletions = [], []
    for d, eps in by_dir.items():
        at = dir_moves.get(d) or renamed(d)
        for e in eps:
            stem = os.path.splitext(os.path.basename(e["Path"]))[0]
            renames, dels = episode_plan(d, stem, episode_name(e, stem))
            dest = season_path(e["ParentIndexNumber"])
            moves += [(os.path.join(at, old), os.path.join(dest, new)) for old, new in renames]
            deletions += [os.path.join(at, name) for name in dels]
    moves = [(src, dst) for src, dst in moves if src != dst]
    news = [dst.lower() for _, dst in moves]
    if len(set(news)) != len(news):
        same = sorted({os.path.basename(n) for n in news if news.count(n) > 1})
        raise Conflict(f"две неща биха се казали еднакво: {', '.join(same)}")
    sources = {src for src, _ in moves}
    taken = [dst for _, dst in moves if dst not in sources and os.path.exists(before(dst))]
    if taken:
        raise Conflict(f"вече има {', '.join(os.path.relpath(t, target_dir) for t in taken)}")
    if merge:
        for n, dest in seasons.items():
            if not os.path.isdir(dest):
                continue
            have = {
                i
                for name in os.listdir(dest)
                if os.path.splitext(name)[1].lower() in VIDEO_EXTS
                for m in [EPISODE.search(name)]
                if m
                for i in range(int(m.group(2)), int(m.group(3) or m.group(2)) + 1)
            }
            incoming = {
                i
                for e in episodes
                if e["ParentIndexNumber"] == n
                for _, first, last in [episode_numbers(e)]
                for i in range(first, (last or first) + 1)
            }
            if have & incoming:
                raise Conflict(
                    f"{os.path.basename(dest)} вече има епизод {', '.join(map(str, sorted(have & incoming)))}"
                )

    if not merge:
        os.rename(folder, target_dir)
    for src, dst in dir_moves.items():
        os.rename(renamed(src), dst)
    for dest in seasons.values():
        if not os.path.isdir(dest):
            make_dir(dest, os.path.dirname(target_dir))
    for path in deletions:
        os.remove(path)
    for src, dst in moves:
        os.rename(src, dst)

    # What's left of the release: Jellyfin's NFO files and artwork go, anything else stays for a look
    emptied = {renamed(d) for d in by_dir if d not in dir_moves} | ({folder} if merge else set())
    emptied -= set(seasons.values()) | {target_dir}
    left = []
    for d in sorted(emptied, key=len, reverse=True):
        if not os.path.isdir(d):
            continue
        for root, _, names in os.walk(d, topdown=False):
            for name in names:
                if os.path.splitext(name)[1].lower() in JELLYFIN_FILES:
                    os.remove(os.path.join(root, name))
            if not os.listdir(root):
                os.rmdir(root)
        if os.path.exists(d):
            left.append(os.path.relpath(d, os.path.dirname(target_dir)))

    updates = [
        {"Path": folder, "UpdateType": "Modified" if os.path.exists(folder) else "Deleted"},
        {"Path": target_dir, "UpdateType": "Created"},
    ]
    jellyfin("/Library/Media/Updated", "POST", {"Updates": updates})
    detail = f"{os.path.basename(folder)} → {os.path.basename(target_dir)}/ ({len(episodes)} еп."
    detail += ", добавени към сериала" if merge else ""
    detail += f"; остана {', '.join(left)}" if left else ""
    event(os.path.join(target_dir, "tvshow.nfo"), "moved", detail + ")", label)


def check_series(item, library, location, admin):
    """Looks at one series and organizes it when it's a release folder that's ready; returns when it wants to be
    looked at again."""
    folder = item["Path"].rstrip("/")
    name = os.path.basename(folder)
    label = f"{library['Name']}/{name}"
    identified = bool(id_tag(item) and item.get("ProductionYear"))
    nfo = is_xml(os.path.join(folder, "tvshow.nfo"))

    if ORGANIZED.fullmatch(name):
        with lock:
            status.pop(item["Id"], None)
        if identified and nfo:
            set_sort_name(item, admin, label)
        return float("inf")
    if not SEASON_TOKEN.search(name):
        # A downloaded series pack ("Everybody Loves Raymond/... s01/...") is a release folder too; series
        # folders named by hand never came through qBittorrent
        with lock:
            downloaded = db.execute("SELECT 1 FROM events WHERE kind = 'downloaded' AND path = ?", (folder,)).fetchone()
        if not downloaded:
            return float("inf")

    if not os.path.isdir(folder):
        record(item, library["Name"], name, "pending", "Jellyfin още показва стария път")
        missing_since.setdefault(item["Id"], time.time())
        return float("inf")
    videos = [
        os.path.join(d, n)
        for d, _, names in os.walk(folder)
        for n in names
        if os.path.splitext(n)[1].lower() in VIDEO_EXTS
    ]
    if not videos:
        record(item, library["Name"], name, "review", "в папката няма видео")
        return float("inf")
    settles = max(os.stat(v).st_ctime for v in videos) + SETTLE_AFTER
    if time.time() < settles:
        record(item, library["Name"], name, "pending", "файловете още се копират")
        return settles
    if not identified:
        detail = "Jellyfin не е разпознал сериала (или няма година): Identify в Jellyfin"
        record(item, library["Name"], name, "unidentified", detail)
        return float("inf")
    tag = id_tag(item)
    existing = [
        os.path.join(loc.rstrip("/"), n)
        for loc in library["Locations"]
        if os.path.isdir(loc)
        for n in os.listdir(loc)
        if n.endswith(f"[{tag}]") and os.path.isdir(os.path.join(loc, n))
    ]
    if len(existing) > 1 or (existing and os.path.dirname(existing[0]) != location.rstrip("/")):
        detail = f"сериалът вече е в {', '.join(existing)}: премести го на ръка"
        record(item, library["Name"], name, "review", detail)
        return float("inf")
    target_dir = existing[0] if existing else os.path.join(location.rstrip("/"), target_name(item))
    target = os.path.basename(target_dir)
    if not nfo:
        record(item, library["Name"], name, "pending", "чака Jellyfin да запише NFO файла", target)
        return float("inf")
    listed = jellyfin(
        "/Items", ParentId=item["Id"], Recursive="true", IncludeItemTypes="Episode", Fields="Path", Limit=100000
    )["Items"]
    episodes = [e for e in listed if e.get("Path")]
    problem = series_problem(folder, videos, episodes)
    if problem:
        record(item, library["Name"], name, "review", problem, target)
        return float("inf")
    if not all(is_xml(os.path.splitext(e["Path"])[0] + ".nfo") for e in episodes):
        record(item, library["Name"], name, "pending", "чака Jellyfin да запише NFO файловете на епизодите", target)
        return float("inf")
    due = ready_since.setdefault(item["Id"], time.time()) + ORGANIZE_AFTER
    if time.time() < due:
        record(item, library["Name"], name, "pending", "изчаква Jellyfin да запише картинките", target)
        return due
    reason = doubt(item, [release_title(name), release_title(os.path.basename(videos[0]))])
    with lock:
        confirmed = db.execute("SELECT 1 FROM confirmed WHERE path = ? AND tag = ?", (item["Path"], tag)).fetchone()
    if reason and not confirmed:
        detail = f"{reason}: провери match-а в Jellyfin и потвърди"
        record(item, library["Name"], name, "review", detail, target, confirm=True)
        return float("inf")
    step(f"{label}: подрежда в {target}")
    try:
        organize_series(folder, episodes, target_dir, label)
    except (Conflict, OSError) as e:
        log(f"error {label}: {e}")
        record(item, library["Name"], name, "review", str(e), target)
        return float("inf")
    record(item, library["Name"], name, "pending", "подреден, чака Jellyfin да види новата папка", target)
    return time.time() + 60
