"""Jellyfin's libraries as code: libraries.toml says which libraries there are (name, type, folders, settings), which
user sees which of them and how NFOs are written; this makes the server match it. Without --apply it only lists what
differs. It creates and corrects, and never deletes: a library, a folder or a user it doesn't know is left as it is.
Users themselves (and their passwords) are not created here."""

import argparse
import json
import os
import sys
import tomllib
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.jellyfin import jellyfin  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def load(path):
    with open(path, "rb") as f:
        return tomllib.load(f)


def wanted_options(config, library):
    """The LibraryOptions the description sets for a library; keys it leaves out are Jellyfin's business."""
    return {**config.get("options", {}), **library.get("options", {})}


def short(value):
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 100 else text[:97] + "..."


def plan_libraries(config, folders):
    """(description, action) pairs that bring the libraries in line; action() does it."""
    changes = []
    for library in config["library"]:
        name, options = library["name"], wanted_options(config, library)
        current = folders.get(name)
        if current is None:
            if library.get("create", True):
                body = {"LibraryOptions": {**options, "PathInfos": [{"Path": p} for p in library["paths"]]}}
                query = urllib.parse.urlencode(
                    {"name": name, "collectionType": library["type"], "refreshLibrary": "false"}
                )
                changes.append(
                    (
                        f"{name}: нова библиотека ({library['type']}, {', '.join(library['paths'])})",
                        lambda q=query, b=body: jellyfin(f"/Library/VirtualFolders?{q}", "POST", b),
                    )
                )
            else:
                changes.append((f"{name}: няма я (Jellyfin я прави сам)", None))
            continue
        if (current.get("CollectionType") or "") != library["type"]:
            changes.append((f"{name}: тип {current.get('CollectionType')}, а трябва {library['type']} — на ръка", None))
        for path in library["paths"]:
            if path not in current["Locations"]:
                body = {"Name": name, "PathInfo": {"Path": path}}
                changes.append(
                    (
                        f"{name}: нова папка {path}",
                        lambda b=body: jellyfin("/Library/VirtualFolders/Paths?refreshLibrary=false", "POST", b),
                    )
                )
        for path in current["Locations"]:
            if path not in library["paths"]:
                changes.append((f"{name}: папка {path} не е в описанието (остава)", None))
        have = current["LibraryOptions"]
        differ = {k: v for k, v in options.items() if have.get(k) != v}
        for key, value in differ.items():
            changes.append((f"{name}: {key} {short(have.get(key))} → {short(value)}", None))
        if differ:
            body = {"Id": current["ItemId"], "LibraryOptions": {**have, **differ}}
            changes.append(
                (
                    f"{name}: записва настройките",
                    lambda b=body: jellyfin("/Library/VirtualFolders/LibraryOptions", "POST", b),
                )
            )
    for name in folders:
        if name not in {library["name"] for library in config["library"]}:
            changes.append((f"{name}: библиотеката не е в описанието (остава)", None))
    return changes


def plan_users(config, folders, users):
    changes = []
    ids = {name: folder["ItemId"] for name, folder in folders.items()}
    for name, access in config.get("users", {}).items():
        user = users.get(name)
        if user is None:
            changes.append((f"потребител {name}: няма го (създава се на ръка)", None))
            continue
        policy = dict(user["Policy"])
        if access == "all":
            if policy["EnableAllFolders"]:
                continue
            policy["EnableAllFolders"] = True
            detail = "всички библиотеки"
        else:
            unknown = [n for n in access if n not in ids]
            if unknown:
                changes.append((f"потребител {name}: няма библиотеки {', '.join(unknown)} (пусни първо тях)", None))
                continue
            wanted = sorted(ids[n] for n in access)
            if not policy["EnableAllFolders"] and sorted(policy["EnabledFolders"]) == wanted:
                continue
            policy.update(EnableAllFolders=False, EnabledFolders=wanted)
            detail = ", ".join(access)
        changes.append(
            (
                f"потребител {name}: вижда {detail}",
                lambda u=user["Id"], p=policy: jellyfin(f"/Users/{u}/Policy", "POST", p),
            )
        )
    return changes


def plan_nfo(config):
    wanted = config.get("nfo", {})
    have = jellyfin("/System/Configuration/xbmcmetadata")
    differ = {k: v for k, v in wanted.items() if have.get(k) != v}
    if not differ:
        return []
    detail = ", ".join(f"{k} {short(have.get(k))} → {short(v)}" for k, v in differ.items())
    body = {**have, **differ}
    return [(f"NFO: {detail}", lambda b=body: jellyfin("/System/Configuration/xbmcmetadata", "POST", b))]


def plan_server(config):
    """The server-wide fetchers per item type (people's above all, which no library setting reaches)."""
    wanted = config.get("metadata_options", {})
    if not wanted:
        return []
    have = jellyfin("/System/Configuration")
    options = {o["ItemType"]: o for o in have.get("MetadataOptions", [])}
    changes, updated = [], False
    for item_type, values in wanted.items():
        current = options.get(item_type)
        if current is None:
            current = {
                "ItemType": item_type,
                "DisabledMetadataSavers": [],
                "LocalMetadataReaderOrder": [],
                "DisabledMetadataFetchers": [],
                "MetadataFetcherOrder": [],
                "DisabledImageFetchers": [],
                "ImageFetcherOrder": [],
            }
            have.setdefault("MetadataOptions", []).append(current)
        for key, value in values.items():
            if current.get(key) != value:
                changes.append((f"сървър, {item_type}: {key} {short(current.get(key))} → {short(value)}", None))
                current[key] = value
                updated = True
    if updated:
        changes.append(("сървър: записва настройките", lambda: jellyfin("/System/Configuration", "POST", have)))
    return changes


def plan_plugins(config):
    """Plugins to switch off (not uninstall); Jellyfin needs a restart for it to take effect."""
    changes = []
    for plugin in jellyfin("/Plugins"):
        if plugin["Name"] in config.get("plugins", {}).get("disabled", []) and plugin["Status"] == "Active":
            path = f"/Plugins/{plugin['Id']}/{plugin['Version']}/Disable"
            changes.append(
                (f"плъгин {plugin['Name']}: изключва се (рестартирай Jellyfin)", lambda p=path: jellyfin(p, "POST"))
            )
    return changes


def run(changes, apply):
    """Prints the changes (→ to make, · only a note) and makes them with apply; returns how many were to make."""
    for text, action in changes:
        if apply and action:
            action()
        print(("✓ " if apply else "→ ") + text if action else "· " + text)
    return sum(1 for _, action in changes if action)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="make the changes, not only list them")
    parser.add_argument("--config", default=os.path.join(HERE, "libraries.toml"))
    args = parser.parse_args()
    config = load(args.config)
    folders = {f["Name"]: f for f in jellyfin("/Library/VirtualFolders")}
    changes = plan_libraries(config, folders) + plan_nfo(config) + plan_server(config) + plan_plugins(config)
    pending = run(changes, args.apply)
    # Users after the libraries, as they name them
    if args.apply:
        folders = {f["Name"]: f for f in jellyfin("/Library/VirtualFolders")}
    users = {u["Name"]: u for u in jellyfin("/Users")}
    pending += run(plan_users(config, folders, users), args.apply)
    if not args.apply:
        print(f"{pending} промени" + (" (пусни с --apply)" if pending else ": Jellyfin е като описанието"))
    return 1 if pending and not args.apply else 0


if __name__ == "__main__":
    sys.exit(main())
