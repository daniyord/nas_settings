"""qBittorrent's Web API as media-ingest and media-adult use it: its finished torrents in a category named after a
library and that category's download folder. Its paths are its own (its container's) and are translated to the
service's with QBIT_PATHS."""

import http.cookiejar
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# Empty turns the downloads off
URL = os.environ.get("QBIT_URL", "").rstrip("/")
USER = os.environ.get("QBIT_USER", "")
PASSWORD = os.environ.get("QBIT_PASSWORD", "")

_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def path_map(text):
    """[(from, to)] of "from=to,from=to"."""
    return [tuple(p.strip().split("=", 1)) for p in text.split(",") if "=" in p]


def translate(path, pairs):
    """The path with the first of `pairs` (from, to) it starts with swapped, or None."""
    for src, dst in pairs:
        src = src.rstrip("/")
        if path == src or path.startswith(src + "/"):
            return dst.rstrip("/") + path[len(src) :]
    return None


def qbit(path, **form) -> Any:
    """The decoded JSON of the answer, or its text; logs in again once when the session has expired."""
    data = urllib.parse.urlencode(form).encode() if form else None
    headers = {"Referer": URL}
    for attempt in (0, 1):
        try:
            with _opener.open(urllib.request.Request(URL + "/api/v2/" + path, data, headers), timeout=30) as response:
                raw = response.read()
            return json.loads(raw) if raw[:1] in (b"[", b"{") else raw.decode()
        except urllib.error.HTTPError as e:
            if e.code != 403 or attempt:
                raise
            login = urllib.parse.urlencode({"username": USER, "password": PASSWORD}).encode()
            request = urllib.request.Request(URL + "/api/v2/auth/login", login, headers)
            with _opener.open(request, timeout=30) as response:
                if response.read().strip() != b"Ok.":
                    raise RuntimeError("qBittorrent rejected QBIT_USER / QBIT_PASSWORD") from None
