"""Collections (PLAN_media_services.md). Jellyfin keeps who is in a collection only in its database (the collection.xml
it writes is never read back), so the movies' NFOs keep a copy, <set tmdbcolid="…"><name>…</name></set>, and the
collections are changed through its API. Three steps:

sync: a new collection once two movies name one (TMDB's, when the movie is put in place) and none of them is in a
collection yet; a new movie whose series has its collection goes into it. Named as the rest: "X (поредица)" after the
first movie's Bulgarian title ("(анимационна поредица)" when that is taken and they are cartoons), the name locked,
sorted with bg_sort; plot and artwork from TMDB. Only movies added lately count, so a collection deleted in Jellyfin,
or a movie taken out of one, stays so.

mirror: who is in which collection in Jellyfin, into the NFOs: a <set> for each, named as the collection; the one of a
collection the movie was taken out of, or that was deleted, goes. A collection.xml would not do, it goes with the
database. Not when collections vanish all at once (an empty or new database).

restore (by hand: python boxsets.py restore [--apply]): the collections back from the NFOs, after the database or the
folders are lost: the members missing put in, the collections missing made. Nothing is taken out."""

import json
import os
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timedelta, timezone

from common import nfo, tmdb
from common.jellyfin import jellyfin, library_locations
from config import COLLECTIONS_DIR, LIBRARIES
from metadata import own
from names import bg_sort
from state import event, log

# Where COLLECTIONS_DIR is inside Jellyfin
JELLYFIN_DIR = "/config/data/data/collections"
RECENT = timedelta(days=7)
AGAIN_AFTER = 3600
# A new movie reaches Jellyfin with the library scan after it is put in place
SETTLE = 300
RETRY = 300
# "Хан Аспарух - I серия - Фанагория", "Баварец 2: Филм втори", "Елхи 1": the series is what comes before
PART = re.compile(r"\s+-\s+|:\s")
NUMBER = re.compile(r"\s+\d+$")
ANIMATION = "Анимация"
# The collections mirror saw last, to tell one deleted in Jellyfin; Jellyfin passes over it for the dot
SEEN = ".seen.json"
# More collections gone at once, or fewer members than this share of the last time, is not taken from Jellyfin
MAX_GONE = 3
MIN_KEPT = 0.75
# No collections of these: every collection is in the Collections library, and its name shows in search and among
# the latest to users who can't see the library itself
ADULT = {"Еротика"}
wake = threading.Event()


def parse_set(e):
    """(TMDB id or None, name) of one <set>, the old form (<set>name</set>) too."""
    tmdb_id = (e.get("tmdbcolid") or "").strip()
    return int(tmdb_id) if tmdb_id.isdigit() else None, (e.findtext("name") or e.text or "").strip()


def nfo_path(movie):
    return os.path.join(os.path.dirname(movie["Path"]), "movie.nfo")


def read_nfo(movie):
    try:
        return nfo.read(nfo_path(movie))
    except (ET.ParseError, UnicodeDecodeError, OSError):
        return None


def key_of(tmdb_id, name):
    return (tmdb_id, None) if tmdb_id else (None, name.casefold())


def sets_of(movie):
    """The collections a movie's NFO names: (TMDB id, None) or (None, name)."""
    root = read_nfo(movie)
    found = [parse_set(e) for e in root.findall("set")] if root is not None else []
    return [key_of(tmdb_id, name) for tmdb_id, name in found if tmdb_id or name]


def added(movie, since):
    return datetime.fromisoformat(movie["DateCreated"][:19]).replace(tzinfo=timezone.utc) > since


def name_for(movies, taken):
    """The name, or None when it is taken."""
    first = min(movies, key=lambda m: (m.get("ProductionYear") or 9999, m["Name"]))
    title = NUMBER.sub("", PART.split(first["Name"])[0]).strip()
    kinds = ["поредица"]
    if all(ANIMATION in (m.get("Genres") or []) for m in movies):
        kinds.append("анимационна поредица")
    return next((n for n in (f"{title} ({kind})" for kind in kinds) if n.casefold() not in taken), None)


def make(movies, name, tmdb_id, admin):
    item_id = jellyfin("/Collections", "POST", Name=name, Ids=",".join(m["Id"] for m in movies))["Id"]
    details = None
    if tmdb_id:
        try:
            details = tmdb.collection(tmdb_id)
        except tmdb.NotFound:
            pass
    full = jellyfin(f"/Users/{admin}/Items/{item_id}")
    full["ForcedSortName"] = bg_sort(name)
    full["LockedFields"] = sorted(set(full.get("LockedFields") or []) | {"Name"})
    if details and details.get("overview"):
        full["Overview"] = details["overview"]
    if tmdb_id:
        full["ProviderIds"] = {**(full.get("ProviderIds") or {}), "Tmdb": str(tmdb_id)}
    jellyfin(f"/Items/{item_id}", "POST", full)
    folder = os.path.join(COLLECTIONS_DIR, os.path.relpath(full.get("Path") or JELLYFIN_DIR, JELLYFIN_DIR))
    if not details or folder == COLLECTIONS_DIR or not os.path.isdir(folder):
        return item_id
    images = details.get("images", {})
    for file, kind, fallback in (
        ("folder.jpg", "posters", "poster_path"),
        ("backdrop.jpg", "backdrops", "backdrop_path"),
    ):
        picked = tmdb.pick_image(images.get(kind, [])) or details.get(fallback)
        if picked:
            tmdb.download_image(picked, os.path.join(folder, file))
            own(os.path.join(folder, file), folder)
    jellyfin(
        f"/Items/{item_id}/Refresh",
        "POST",
        MetadataRefreshMode="Default",
        ImageRefreshMode="Default",
        ReplaceAllMetadata="false",
        ReplaceAllImages="false",
    )
    return item_id


class Library:
    """The movies of LIBRARIES and the collections, as Jellyfin has them now."""

    def __init__(self):
        self.admin = next(u for u in jellyfin("/Users") if u["Policy"]["IsAdministrator"])["Id"]
        folders = {f["Name"]: f for f in jellyfin("/Library/VirtualFolders")}
        locations = library_locations(folders, [n for n in LIBRARIES if n not in ADULT])
        missing = [p for p in locations if not os.path.isdir(p)]
        if missing:
            # An unmounted disk would look like its movies' NFOs name nothing
            raise OSError(f"no folder {', '.join(missing)}")
        ours = tuple(location.rstrip("/") + "/" for location in locations)
        self.movies = [
            m
            for m in jellyfin(
                "/Items",
                Recursive="true",
                IncludeItemTypes="Movie",
                Fields="Path,Genres,DateCreated",
                Limit=100000,
                userId=self.admin,
            )["Items"]
            if (m.get("Path") or "").startswith(ours)
        ]
        self.boxes = {
            b["Id"]: b
            for b in jellyfin(
                "/Items",
                Recursive="true",
                IncludeItemTypes="BoxSet",
                Fields="ProviderIds",
                Limit=100000,
                userId=self.admin,
            )["Items"]
        }
        self.inside = {
            b: {m["Id"] for m in jellyfin("/Items", ParentId=b, userId=self.admin)["Items"]} for b in self.boxes
        }

    def tmdb_of(self, box):
        tmdb_id = ((self.boxes[box].get("ProviderIds") or {}).get("Tmdb") or "").strip()
        return int(tmdb_id) if tmdb_id.isdigit() else None

    def key(self, box):
        """How the NFOs name a collection: by its TMDB id when no other collection has it, else by name."""
        tmdb_id = self.tmdb_of(box)
        if tmdb_id and sum(self.tmdb_of(b) == tmdb_id for b in self.boxes) == 1:
            return tmdb_id, None
        return None, self.boxes[box]["Name"].casefold()

    def find(self, tmdb_id, name):
        """The collection a <set> names: by TMDB id, else by name; None for none or two."""
        found = [b for b in self.boxes if tmdb_id and self.tmdb_of(b) == tmdb_id]
        if not found:
            found = [b for b in self.boxes if self.boxes[b]["Name"].casefold() == name.casefold()]
        return found[0] if len(found) == 1 else None


def sync(library=None):
    """Makes the collections due and puts new movies into theirs; returns what was done."""
    library = library or Library()
    since = datetime.now(timezone.utc) - RECENT
    if not any(added(m, since) for m in library.movies):
        return []
    admin, inside = library.admin, library.inside
    taken = {b["Name"].casefold() for b in library.boxes.values()}
    names = {b: box["Name"] for b, box in library.boxes.items()}
    keys = {m["Id"]: set(sets_of(m)) for m in library.movies}
    groups = {}
    for m in library.movies:
        for key in keys[m["Id"]]:
            groups.setdefault(key, []).append(m)
    done = []
    for key, group in groups.items():
        recent = [m for m in group if added(m, since)]
        if not recent:
            continue
        # The series' collection: no movie in it names another (a movie may be in other collections as well, made by
        # hand); some NFOs name none
        ids = {m["Id"] for m in group}
        homes = [
            b
            for b, members in inside.items()
            if members & ids and all(key in keys.get(i, ()) or not keys.get(i) for i in members)
        ]
        if len(homes) > 1:
            listed = ", ".join(m["Name"] for m in recent)
            log(f"error boxsets: {listed}: which of {', '.join(sorted(names[b] for b in homes))} can't be told")
            continue
        if homes:
            # A new movie of a series that has its collection; the older ones not in it were taken out of it
            box = homes[0]
            new = [m for m in recent if m["Id"] not in inside[box]]
            if not new:
                continue
            jellyfin(f"/Collections/{box}/Items", "POST", ids=",".join(m["Id"] for m in new))
            name = names[box]
        else:
            if len(group) < 2:
                continue
            name = name_for(group, taken)
            if not name:
                log(f"error boxsets: {', '.join(m['Name'] for m in group)}: the collection's name is taken")
                continue
            new = group
            box = make(group, name, key[0], admin)
            names[box] = name
            taken.add(name.casefold())
        inside.setdefault(box, set()).update(m["Id"] for m in new)
        done.append(name)
        event(COLLECTIONS_DIR, "boxsets", f"{name}: {', '.join(m['Name'] for m in new)}", "Колекции")
    return done


def load_seen():
    """{"keys": [[TMDB id, name], …], "members": count} as mirror saw it last; None before the first time."""
    try:
        with open(os.path.join(COLLECTIONS_DIR, SEEN), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_seen(keys, members):
    path = os.path.join(COLLECTIONS_DIR, SEEN)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"keys": sorted(map(list, keys), key=str), "members": members}, f, ensure_ascii=False)
    os.replace(path + ".tmp", path)
    own(path, COLLECTIONS_DIR)


def mirror(library=None):
    """Puts who is in which collection into the NFOs; returns the number of NFOs changed, None when it was not
    taken from Jellyfin."""
    library = library or Library()
    keys = {b: library.key(b) for b in library.boxes}
    members = sum(len(m) for m in library.inside.values())
    seen = load_seen()
    gone = set()
    if seen:
        gone = {tuple(k) for k in seen["keys"]} - set(keys.values())
        if len(gone) > MAX_GONE or members < seen["members"] * MIN_KEPT:
            log(
                f"error boxsets: {len(gone)} collections gone, {members} titles in them of {seen['members']}; "
                "not put into the NFOs (deleted in Jellyfin? then delete .seen.json in the collections' folder)"
            )
            return None
    since = datetime.now(timezone.utc) - RECENT
    boxes_of = {}
    for box, ids in library.inside.items():
        for i in ids:
            boxes_of.setdefault(i, []).append(box)
    changed = 0
    for movie in library.movies:
        root = read_nfo(movie)
        if root is None:
            continue
        before = nfo.serialize(root)
        theirs = boxes_of.get(movie["Id"], [])
        # A movie added lately is sync's: what its NFO names is not taken away yet
        recent = added(movie, since)
        placed = set()
        for e in root.findall("set"):
            tmdb_id, name = parse_set(e)
            box = library.find(tmdb_id, name)
            if box in theirs and box not in placed:
                placed.add(box)
                root.remove(e)
            elif (box or key_of(tmdb_id, name) in gone) and not recent:
                root.remove(e)
        for box in theirs:
            tmdb_id = keys[box][0]
            attributes = {"tmdbcolid": tmdb_id} if tmdb_id else {}
            root.append(nfo.element("set", None, nfo.element("name", library.boxes[box]["Name"]), **attributes))
        if nfo.write(nfo_path(movie), root, before):
            own(nfo_path(movie), os.path.dirname(nfo_path(movie)))
            changed += 1
    save_seen(set(keys.values()), members)
    if changed:
        event(COLLECTIONS_DIR, "boxsets", f"{changed} NFO с колекциите от Jellyfin", "Колекции")
    return changed


def restore(apply=False, library=None):
    """The collections back from the NFOs; returns [(collection, what is done, titles)]."""
    library = library or Library()
    groups, names = {}, {}
    for movie in library.movies:
        root = read_nfo(movie)
        for tmdb_id, name in [parse_set(e) for e in root.findall("set")] if root is not None else []:
            if tmdb_id or name:
                key = key_of(tmdb_id, name)
                groups.setdefault(key, []).append(movie)
                names.setdefault(key, Counter())[name] += 1
    plan = []
    for key, group in groups.items():
        name = names[key].most_common(1)[0][0]
        box = library.find(key[0], name)
        if box:
            missing = [m for m in group if m["Id"] not in library.inside[box]]
            if missing:
                plan.append((library.boxes[box]["Name"], "добавяне", missing, box))
        # TMDB names a collection for a movie alone too: one of ours of one movie is not told from it, and is lost
        elif len(group) >= 2:
            plan.append((name, "нова", group, key[0]))
    for name, what, group, target in plan:
        if not apply:
            continue
        if what == "нова":
            make(group, name, target, library.admin)
        else:
            jellyfin(f"/Collections/{target}/Items", "POST", ids=",".join(m["Id"] for m in group))
    if apply and plan:
        event(COLLECTIONS_DIR, "boxsets", f"Възстановени {len(plan)} колекции от NFO", "Колекции")
    return [(name, what, [m["Name"] for m in group]) for name, what, group, _ in plan]


def boxsets_loop():
    """At once, hourly, and SETTLE seconds after woken (a new movie)."""
    while True:
        try:
            library = Library()
            sync(library)
            # What sync did is in Jellyfin now, not in this snapshot
            mirror()
        except Exception as e:
            log(f"error boxsets: {e!r}")
            time.sleep(RETRY)
            continue
        if wake.wait(AGAIN_AFTER):
            wake.clear()
            time.sleep(SETTLE)


if __name__ == "__main__":
    if sys.argv[1:2] != ["restore"]:
        sys.exit("python boxsets.py restore [--apply]")
    applying = "--apply" in sys.argv
    for collection, what, titles in restore(applying):
        print(f"{what}: {collection}: {', '.join(titles)}")
    if not applying:
        print("(проба; с --apply се прилага)")
