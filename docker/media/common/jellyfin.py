"""Jellyfin's API as the services' admin key sees it."""

import json
import os
import urllib.parse
import urllib.request
from typing import Any

URL = os.environ["JELLYFIN_URL"].rstrip("/")
API_KEY = os.environ["JELLYFIN_API_KEY"]


def jellyfin(path, method="GET", body=None, **params) -> Any:
    """The decoded JSON of the answer; None for an empty one (POSTs)."""
    if path == "/Items" and "IncludeItemTypes" in params:
        # Otherwise the movies of a collection are listed as the collection alone
        params.setdefault("CollapseBoxSetItems", "false")
    query = "?" + urllib.parse.urlencode(params) if params else ""
    headers = {
        "Authorization": f'MediaBrowser Token="{API_KEY}"',
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(URL + path + query, data, headers, method=method)
    with urllib.request.urlopen(request, timeout=60) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


def is_empty(folder):
    """Whether a folder has nothing in it but hidden entries."""
    try:
        return not any(not name.startswith(".") for name in os.listdir(folder))
    except OSError:
        return False


def announce(paths, rescan=False):
    """Tells Jellyfin of new paths in its libraries. rescan: one went into a library folder that was empty, which
    Jellyfin doesn't watch when it starts with it so; what comes into it then isn't seen, announced or not, until a
    scan of all libraries (about half a minute), which also starts watching it."""
    jellyfin("/Library/Media/Updated", "POST", {"Updates": [{"Path": p, "UpdateType": "Created"} for p in paths]})
    if rescan:
        jellyfin("/Library/Refresh", "POST")


def library_locations(folders, names):
    """The folders of the named libraries, from Jellyfin's /Library/VirtualFolders listing."""
    return [location for name in names if name in folders for location in folders[name]["Locations"]]


def reread(item_id, user_id):
    """Makes Jellyfin read a changed NFO again: a locked item never does on its own, so it is unlocked and refreshed
    in full; the NFO locks it again, and being locked keeps the online providers out (PLAN_media_services.md, 2.1).
    Only safe with Jellyfin's NFO saver off, or the unlock would be written over the NFO. Its people are cleared
    first: Jellyfin keeps the ones it has when the NFO names nobody (an animation, which gets no actors)."""
    item = jellyfin(f"/Items/{item_id}", userId=user_id)
    item["LockData"] = False
    item["People"] = []
    jellyfin(f"/Items/{item_id}", "POST", item)
    jellyfin(
        f"/Items/{item_id}/Refresh",
        "POST",
        MetadataRefreshMode="FullRefresh",
        ImageRefreshMode="Default",
        ReplaceAllMetadata="false",
        ReplaceAllImages="false",
    )
