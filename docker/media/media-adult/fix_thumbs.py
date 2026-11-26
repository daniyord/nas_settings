"""The performers' photos in „Порно“'s NFOs pointed at our own files (PLAN_media_services.md, stage 4): the migration
kept the <thumb> of the NFOs Jellyfin's ThePornDB plugin wrote, a path in Jellyfin's own metadata folder that an
empty Jellyfin doesn't have. A performer with a ThePornDB id and a photo of ours gets PEOPLE_DIR/tpdb-<id>.jpg; one
ThePornDB has no photo of keeps Jellyfin's. Jellyfin reads the NFOs it changes again.

    docker exec media-adult python /app/fix_thumbs.py [--apply]"""

import os
import sys
import xml.etree.ElementTree as ET

import metadata
from common import nfo
from common.jellyfin import jellyfin
from config import LIBRARY, PEOPLE_DIR


def fixed(root):
    """How many <thumb>s of the NFO now point at our photos."""
    changed = 0
    for actor in root.findall("actor"):
        tpdb_id = (actor.findtext("tpdbid") or "").strip()
        ours = os.path.join(PEOPLE_DIR, f"tpdb-{tpdb_id}.jpg")
        if not tpdb_id or not os.path.isfile(ours):
            continue
        thumb = actor.find("thumb")
        if thumb is None:
            thumb = ET.SubElement(actor, "thumb")
        if thumb.text != ours:
            thumb.text = ours
            changed += 1
    return changed


def main(argv):
    apply = "--apply" in argv
    folders = {f["Name"]: f for f in jellyfin("/Library/VirtualFolders")}
    admin = next(u for u in jellyfin("/Users") if u["Policy"]["IsAdministrator"])["Id"]
    items = jellyfin(
        "/Items", ParentId=folders[LIBRARY]["ItemId"], Recursive="true", IncludeItemTypes="Movie", Fields="Path"
    )["Items"]
    by_folder = {os.path.dirname(i["Path"]): i["Id"] for i in items if i.get("Path")}
    titles = thumbs = 0
    for location in folders[LIBRARY]["Locations"]:
        for root_dir, _, files in os.walk(location):
            if "movie.nfo" not in files:
                continue
            path = os.path.join(root_dir, "movie.nfo")
            root = nfo.read(path)
            if root is None:
                continue
            before = nfo.serialize(root)
            changed = fixed(root)
            if not changed:
                continue
            titles, thumbs = titles + 1, thumbs + changed
            if apply and nfo.write(path, root, before):
                metadata.own(path, root_dir)
                if root_dir in by_folder:
                    metadata.read_again(by_folder[root_dir], admin)
    print(f"{titles} заглавия, {thumbs} снимки" + ("" if apply else " (пусни с --apply)"), flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
