"""What each user has watched, where they stopped and what they marked as a favorite, exported every night to
DATA_DIR/userdata/<date>.json (the last KEEP are kept), so it outlives Jellyfin's database. Put back with

    docker exec media-ingest python /app/userdata.py restore /data/userdata/<date>.json [--user NAME] [--to NAME] [--apply]

which finds each item by its path, else by its ids, and without --apply only says what it would do."""

import argparse
import datetime
import json
import os
import sys
import threading
import time

from common.jellyfin import jellyfin

DATA_DIR = os.environ.get("DATA_DIR", "/data")
EXPORT_DIR = os.path.join(DATA_DIR, "userdata")
KEEP = 30
# UTC; 04:00 in Sofia in winter, when no one watches
EXPORT_HOUR = 2
FILTERS = ("IsPlayed", "IsResumable", "IsFavorite")
# What can be watched; a series' or season's played state is its episodes', so only their favorite is kept
TYPES = "Movie,Episode,Series,Season,Video,MusicVideo,BoxSet"
FOLDERS = {"Series", "Season", "BoxSet"}

wake = threading.Event()


def user_items(user_id):
    """The items a user has watched, started or marked as a favorite, with their user data."""
    found = {}
    for filter_ in FILTERS:
        for item in jellyfin(
            "/Items",
            userId=user_id,
            Recursive="true",
            IncludeItemTypes=TYPES,
            Filters=filter_,
            Fields="Path,ProviderIds",
            EnableUserData="true",
            Limit=1000000,
        )["Items"]:
            found[item["Id"]] = item
    return found.values()


def record(item):
    data = item.get("UserData") or {}
    entry = {
        "type": item["Type"],
        "name": item.get("Name"),
        "path": item.get("Path"),
        "ids": item.get("ProviderIds") or {},
        "favorite": bool(data.get("IsFavorite")),
    }
    if item["Type"] in ("Episode", "Season"):
        entry.update(series=item.get("SeriesName"), season=item.get("ParentIndexNumber"))
    if item["Type"] == "Episode":
        entry["episode"] = item.get("IndexNumber")
    if item["Type"] not in FOLDERS:
        entry.update(
            played=bool(data.get("Played")),
            play_count=data.get("PlayCount") or 0,
            position=data.get("PlaybackPositionTicks") or 0,
            last_played=data.get("LastPlayedDate"),
        )
    return entry


def export(directory=EXPORT_DIR, today=None):
    """Writes <date>.json for every user and drops all but the last KEEP; returns its path."""
    today = today or datetime.datetime.now(datetime.UTC).date().isoformat()
    users = {
        u["Name"]: [
            record(i)
            for i in user_items(u["Id"])
            if i["Type"] not in FOLDERS or (i.get("UserData") or {}).get("IsFavorite")
        ]
        for u in jellyfin("/Users")
    }
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{today}.json")
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as f:
        json.dump({"exported": time.time(), "users": users}, f, ensure_ascii=False, indent=1)
    os.replace(temporary, path)
    exports = sorted(n for n in os.listdir(directory) if n.endswith(".json"))
    for old in exports[:-KEEP]:
        os.remove(os.path.join(directory, old))
    return path, {name: len(items) for name, items in users.items()}


def exported_today(directory=EXPORT_DIR):
    today = datetime.datetime.now(datetime.UTC).date().isoformat()
    return os.path.exists(os.path.join(directory, f"{today}.json"))


def export_loop(log, event):
    """Once a night after EXPORT_HOUR, or at once when that night's export is missing."""
    while True:
        now = datetime.datetime.now(datetime.UTC)
        if now.hour >= EXPORT_HOUR and not exported_today():
            try:
                path, counts = export()
                detail = ", ".join(f"{name} {count}" for name, count in counts.items())
                event(path, "userdata", f"гледаното е запазено ({detail})", "Гледано")
            except Exception as e:
                log(f"error userdata: {e!r}")
        wake.wait(3600)


def index():
    """Every item Jellyfin has, by path and by (type, provider, id)."""
    by_path, by_id = {}, {}
    for item in jellyfin("/Items", Recursive="true", IncludeItemTypes=TYPES, Fields="Path,ProviderIds", Limit=1000000)[
        "Items"
    ]:
        if item.get("Path"):
            by_path[item["Path"]] = item["Id"]
        for provider, value in (item.get("ProviderIds") or {}).items():
            if value:
                by_id[(item["Type"], provider.lower(), str(value))] = item["Id"]
    return by_path, by_id


def find(entry, by_path, by_id):
    if entry.get("path") in by_path:
        return by_path[entry["path"]]
    for provider, value in entry.get("ids", {}).items():
        found = by_id.get((entry["type"], provider.lower(), str(value)))
        if found:
            return found
    return None


def user_data(entry):
    if entry["type"] in FOLDERS:
        return {"IsFavorite": entry["favorite"]}
    data = {
        "IsFavorite": entry["favorite"],
        "Played": entry["played"],
        "PlayCount": entry["play_count"],
        "PlaybackPositionTicks": entry["position"],
    }
    if entry.get("last_played"):
        data["LastPlayedDate"] = entry["last_played"]
    return data


def restore(path, only=None, to=None, apply=False):
    """Puts a user's (or every user's) exported data back; `to` puts one user's data on another user."""
    with open(path, encoding="utf-8") as f:
        exported = json.load(f)["users"]
    users = {u["Name"]: u["Id"] for u in jellyfin("/Users")}
    by_path, by_id = index()
    for name, entries in exported.items():
        if only and name != only:
            continue
        target = to or name
        if target not in users:
            print(f"{target}: няма такъв потребител")
            continue
        missing = []
        for entry in entries:
            item_id = find(entry, by_path, by_id)
            if not item_id:
                missing.append(entry)
                continue
            if apply:
                jellyfin(f"/UserItems/{item_id}/UserData", "POST", user_data(entry), userId=users[target])
        verb = "върнати" if apply else "за връщане"
        print(f"{name} → {target}: {len(entries) - len(missing)} {verb}, {len(missing)} не са намерени")
        for entry in missing:
            print(f"  ? {entry['type']} {entry.get('series') or ''} {entry.get('name')} {entry.get('path')}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("export")
    back = sub.add_parser("restore")
    back.add_argument("file")
    back.add_argument("--user", help="only this user's data")
    back.add_argument("--to", help="onto this user instead (with --user)")
    back.add_argument("--apply", action="store_true", help="write it, not only count it")
    args = parser.parse_args()
    if args.command == "export":
        print(export())
    else:
        if args.to and not args.user:
            parser.error("--to needs --user")
        restore(args.file, args.user, args.to, args.apply)


if __name__ == "__main__":
    sys.exit(main())
