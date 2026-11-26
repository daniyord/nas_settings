"""ThePornDB's API as media-ingest needs it for the adult library: movies, the search within a site or across all of
it, sites and performers. Answers are cached on disk like TMDB's, a search's for a day only, as new releases show up
in it."""

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

API = "https://api.theporndb.net"
TOKEN = os.environ.get("TPDB_API_TOKEN", "")
CACHE_DIR = os.path.join(os.environ.get("DATA_DIR", "/data"), "tpdb-cache")
CACHE_DAYS = 30
SEARCH_CACHE_DAYS = 1
PAUSE = 0.2
# A search across all of ThePornDB (no site) takes up to minutes
TIMEOUT = 300

_lock = threading.Lock()
_last = 0.0


class NotFound(Exception):
    pass


def _cache_file(url):
    return os.path.join(CACHE_DIR, hashlib.sha1(url.encode()).hexdigest() + ".json")


def _fetch(url, headers):
    global _last
    for attempt in range(4):
        with _lock:
            wait = _last + PAUSE - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _last = time.monotonic()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=TIMEOUT) as response:
                return response.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound(url) from e
            if e.code != 429 and e.code < 500 or attempt == 3:
                raise
            time.sleep(int(e.headers.get("Retry-After") or 2**attempt))
        except urllib.error.URLError:
            if attempt == 3:
                raise
            time.sleep(2**attempt)
    raise AssertionError("unreachable")


def get(path, cache_days=CACHE_DAYS, **params) -> Any:
    """The "data" of the answer for /<path>; a cached one if it is younger than cache_days. NotFound for a 404."""
    url = f"{API}/{path}" + ("?" + urllib.parse.urlencode(sorted(params.items())) if params else "")
    cached = _cache_file(url)
    try:
        if time.time() - os.path.getmtime(cached) < cache_days * 86400:
            with open(cached, encoding="utf-8") as f:
                return json.load(f)["data"]
    except OSError:
        pass
    raw = _fetch(url, {"Accept": "application/json", "Authorization": f"Bearer {TOKEN}"})
    os.makedirs(CACHE_DIR, exist_ok=True)
    temporary = cached + ".tmp"
    with open(temporary, "wb") as f:
        f.write(raw)
    os.replace(temporary, cached)
    return json.loads(raw)["data"]


def movie(ref):
    """A movie by its UUID, slug or numeric id, with its site and performers."""
    return get(f"movies/{urllib.parse.quote(str(ref))}")


def search(query, site=None):
    """Movies whose title matches, within a site (by name) or, much slower, across all of ThePornDB."""
    params: dict[str, Any] = {"q": query, "per_page": 50}
    if site:
        params["site"] = site
    return get("movies", SEARCH_CACHE_DAYS, **params)


def sites(query):
    return get("sites", SEARCH_CACHE_DAYS, q=query, per_page=50)


def performer(performer_id):
    """A performer by the id of their main entry (a movie's performers are a site's entries, with it as "parent")."""
    return get(f"performers/{urllib.parse.quote(str(performer_id))}")


def cover(found):
    """The URL of a movie's cover, or None: some entries only link a dead studio image."""
    return (found.get("posters") or {}).get("large") or found.get("poster") or None


def backdrop(found):
    return (found.get("background") or {}).get("large") or None


def people(found):
    """(id, name, photo URL) of a movie's performers in its order, by their main entry: a site's entry often has
    neither a photo nor the usual spelling of the name ("Nicky Hunter" for Nicki Hunter)."""
    result = []
    for entry in found.get("performers") or []:
        main = entry.get("parent") or entry
        if main.get("id") and main.get("name") and all(main["id"] != p[0] for p in result):
            result.append((main["id"], main["name"].strip(), main.get("image") or None))
    return result


def download(url, destination):
    """Saves an image next to its final name and renames it there, so Jellyfin never reads half a file."""
    raw = _fetch(url, {})
    temporary = destination + ".part"
    with open(temporary, "wb") as f:
        f.write(raw)
    os.replace(temporary, destination)
