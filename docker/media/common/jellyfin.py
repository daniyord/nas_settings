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


def library_locations(folders, names):
    """The folders of the named libraries, from Jellyfin's /Library/VirtualFolders listing."""
    return [location for name in names if name in folders for location in folders[name]["Locations"]]
