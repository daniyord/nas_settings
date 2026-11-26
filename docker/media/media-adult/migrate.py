"""„Порно“'s folders from before our NFOs brought to them (PLAN_media_services.md, stage 4), as stage 3 did for the
other libraries. The NFOs Jellyfin's ThePornDB plugin wrote stay the base, with what was corrected by hand: from
ThePornDB (or TMDB) by the id in the folder's name only what is missing is added, above all the performers with
their ids and photos; the genres and tags go and the studio is the folder's. A movie without an id keeps its NFO and
gets the poster with its title when its folder has none. Only safe with the library offline: Jellyfin then reads the
NFOs again as they are.

    docker exec media-adult python /app/migrate.py [--apply] [--limit N] [Studio/Folder ...]

Without --apply only shows what would change."""

import os
import sys
import tarfile
import time
import xml.etree.ElementTree as ET

import match
import metadata
from common import nfo, tmdb, tpdb
from common.jellyfin import jellyfin
from common.media import VIDEO_EXTS
from config import DATA_DIR, LIBRARY, MAX_CAST

BACKUP_DIR = os.path.join(DATA_DIR, "nfo-backup")
DROP = ("genre", "tag", "theporndbid", "studio")


def backup(locations):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR, f"{LIBRARY}-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz")
    with tarfile.open(path + ".part", "w:gz") as tar:
        for location in locations:
            for root, _, names in os.walk(location):
                for name in names:
                    if name.lower().endswith(".nfo"):
                        tar.add(os.path.join(root, name))
    os.replace(path + ".part", path)
    return path


def titles(locations):
    """(studio, folder, video) of every movie folder."""
    for location in locations:
        if not os.path.isdir(location):
            continue
        for studio in sorted(os.listdir(location)):
            studio_dir = os.path.join(location, studio)
            if studio.startswith(".") or not os.path.isdir(studio_dir):
                continue
            for name in sorted(os.listdir(studio_dir)):
                folder = os.path.join(studio_dir, name)
                if not os.path.isdir(folder):
                    continue
                videos = [n for n in os.listdir(folder) if os.path.splitext(n)[1].lower() in VIDEO_EXTS]
                if len(videos) == 1:
                    yield studio, folder, os.path.join(folder, videos[0])


def add_performers(root, new):
    """The new <actor>s the NFO lacks (by ThePornDB or TMDB id, or name), up to MAX_CAST in all; those it has get
    the id and photo they lack."""
    actors = root.findall("actor")

    def key(actor, tag):
        return (actor.findtext(tag) or "").strip()

    added = 0
    for actor in new:
        name = key(actor, "name").casefold()
        same = next(
            (
                a
                for a in actors
                if key(a, "name").casefold() == name
                or any(key(a, t) and key(a, t) == key(actor, t) for t in ("tpdbid", "tmdbid"))
            ),
            None,
        )
        if same is not None:
            for child in actor:
                if child.tag in ("tpdbid", "tmdbid", "thumb") and not key(same, child.tag):
                    same.append(child)
        elif len(actors) < MAX_CAST:
            order = actor.find("order")
            if order is not None:
                order.text = str(len(actors))
            root.append(actor)
            actors.append(actor)
            added += 1
    return added


def migrated(studio, folder, video):
    """(NFO path, its content before or None, the new root, what changed)."""
    path = nfo.path_for("movie", folder)
    root = nfo.read(path)
    before = None if root is None else nfo.serialize(root)
    if root is None:
        root = ET.Element("movie")
    notes = []
    dropped = [e for e in root if e.tag in DROP]
    genres = sum(e.tag in ("genre", "tag") for e in dropped)
    for e in dropped:
        root.remove(e)
    if genres:
        notes.append(f"-{genres} жанра/тага")
    pin = match.pinned(os.path.basename(folder))
    new, found = [], None
    if pin and pin[0] == "tpdb":
        found = tpdb.movie(pin[1])
        new = metadata.movie_nfo(found, studio)
    elif pin:
        found = metadata.tmdb_details(pin[1])
        new = metadata.tmdb_movie_nfo(found, studio)
    else:
        new = [nfo.element("studio", metadata.studio_name(studio)), nfo.element("mpaa", "XXX")]
        if not (root.findtext("title") or "").strip():
            new.append(nfo.element("title", match.ID_TAG.sub("", os.path.basename(folder)).strip()))
    performers = [e for e in new if e.tag == "actor"]
    nfo.merge(root, [e for e in new if e.tag != "actor"])
    added = add_performers(root, performers)
    if added:
        notes.append(f"+{added} изпълнители")
    filled = sum(1 for e in root.findall("actor") if e.find("thumb") is not None)
    notes.append(f"{len(root.findall('actor'))} изпълнители ({filled} със снимка)")
    return path, before, root, found, notes


def art(folder, found, pin, studio, video, apply):
    """The poster and background the folder lacks: ThePornDB's, TMDB's, or the poster with the title."""
    # A movie no database knows has no background to get
    wanted = ("folder.jpg", "backdrop.jpg") if pin else ("folder.jpg",)
    missing = [n for n in wanted if not metadata_has(folder, n)]
    if not missing or not apply:
        return missing
    if pin and pin[0] == "tpdb":
        urls = {"folder.jpg": tpdb.cover(found), "backdrop.jpg": tpdb.backdrop(found)}
        for name in missing:
            metadata.save_image(urls[name], os.path.join(folder, name))
    elif pin:
        for name, file_path in (("folder.jpg", found.get("poster_path")), ("backdrop.jpg", found.get("backdrop_path"))):
            if name in missing and file_path:
                tmdb.download_image(file_path, os.path.join(folder, name))
                metadata.own(os.path.join(folder, name), folder)
    elif "folder.jpg" in missing:
        root = nfo.read(nfo.path_for("movie", folder))
        title = root.findtext("title") if root is not None else os.path.basename(folder)
        metadata.write_poster(video, title or os.path.basename(folder), studio, os.path.join(folder, "folder.jpg"))
    return missing


def metadata_has(folder, name):
    bases = {"folder.jpg": ("folder", "poster", "cover"), "backdrop.jpg": ("backdrop", "fanart", "background")}[name]
    return any(
        os.path.splitext(n.lower())[0] in bases and os.path.splitext(n.lower())[1] in (".jpg", ".jpeg", ".png", ".webp")
        for n in os.listdir(folder)
    )


def main(argv):
    apply = "--apply" in argv
    metadata.DOWNLOAD = apply
    limit = int(argv[argv.index("--limit") + 1]) if "--limit" in argv else None
    only = [a for a in argv if "/" in a]
    folders = {f["Name"]: f for f in jellyfin("/Library/VirtualFolders")}
    locations = folders[LIBRARY]["Locations"]
    admin = next(u for u in jellyfin("/Users") if u["Policy"]["IsAdministrator"])["Id"]
    items = {}
    if apply:
        print("backup", backup(locations), flush=True)
        found_items = jellyfin(
            "/Items",
            ParentId=folders[LIBRARY]["ItemId"],
            Recursive="true",
            IncludeItemTypes="Movie",
            Fields="Path,Genres,Tags",
        )["Items"]
        items = {os.path.dirname(i["Path"]): i for i in found_items if i.get("Path")}
    done = 0
    for studio, folder, video in titles(locations):
        label = f"{studio}/{os.path.basename(folder)}"
        if only and label not in only:
            continue
        if limit is not None and done >= limit:
            break
        done += 1
        try:
            path, before, root, found, notes = migrated(studio, folder, video)
            pin = match.pinned(os.path.basename(folder))
            missing = art(folder, found, pin, studio, video, apply)
            if missing:
                notes.append("картинки: " + ", ".join(missing))
            changed = nfo.serialize(root) != before
            if apply and nfo.write(path, root, before):
                metadata.own(path, folder)
            item = items.get(folder)
            if apply and item and (changed or item.get("Genres") or item.get("Tags")):
                metadata.read_again(item["Id"], admin)
            print(f"{label}: {'промени' if changed else 'без промяна'}; {'; '.join(notes)}", flush=True)
        except (OSError, ET.ParseError, tpdb.NotFound, tmdb.NotFound) as e:
            print(f"{label}: грешка {e!r}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
