"""TMDB's API as the services need it: Bulgarian metadata, images picked Bulgarian text first, then without text,
then English. Answers are cached on disk, so walking the whole library again costs no requests."""

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

API = "https://api.themoviedb.org/3"
IMAGES = "https://image.tmdb.org/t/p"
# Either a v3 API key or a v4 read access token (a JWT, so it starts with eyJ)
API_KEY = os.environ.get("TMDB_API_KEY", "")
LANGUAGE = "bg-BG"
IMAGE_LANGUAGES = ("bg", None, "en")
CACHE_DIR = os.path.join(os.environ.get("DATA_DIR", "/data"), "tmdb-cache")
CACHE_DAYS = 30
# TMDB allows about 50 requests a second; this keeps a full library walk well below that
PAUSE = 0.1

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
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
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


def get(path, **params) -> Any:
    """The decoded answer for /3/<path>; a cached one if it is younger than CACHE_DAYS. NotFound for a 404."""
    params = {"language": LANGUAGE, **params}
    url = f"{API}/{path}?" + urllib.parse.urlencode(sorted(params.items()))
    cached = _cache_file(url)
    try:
        if time.time() - os.path.getmtime(cached) < CACHE_DAYS * 86400:
            with open(cached, encoding="utf-8") as f:
                return json.load(f)
    except OSError:
        pass
    headers = {"Accept": "application/json"}
    if API_KEY.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {API_KEY}"
        request_url = url
    else:
        request_url = url + "&" + urllib.parse.urlencode({"api_key": API_KEY})
    raw = _fetch(request_url, headers)
    os.makedirs(CACHE_DIR, exist_ok=True)
    temporary = cached + ".tmp"
    with open(temporary, "wb") as f:
        f.write(raw)
    os.replace(temporary, cached)
    return json.loads(raw)


def search(kind, query, year=None):
    """Results for a "movie" or "tv" search, best first as TMDB orders them."""
    params: dict[str, Any] = {"query": query, "include_adult": "true"}
    if year:
        params["year" if kind == "movie" else "first_air_date_year"] = year
    return get(f"search/{kind}", **params)["results"]


def find_imdb(imdb_id):
    """The TMDB ("movie" or "tv", id) of an IMDb id, or None."""
    found = get(f"find/{imdb_id}", external_source="imdb_id")
    for kind in ("movie", "tv"):
        if found.get(f"{kind}_results"):
            return kind, found[f"{kind}_results"][0]["id"]
    return None


def _image_params():
    return {"include_image_language": ",".join(language or "null" for language in IMAGE_LANGUAGES)}


def movie(tmdb_id):
    return get(
        f"movie/{tmdb_id}",
        append_to_response="credits,images,release_dates,external_ids,translations",
        **_image_params(),
    )


def tv(tmdb_id):
    return get(
        f"tv/{tmdb_id}",
        append_to_response="aggregate_credits,images,content_ratings,external_ids,translations",
        **_image_params(),
    )


def season(tmdb_id, number):
    return get(f"tv/{tmdb_id}/season/{number}", append_to_response="images", **_image_params())


def episode(tmdb_id, season_number, number):
    return get(
        f"tv/{tmdb_id}/season/{season_number}/episode/{number}",
        append_to_response="images,external_ids,translations",
        **_image_params(),
    )


def collection(tmdb_id):
    return get(f"collection/{tmdb_id}", append_to_response="images", **_image_params())


def person(tmdb_id):
    return get(f"person/{tmdb_id}")


def credits(kind, tmdb_id):
    """Cast and crew of a "movie" or "tv" with the people's names in English: with bg-BG TMDB writes them in Cyrillic
    where it can, while the NFOs Jellyfin wrote have them in Latin, and Jellyfin tells people apart by name only."""
    return get(f"{kind}/{tmdb_id}/{'credits' if kind == 'movie' else 'aggregate_credits'}", language="en-US")


def episode_credits(tmdb_id, season_number, number):
    """An episode's cast and guest stars with their names in English, as credits() gives a title's."""
    return get(f"tv/{tmdb_id}/season/{season_number}/episode/{number}/credits", language="en-US")


def bulgarian_title(details):
    """The title TMDB has in Bulgarian, or None: with language=bg it falls back to the original title, so only the
    translations tell whether there is one."""
    for translation in details.get("translations", {}).get("translations", []):
        if translation.get("iso_639_1") == "bg":
            data = translation.get("data") or {}
            title = (data.get("title") or data.get("name") or "").strip()
            if title:
                return title
    if details.get("original_language") == "bg":
        return details.get("original_title") or details.get("original_name")
    return None


def pick_image(images, languages=IMAGE_LANGUAGES):
    """The file_path of the best of a list of TMDB images: by `languages` (None is an image without text; by default
    Bulgarian text, then no text, then English), then any; the most voted within each."""

    def rank(image):
        language = image.get("iso_639_1")
        order = languages.index(language) if language in languages else len(languages)
        return order, -(image.get("vote_average") or 0), -(image.get("vote_count") or 0)

    return min(images, key=rank)["file_path"] if images else None


def image_url(file_path, size="original"):
    return f"{IMAGES}/{size}{file_path}"


def download_image(file_path, destination, size="original"):
    """Saves an image next to its final name and renames it there, so Jellyfin never reads half a file."""
    raw = _fetch(image_url(file_path, size), {})
    temporary = destination + ".part"
    with open(temporary, "wb") as f:
        f.write(raw)
    os.replace(temporary, destination)
