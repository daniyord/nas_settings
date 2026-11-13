# Matches new items in the adult Jellyfin library to ThePornDB movies. The plugin's own search
# ignores the studio and picks random scenes, so items are matched here by title within the
# studio of their folder (<library>/<Studio>/<Title>.mp4) and runtime, and the plugin is then
# pointed at the right ThePornDB id. Items that can't be matched with confidence are logged
# and left alone; locked items are never touched. A status page on PORT shows what was matched
# and takes per-item keywords (extra titles to search and compare) to retry a failed item with.
# What can't be rebuilt from Jellyfin (when and how a movie was matched, skips, keywords and the
# history of every action) lives in a SQLite database in DATA_DIR.

import difflib
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

JELLYFIN_URL = os.environ["JELLYFIN_URL"].rstrip("/")
JELLYFIN_API_KEY = os.environ["JELLYFIN_API_KEY"]
LIBRARY = os.environ.get("LIBRARY", "Порно")
INTERVAL = int(os.environ.get("INTERVAL", "300"))
PORT = int(os.environ.get("PORT", "8097"))
DATA_DIR = os.environ.get("DATA_DIR", "/data")
# ThePornDB names a studio's sites differently ("Wicked", "Wicked Pictures", ...); these collapse to one name
MERGE_STUDIOS = [s.strip() for s in os.environ.get("MERGE_STUDIOS", "").split(",") if s.strip()]
RETRY_AFTER = 24 * 3600
MIN_SIMILARITY = 0.9
MAX_RUNTIME_OFF_MINUTES = 15
# Gives the plugin's refresh time to write the NFO and artwork next to the file before it is moved
ORGANIZE_AFTER = 120
# A movie costs 10-20 ThePornDB requests; capping a run keeps a large import from holding up the
# moves and studio merges of the rest of the library, the next run starts right away
MAX_LOOKUPS_PER_RUN = 20
STALE_RESCAN = 300
SETTLE_AFTER = 120
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# Like Jellyfin's [imdbid-tt...]: a ThePornDB (or TMDb) id in a file or folder name pins the match, no search involved
ID_TAG = re.compile(r"\s*\[(tpdbid-" + UUID + r"|tmdbid-\d+)\]", re.I)
# A keyword that is a ThePornDB movie link (uuid, slug or numeric id) or a bare uuid pins the match too
TPDB_REF = re.compile(r"(?:https?://)?(?:www\.)?theporndb\.net/movies/([\w-]+)/?|(" + UUID + ")", re.I)
TMDB_REF = re.compile(r"(?:https?://)?(?:www\.)?themoviedb\.org/movie/(\d+)[\w-]*/?(?:\?.*)?", re.I)
# TMDb is the last resort, searched only when ThePornDB has nothing; unset turns it off. Either the v3
# API key or the v4 read access token (a JWT)
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".wmv", ".m4v", ".mov", ".mpg", ".mpeg", ".ts", ".webm"}

# In memory only: rebuilt from Jellyfin and the database on every run
status = {}
seen_matched_at = {}
missing_since = {}
last_rescan = [0.0]
priority = set()
lock = threading.RLock()
wake = threading.Event()
last_run = {"started": None, "finished": None, "error": None, "interval": INTERVAL, "queued": 0}

os.makedirs(DATA_DIR, exist_ok=True)
db = sqlite3.connect(os.path.join(DATA_DIR, "state.db"), check_same_thread=False, isolation_level=None)
db.row_factory = sqlite3.Row
db.executescript("""
PRAGMA journal_mode = WAL;
-- Keyed by file path rather than Jellyfin item id: moving a movie gives it a new item id
CREATE TABLE IF NOT EXISTS movies (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    keywords TEXT NOT NULL DEFAULT '[]',
    tpdb_id TEXT,
    matched_at REAL,
    matched_by TEXT,
    skipped_at REAL,
    skip_reason TEXT,
    candidates TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    movie_id INTEGER NOT NULL REFERENCES movies(id),
    at REAL NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_movie ON events(movie_id, at);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
""")


def meta(key, value=None):
    with lock:
        if value is not None:
            db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
        row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row and row["value"]


def movie(path):
    with lock:
        db.execute("INSERT OR IGNORE INTO movies (path) VALUES (?)", (path,))
        return db.execute("SELECT * FROM movies WHERE path = ?", (path,)).fetchone()


def update_movie(path, **fields):
    with lock:
        movie(path)
        db.execute(f"UPDATE movies SET {', '.join(f'{k} = ?' for k in fields)} WHERE path = ?", (*fields.values(), path))


def event(path, kind, detail, label):
    print(f"{kind} {label}: {detail}", flush=True)
    with lock:
        db.execute("INSERT INTO events (movie_id, at, kind, detail) VALUES (?, ?, ?, ?)", (movie(path)["id"], time.time(), kind, detail))


def import_keywords_json():
    path = os.path.join(DATA_DIR, "keywords.json")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for movie_path, keywords in json.load(f).items():
            update_movie(movie_path, keywords=json.dumps(keywords, ensure_ascii=False))
    os.replace(path, path + ".imported")


import_keywords_json()


def request(url, headers, method="GET", body=None):
    data = None if body is None else json.dumps(body).encode()
    headers = {**headers, "Accept": "application/json", "Content-Type": "application/json", "User-Agent": "jellyfin-tpdb-match"}
    for attempt in range(6):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data, headers, method=method), timeout=60) as response:
                raw = response.read()
            return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code not in (429, 502, 503, 504) or attempt == 5:
                raise
            retry_after = e.headers.get("Retry-After", "")
            time.sleep(int(retry_after) if retry_after.isdigit() else 2 ** attempt * 5)


def jellyfin(path, method="GET", body=None, **params):
    query = "?" + urllib.parse.urlencode(params) if params else ""
    return request(JELLYFIN_URL + path + query, {"Authorization": f'MediaBrowser Token="{JELLYFIN_API_KEY}"'}, method, body)


def tpdb(token, path, **params):
    return request(f"https://api.theporndb.net/{path}?" + urllib.parse.urlencode(params), {"Authorization": f"Bearer {token}"})["data"]


def tpdb_movie(token, ref):
    try:
        return tpdb(token, f"movies/{urllib.parse.quote(ref)}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def tmdb(path, **params):
    headers = {}
    if TMDB_API_KEY.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {TMDB_API_KEY}"
    else:
        params["api_key"] = TMDB_API_KEY
    return request(f"https://api.themoviedb.org/3/{path}?" + urllib.parse.urlencode({"language": "en-US", **params}), headers)


def tmdb_movie(tmdb_id):
    try:
        return tmdb(f"movie/{tmdb_id}", append_to_response="credits")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


# The id stored for a match and compared with folder tags: ThePornDB's provider id, or tmdb/<id>
def tag_ref(tag):
    kind, _, value = tag.group(1).lower().partition("-")
    return f"movies/{value}" if kind == "tpdbid" else f"tmdb/{value}"


def tmdb_candidate(movie, similarity):
    companies = ", ".join(c["name"] for c in movie.get("production_companies", [])) or "?"
    return {"title": movie["title"], "site": f"{companies} (TMDb)", "year": int(movie["release_date"][:4]) if movie.get("release_date") else None,
            "minutes": movie.get("runtime") or None, "similarity": similarity}


# TMDb has few adult movies and lists production companies rather than sites, so a match still has to name
# the folder's studio among them: without that, a parody would match the mainstream movie of the same title
def find_tmdb_match(title, studio, year, minutes, keywords):
    texts = [title, *keywords] + ([f"{title} {' '.join(keywords)}"] if keywords else [])
    found = {}
    for query in {title.replace(" - ", ": "), title.split(" - ")[0], *texts}:
        for movie in tmdb("search/movie", query=query, include_adult="true")["results"]:
            found[movie["id"]] = movie
    similar = []
    for movie in found.values():
        names = {movie["title"], movie.get("original_title") or movie["title"]}
        similarity = round(max(difflib.SequenceMatcher(None, norm(t), norm(n)).ratio() for t in texts for n in names), 2)
        if similarity >= MIN_SIMILARITY:
            similar.append((similarity, movie["id"]))
    if not similar:
        return None, "no title match on TMDb", []

    scored = []
    candidates = []
    for similarity, tmdb_id in sorted(similar, reverse=True)[:10]:
        movie = tmdb_movie(tmdb_id)
        if not movie:
            continue
        candidates.append(tmdb_candidate(movie, similarity))
        movie_year = int(movie["release_date"][:4]) if movie.get("release_date") else None
        off = abs(movie["runtime"] - minutes) if movie.get("runtime") and minutes else None
        companies = movie.get("production_companies", [])
        if any(same_studio(studio, c["name"]) for c in companies):
            scored.append((similarity, movie_year, off, movie, True))
        # Many adult entries on TMDb name no company at all; those can't be checked against the studio, so they
        # need the exact title and a runtime that agrees instead
        elif not companies and movie.get("adult") and similarity == 1 and off is not None and off <= MAX_RUNTIME_OFF_MINUTES:
            scored.append((similarity, movie_year, off, movie, False))
    candidates = candidates[:3]
    if not scored:
        return None, "no TMDb title match within the studio", candidates
    if any(s[4] for s in scored):
        scored = [s for s in scored if s[4]]
    best_similarity = max(s[0] for s in scored)
    scored = [s for s in scored if s[0] == best_similarity]
    if year and any(s[1] == year for s in scored):
        scored = [s for s in scored if s[1] == year]
    scored.sort(key=lambda s: s[2] if s[2] is not None else float("inf"))
    best = scored[0]
    if best[2] is None:
        return (best[3], None, []) if len(scored) == 1 else (None, f"{len(scored)} TMDb candidates and no runtime to tell them apart", candidates)
    if best[2] > MAX_RUNTIME_OFF_MINUTES:
        return None, f"TMDb runtime differs by {best[2]:.0f} min", candidates
    if len(scored) > 1 and scored[1][2] is not None and scored[1][2] - best[2] < 5:
        return None, f"{len(scored)} TMDb candidates with about the same runtime", candidates
    return best[3], None, []


# The library has only ThePornDB's metadata fetcher (TMDb's would search every movie by name), so a TMDb
# match is written into the item here and locked, or the plugin's next refresh would search it by name again
def apply_tmdb(item_id, full, movie):
    full["Name"] = movie["title"]
    full["OriginalTitle"] = movie.get("original_title") or movie["title"]
    full["Overview"] = movie.get("overview") or ""
    full["Taglines"] = [movie["tagline"]] if movie.get("tagline") else []
    full["PremiereDate"] = f"{movie['release_date']}T00:00:00.0000000Z" if movie.get("release_date") else None
    full["ProductionYear"] = int(movie["release_date"][:4]) if movie.get("release_date") else None
    full["Genres"] = [g["name"] for g in movie.get("genres", [])]
    full["Tags"] = []
    full["Studios"] = [{"Name": n} for n in merged_studios([c["name"] for c in movie.get("production_companies", [])])]
    full["CommunityRating"] = movie.get("vote_average") if movie.get("vote_count") else None
    credits = movie.get("credits") or {}
    full["People"] = [{"Name": p["name"], "Role": p.get("character") or "", "Type": "Actor"} for p in credits.get("cast", [])[:30]]
    full["People"] += [{"Name": p["name"], "Type": "Director"} for p in credits.get("crew", []) if p.get("job") == "Director"]
    full["ProviderIds"] = {"Tmdb": str(movie["id"]), **({"Imdb": movie["imdb_id"]} if movie.get("imdb_id") else {})}
    full["LockData"] = True
    jellyfin(f"/Items/{item_id}", "POST", full)
    # Artwork from an earlier wrong match or a frame grab would otherwise stay next to the new one
    for _ in full.get("BackdropImageTags") or []:
        jellyfin(f"/Items/{item_id}/Images/Backdrop/0", "DELETE")
    for kind, path in (("Primary", movie.get("poster_path")), ("Backdrop", movie.get("backdrop_path"))):
        if path:
            jellyfin(f"/Items/{item_id}/RemoteImages/Download", "POST", Type=kind, ImageUrl=f"https://image.tmdb.org/t/p/original{path}")


def norm(text):
    return re.sub(r"[^a-z0-9]", "", text.lower().replace("&", "and"))


# Prefix rather than substring, so "Wicked" covers "Wicked Pictures" but not "Raw Wicked Love Affair"
def same_studio(folder, name):
    a, b = norm(folder), norm(name)
    return bool(a and b) and (b.startswith(a) or a.startswith(b))


def studio_sites(token, studio, cache):
    if studio not in cache:
        cache[studio] = {studio} | {s["name"] for s in tpdb(token, "sites", q=studio, per_page=50) if same_studio(studio, s["name"])}
    return cache[studio]


def find_match(token, title, studio, year, minutes, keywords, sites):
    # A keyword is compared on its own (a full alternative title) and appended to the file title
    # ("Cinderella" + "XXX Axel Braun Parody"), so either style of hint works
    texts = [title, *keywords] + ([f"{title} {' '.join(keywords)}"] if keywords else [])
    found = {}
    for query in {title.replace(" - ", ": "), title.split(" - ")[0], *texts}:
        for params in [{"q": query, "site": site} for site in sites] + [{"q": query}]:
            for movie in tpdb(token, "movies", per_page=50, **params):
                if same_studio(studio, (movie.get("site") or {}).get("name", "")):
                    found[movie["id"]] = movie

    scored = []
    for movie in found.values():
        similarity = round(max(difflib.SequenceMatcher(None, norm(t), norm(movie["title"])).ratio() for t in texts), 2)
        movie_year = int(movie["date"][:4]) if movie.get("date") else None
        movie_minutes = (movie.get("duration") or 0) / 60
        off = abs(movie_minutes - minutes) if movie_minutes and minutes else None
        scored.append((similarity, movie_year, off, movie))

    candidates = [
        {"title": m["title"], "site": m["site"]["name"], "year": y, "minutes": round((m.get("duration") or 0) / 60) or None, "similarity": s}
        for s, y, _, m in sorted(scored, key=lambda s: -s[0])[:3]
    ]
    best_similarity = max((s[0] for s in scored), default=0)
    if best_similarity < MIN_SIMILARITY:
        return None, "no title match within the studio", candidates
    scored = [s for s in scored if s[0] == best_similarity]
    if year and any(s[1] == year for s in scored):
        scored = [s for s in scored if s[1] == year]
    scored.sort(key=lambda s: s[2] if s[2] is not None else float("inf"))

    best = scored[0]
    if best[2] is None:
        return (best[3], None, []) if len(scored) == 1 else (None, f"{len(scored)} candidates and no runtime to tell them apart", candidates)
    if best[2] > MAX_RUNTIME_OFF_MINUTES:
        return None, f"runtime differs by {best[2]:.0f} min", candidates
    # ThePornDB often lists one release several times (re-releases, "Wicked" vs "Wicked Pictures");
    # candidates this close in runtime are the same movie, so take the original release, but only
    # among those with a cover: some entries only link a dead studio CDN image, and the movie
    # would then get a frame grabbed from the video as its poster
    same_movie = [s for s in scored if s[2] is not None and s[2] - best[2] < 5]
    return min(same_movie, key=lambda s: (not has_cover(s[3]), s[3].get("date") or "9999"))[3], None, []


def has_cover(movie):
    return bool((movie.get("posters") or {}).get("large") or movie.get("poster"))


def merged_studios(names):
    result = []
    for name in names:
        name = next((m for m in MERGE_STUDIOS if same_studio(m, name)), name)
        if name not in result:
            result.append(name)
    return result


# Locks the field, or the next metadata refresh would bring the plugin's site names back
def merge_studios(item, admin, label):
    names = [s["Name"] for s in item.get("Studios", [])]
    merged = merged_studios(names)
    if merged == names:
        return
    full = jellyfin(f"/Items/{item['Id']}", userId=admin["Id"])
    if full.get("LockData"):
        return
    full["Studios"] = [{"Name": n} for n in merged]
    full["LockedFields"] = sorted(set(full.get("LockedFields") or []) | {"Studios"})
    jellyfin(f"/Items/{item['Id']}", "POST", full)
    event(item["Path"], "studios", f"{', '.join(names)} -> {', '.join(merged)}", label)


def sidecars(folder, stem):
    # Names Jellyfin gives the NFO, artwork and subtitles of <stem>.<ext>, plus the generic ones
    # (movie.nfo, poster.jpg, ...) it uses when the video is alone in its folder
    names = os.listdir(folder)
    videos = [os.path.splitext(n)[0] for n in names if os.path.splitext(n)[1].lower() in VIDEO_EXTS]
    own = re.compile(r"(?:-(?:poster|backdrop\d*|fanart\d*|logo|clearlogo|thumb|banner|landscape|clearart|disc|discart)\.(?:jpe?g|png|webp)"
                     r"|(?:\.[\w-]+)*\.(?:nfo|srt|ass|ssa|sub|idx|vtt|sup))", re.I)
    generic = re.compile(r"(?:movie\.nfo|(?:poster|folder|fanart|backdrop\d*|logo|clearlogo|thumb|banner|landscape|clearart|disc|discart)\.(?:jpe?g|png|webp))", re.I)
    result = []
    for name in names:
        if os.path.splitext(name)[1].lower() in VIDEO_EXTS:
            continue
        # The longest matching video stem owns the file, so "Hotel No Tell 2-poster.jpg" stays with part 2
        owners = [v for v in videos if name.lower().startswith(v.lower()) and own.fullmatch(name[len(v):])]
        if (owners and max(owners, key=len) == stem) or (videos == [stem] and generic.fullmatch(name)):
            result.append(name)
    return result


# Alone in its folder, Jellyfin reads and writes movie.nfo, folder.jpg, backdrop.jpg, ... rather than
# <stem>.nfo, <stem>-poster.jpg, ...; keeping the old names would leave stale duplicates next to the new ones
def folder_name(name, stem):
    rest = name[len(stem):] if name.lower().startswith(stem.lower()) else None
    if rest is None:
        return name
    if rest.lower() == ".nfo":
        return "movie.nfo"
    art = re.fullmatch(r"-(\w+)(\.\w+)", rest)
    if art:
        return ("folder" if art.group(1).lower() == "poster" else art.group(1).lower()) + art.group(2).lower()
    return name


# Gives a matched movie its own folder named "<Title (Year)> [tpdbid-<id>]" (or [tmdbid-<id>]): a movie lying directly in its
# studio folder moves there with its sidecar files, a movie already alone in a folder has the tag appended
# Folder name for a movie identified by hand: the file name may well be the wrong title, so Jellyfin's is used
def identified_name(item, folder):
    title = re.sub(r"\s+", " ", re.sub(r'[<>:"/\\|?*]', " ", item.get("Name") or "")).strip(" .")
    if not title:
        title = re.sub(r"\s*\(\d{4}\)$", "", ID_TAG.sub("", folder).strip())
    # ThePornDB lists some titles sorted, as "Ring, the"
    article = re.fullmatch(r"(.+), (the|a|an)", title, re.I)
    if article:
        title = f"{article.group(2).capitalize()} {article.group(1)}"
    year = item.get("ProductionYear") or (re.search(r"\((\d{4})\)", folder) or [None, None])[1]
    return f"{title} ({year})" if year else title


def organize(item, parts, label):
    ids = item.get("ProviderIds", {})
    provider = re.fullmatch(r"movies/(" + UUID + ")", ids.get("ThePornDB", ""), re.I)
    key = f"tpdbid-{provider.group(1).lower()}" if provider else f"tmdbid-{ids['Tmdb']}" if ids.get("Tmdb") else None
    if not key or not os.path.isfile(item["Path"]):
        return
    tag = f"[{key}]"
    folder, filename = os.path.split(item["Path"])
    stem = os.path.splitext(filename)[0]

    if len(parts) == 2:
        year = item.get("ProductionYear")
        base = stem if not year or re.search(r"\(\d{4}\)\s*$", stem) else f"{stem} ({year})"
        source, target = item["Path"], os.path.join(folder, f"{base} {tag}")
        moves = [(item["Path"], os.path.join(target, filename))]
        moves += [(os.path.join(folder, n), os.path.join(target, folder_name(n, stem))) for n in sidecars(folder, stem)]
    elif len(parts) == 3 and ID_TAG.search(parts[1]):
        if ID_TAG.search(parts[1]).group(1).lower() == key:
            return
        source, target = folder, os.path.join(os.path.dirname(folder), f"{identified_name(item, parts[1])} {tag}")
        moves = [(folder, target)]
    elif len(parts) == 3:
        if sum(os.path.splitext(n)[1].lower() in VIDEO_EXTS for n in os.listdir(folder)) != 1:
            return
        source, target = folder, f"{folder} {tag}"
        moves = [(folder, target)]
    else:
        return
    if os.path.exists(target):
        print(f"organize {label}: {target} already exists", flush=True)
        return

    if len(parts) == 2:
        os.mkdir(target)
        parent = os.stat(folder)
        os.chown(target, parent.st_uid, parent.st_gid)
        os.chmod(target, parent.st_mode & 0o7777)
    for src, dst in moves:
        os.rename(src, dst)
    new_path = os.path.join(target, filename)
    with lock:
        db.execute("UPDATE OR REPLACE movies SET path = ? WHERE path = ?", (new_path, item["Path"]))
    jellyfin("/Library/Media/Updated", "POST", {"Updates": [
        {"Path": source, "UpdateType": "Deleted"},
        {"Path": target, "UpdateType": "Created"},
    ]})
    event(new_path, "moved", f"{os.path.relpath(target, os.path.dirname(folder) if len(parts) == 2 else os.path.dirname(os.path.dirname(folder)))}/", label)
    return new_path


# Video file name (lowercased) -> its path, for the movies of a studio already in their own tagged folder
def tagged_files(studio_dir, cache):
    if studio_dir not in cache:
        cache[studio_dir] = {}
        for folder in os.listdir(studio_dir):
            path = os.path.join(studio_dir, folder)
            if ID_TAG.search(folder) and os.path.isdir(path):
                for name in os.listdir(path):
                    if os.path.splitext(name)[1].lower() in VIDEO_EXTS:
                        cache[studio_dir][name.lower()] = os.path.join(path, name)
    return cache[studio_dir]


def record(item, studio, name, state, detail, candidates=()):
    with lock:
        status[item["Id"]] = {
            "id": item["Id"], "path": item["Path"], "studio": studio, "file": name,
            "state": state, "detail": detail, "candidates": list(candidates), "checked": time.time(),
        }


def current_state(item_id):
    with lock:
        return status.get(item_id, {}).get("state")


def run_once():
    plugin = next(p for p in jellyfin("/Plugins") if p["Name"] == "ThePornDB")
    token = jellyfin(f"/Plugins/{plugin['Id']}/Configuration")["MetadataAPIToken"]
    admin = next(u for u in jellyfin("/Users") if u["Policy"]["IsAdministrator"])
    library = next(f for f in jellyfin("/Library/VirtualFolders") if f["Name"] == LIBRARY)
    # Matches that existed before the database did can't be dated
    backfill = not meta("history_since")

    items = jellyfin("/Items", ParentId=library["ItemId"], Recursive="true", IncludeItemTypes="Movie", Fields="Path,ProviderIds,Studios", Limit=100000)["Items"]
    # Classify everything first so the status page shows the whole library before the slow ThePornDB lookups
    todo = []
    next_wake = float("inf")
    stale = float("inf")
    filenames = {}
    for item in items:
        location = next((l for l in library["Locations"] if item["Path"].startswith(l.rstrip("/") + "/")), None)
        parts = item["Path"][len(location.rstrip("/")) + 1:].split("/") if location else []
        if len(parts) < 2:
            record(item, "", os.path.basename(item["Path"]), "ignored", "not inside a studio folder")
            continue
        studio = parts[0]
        name = ID_TAG.sub("", os.path.splitext(parts[-1])[0]).strip()
        label = f"{studio}/{name}"
        # Right after a move Jellyfin still lists the old path; acting on it would create a record for a file that's gone
        if not os.path.isfile(item["Path"]):
            record(item, studio, name, "pending", "waiting for Jellyfin to pick up the move")
            stale = min(stale, missing_since.setdefault(item["Id"], time.time()))
            continue
        # A file copied in again next to the folder its earlier copy was moved into would be matched and moved as
        # a second movie; the copy in the tagged folder is the original, so only the other one is flagged
        if not (len(parts) == 3 and ID_TAG.search(parts[1])):
            original = tagged_files(os.path.join(location, studio), filenames).get(parts[-1].lower())
            if original:
                same = os.path.getsize(item["Path"]) == os.path.getsize(original)
                detail = f"same file as {os.path.relpath(original, location)}" + ("" if same else " (different size)")
                if current_state(item["Id"]) != "duplicate":
                    event(item["Path"], "duplicate", detail, label)
                record(item, studio, name, "duplicate", detail)
                status[item["Id"]]["original"] = original
                continue
        # ctime rather than mtime: copy tools restore the source's mtime, but every write moves ctime. A file still
        # being copied reports a short runtime, and moving it would pull it from under the copy
        settles = os.stat(item["Path"]).st_ctime + SETTLE_AFTER
        if time.time() < settles:
            record(item, studio, name, "pending", "file is still being copied")
            next_wake = min(next_wake, settles)
            continue
        tag = ID_TAG.search(item["Path"])
        provider = item.get("ProviderIds", {}).get("ThePornDB")
        row = movie(item["Path"])
        # A tagged item is matched only once it points at the tagged id; retagging a folder re-points it. When the
        # folder still has the id last seen in Jellyfin but Jellyfin now has another one, the movie was identified
        # again by hand there, so the folder follows Jellyfin instead
        tagged_id = tag_ref(tag) if tag else None
        identified = bool(tag and provider and row["tpdb_id"] and row["tpdb_id"].lower() == tagged_id and provider.lower() != tagged_id)
        if provider and (identified or (provider.lower() == tagged_id if tag else any(same_studio(studio, s["Name"]) for s in item.get("Studios", [])))):
            if identified:
                # Retagged right away and the new id stored only once that worked: a stored id that no longer
                # matches the tag would read as a folder retagged by hand on the next run, undoing the fix
                new_path = organize(item, parts, label)
                if new_path:
                    update_movie(new_path, tpdb_id=provider, matched_at=time.time(), matched_by="jellyfin", skipped_at=None)
                    event(new_path, "matched", f"{item['Name']} ({provider}, identified in Jellyfin)", label)
                record(item, studio, name, "matched", item["Name"])
                continue
            if row["tpdb_id"] != provider:
                if backfill:
                    update_movie(item["Path"], tpdb_id=provider, matched_by="unknown", skipped_at=None)
                else:
                    update_movie(item["Path"], tpdb_id=provider, matched_at=time.time(), matched_by="plugin", skipped_at=None)
                    event(item["Path"], "matched", f"{item['Name']} ({provider}, by the Jellyfin plugin)", label)
            if current_state(item["Id"]) != "matched":
                record(item, studio, name, "matched", item["Name"])
            merge_studios(item, admin, label)
            # Counted from when this process first saw the item matched, as the plugin also identifies new
            # items on its own and is still writing their artwork right after
            due = seen_matched_at.setdefault(item["Id"], time.time()) + ORGANIZE_AFTER
            if time.time() > due:
                organize(item, parts, label)
            elif len(parts) == 2 or (len(parts) == 3 and not ID_TAG.search(parts[1])):
                next_wake = min(next_wake, due)
            continue
        tmdb_id = item.get("ProviderIds", {}).get("Tmdb")
        if not provider and tmdb_id and row["tpdb_id"] == f"tmdb/{tmdb_id}":
            # The move gives the movie a new Jellyfin item that comes back unlocked and without its cast, so the
            # metadata is written once more; only right after the move, so unlocking it later to edit by hand sticks
            with lock:
                last = db.execute("SELECT kind FROM events WHERE movie_id = ? ORDER BY at DESC, id DESC LIMIT 1", (row["id"],)).fetchone()
            if TMDB_API_KEY and last and last["kind"] == "moved":
                full = jellyfin(f"/Items/{item['Id']}", userId=admin["Id"])
                found = None if full.get("LockData") else tmdb_movie(tmdb_id)
                if found:
                    apply_tmdb(item["Id"], full, found)
                    event(item["Path"], "matched", f"{found['title']} (tmdb/{tmdb_id}, written again after the move)", label)
            if current_state(item["Id"]) != "matched":
                record(item, studio, name, "matched", item["Name"])
            due = seen_matched_at.setdefault(item["Id"], time.time()) + ORGANIZE_AFTER
            if time.time() > due:
                organize(item, parts, label)
            elif len(parts) == 2 or (len(parts) == 3 and not ID_TAG.search(parts[1])):
                next_wake = min(next_wake, due)
            continue
        if row["skipped_at"] and time.time() - row["skipped_at"] < RETRY_AFTER:
            if current_state(item["Id"]) != "skipped":
                record(item, studio, name, "skipped", row["skip_reason"], json.loads(row["candidates"]))
            continue
        if current_state(item["Id"]) != "locked":
            record(item, studio, name, "pending", "waiting for the matcher")
        todo.append((item, studio, name, label, tag))

    if backfill:
        meta("history_since", str(time.time()))
    # Jellyfin sometimes keeps a moved movie at its old path, showing artwork that's gone, until the library is rescanned
    for gone in set(missing_since) - {item["Id"] for item in items if not os.path.isfile(item["Path"])}:
        del missing_since[gone]
    if time.time() - stale > STALE_RESCAN and time.time() - last_rescan[0] > STALE_RESCAN:
        last_rescan[0] = time.time()
        print("rescan: Jellyfin still lists moved files at their old paths", flush=True)
        jellyfin(f"/Items/{library['ItemId']}/Refresh", "POST", Recursive="true", MetadataRefreshMode="Default", ImageRefreshMode="Default")
    if stale < float("inf"):
        next_wake = min(next_wake, max(stale, last_rescan[0]) + STALE_RESCAN)
    with lock:
        for gone in set(status) - {item["Id"] for item in items}:
            del status[gone]

    sites_cache = {}
    lookups = 0
    # Movies retried from the status page go first, ahead of a large import
    todo.sort(key=lambda t: t[0]["Path"] not in priority)
    last_run["queued"] = len(todo)
    for n, (item, studio, name, label, tag) in enumerate(todo):
        last_run["queued"] = len(todo) - n
        full = jellyfin(f"/Items/{item['Id']}", userId=admin["Id"])
        if full.get("LockData"):
            record(item, studio, name, "locked", "metadata is locked in Jellyfin")
            continue
        if lookups == MAX_LOOKUPS_PER_RUN:
            return time.time()
        lookups += 1
        priority.discard(item["Path"])

        year_match = re.search(r"\((\d{4})\)\s*$", name)
        year = int(year_match.group(1)) if year_match else None
        title = re.sub(r"\s*\(\d{4}\)\s*$", "", name)
        minutes = (full.get("RunTimeTicks") or 0) / 600_000_000
        keywords = json.loads(movie(item["Path"])["keywords"])

        refs = [m.group(1) or m.group(2) for k in keywords if (m := TPDB_REF.fullmatch(k.strip()))]
        tmdb_refs = [m.group(1) for k in keywords if (m := TMDB_REF.fullmatch(k.strip()))]
        on_tmdb = False
        if (tag and tag_ref(tag).startswith("tmdb/")) or (not tag and not refs and tmdb_refs):
            on_tmdb = True
            ref = tag_ref(tag)[len("tmdb/"):] if tag else tmdb_refs[0]
            matched_by = "tmdb-tag" if tag else "tmdb-link"
            found, reason, candidates = None, "TMDB_API_KEY is not set", []
            if TMDB_API_KEY:
                found, reason = tmdb_movie(ref), None
                if not found:
                    reason = f"TMDb has no movie {ref}"
        elif tag or refs:
            ref = tag_ref(tag)[len("movies/"):] if tag else refs[0]
            matched_by = "tag" if tag else "link"
            found, reason, candidates = tpdb_movie(token, ref), None, []
            if not found:
                reason = f"ThePornDB has no movie {ref}"
        else:
            matched_by = "keywords" if keywords else "title"
            found, reason, candidates = find_match(token, title, studio, year, minutes, keywords, studio_sites(token, studio, sites_cache))
            if not found and TMDB_API_KEY:
                tmdb_found, tmdb_reason, tmdb_candidates = find_tmdb_match(title, studio, year, minutes, keywords)
                if tmdb_found:
                    found, on_tmdb, matched_by = tmdb_found, True, "tmdb"
                else:
                    reason, candidates = f"{reason}; {tmdb_reason}", candidates + tmdb_candidates
        if not found:
            update_movie(item["Path"], skipped_at=time.time(), skip_reason=reason, candidates=json.dumps(candidates, ensure_ascii=False))
            event(item["Path"], "skipped", reason, label)
            record(item, studio, name, "skipped", reason, candidates)
            continue

        if on_tmdb:
            provider = f"tmdb/{found['id']}"
            apply_tmdb(item["Id"], full, found)
            match = f"{found['title']} (TMDb, {(found.get('release_date') or '')[:4]})"
        else:
            provider = f"movies/{found['id']}"
            full["ProviderIds"] = {"ThePornDB": provider}
            jellyfin(f"/Items/{item['Id']}", "POST", full)
            jellyfin(f"/Items/{item['Id']}/Refresh", "POST", MetadataRefreshMode="FullRefresh", ImageRefreshMode="FullRefresh", ReplaceAllMetadata="true", ReplaceAllImages="true")
            match = f"{found['title']} ({found['site']['name']}, {(found.get('date') or '')[:4]})"
        update_movie(item["Path"], tpdb_id=provider, matched_at=time.time(), matched_by=matched_by, skipped_at=None, skip_reason=None, candidates="[]")
        event(item["Path"], "matched", f"{match} ({provider}, by {matched_by})", label)
        seen_matched_at[item["Id"]] = time.time()
        next_wake = min(next_wake, time.time() + ORGANIZE_AFTER)
        record(item, studio, name, "matched", match)

    last_run["queued"] = 0
    return next_wake


def matcher_loop():
    while True:
        wake.clear()
        last_run["started"] = time.time()
        next_wake = float("inf")
        try:
            next_wake = run_once()
            last_run["error"] = None
        except Exception as e:
            print(f"error: {e!r}", flush=True)
            last_run["error"] = repr(e)
        last_run["finished"] = time.time()
        # Wake early for a movie waiting to be moved or a queue left over, rather than after a full interval
        wake.wait(max(1, min(INTERVAL, next_wake - time.time() + 1)))


app = FastAPI(title="jellyfin-tpdb-match")


class Retry(BaseModel):
    keywords: list[str] = []


@app.get("/")
def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))


@app.get("/api/status")
def api_status():
    with lock:
        rows = {r["path"]: r for r in db.execute("SELECT path, keywords, matched_at, matched_by FROM movies")}
        items = []
        for s in status.values():
            row = rows.get(s["path"])
            items.append({
                **s,
                "keywords": json.loads(row["keywords"]) if row else [],
                "matched_at": row["matched_at"] if row and s["state"] == "matched" else None,
                "matched_by": row["matched_by"] if row and s["state"] == "matched" else None,
            })
    return {"run": last_run, "items": items}


@app.get("/api/items/{item_id}/history")
def history(item_id: str):
    with lock:
        entry = status.get(item_id)
        if not entry:
            raise HTTPException(404, "unknown item")
        events = db.execute(
            "SELECT e.at, e.kind, e.detail FROM events e JOIN movies m ON m.id = e.movie_id WHERE m.path = ? ORDER BY e.at DESC, e.id DESC",
            (entry["path"],),
        ).fetchall()
    return [dict(e) for e in events]


@app.post("/api/run")
def run_now():
    wake.set()
    return {"run": last_run}


@app.post("/api/items/{item_id}/retry")
def retry(item_id: str, body: Retry):
    with lock:
        entry = status.get(item_id)
        if not entry:
            raise HTTPException(404, "unknown item")
        if entry["state"] not in ("skipped", "pending"):
            raise HTTPException(409, f"item is {entry['state']}")
        keywords = [k.strip() for k in body.keywords if k.strip()]
        update_movie(entry["path"], keywords=json.dumps(keywords, ensure_ascii=False), skipped_at=None)
        event(entry["path"], "retry", "; ".join(keywords) or "no keywords", f"{entry['studio']}/{entry['file']}")
        entry.update(state="pending", detail="queued for retry", checked=time.time())
        priority.add(entry["path"])
    wake.set()
    return {**entry, "keywords": keywords}


@app.delete("/api/items/{item_id}")
def delete_duplicate(item_id: str):
    with lock:
        entry = status.get(item_id)
        if not entry:
            raise HTTPException(404, "unknown item")
        if entry["state"] != "duplicate":
            raise HTTPException(409, f"item is {entry['state']}")
        path, original = entry["path"], entry["original"]
        # Only ever delete the copy while the original is still in place, and never anything inside a tagged folder
        if not os.path.isfile(original) or not os.path.isfile(path) or ID_TAG.search(os.path.dirname(path)):
            raise HTTPException(409, "original or copy changed since the last run")
        folder = os.path.dirname(path)
        extra = sidecars(folder, os.path.splitext(os.path.basename(path))[0])
        for name in [os.path.basename(path), *extra]:
            os.remove(os.path.join(folder, name))
        event(path, "deleted", f"duplicate of {original}" + (f", with {', '.join(extra)}" if extra else ""), f"{entry['studio']}/{entry['file']}")
        del status[item_id]
    jellyfin("/Library/Media/Updated", "POST", {"Updates": [{"Path": path, "UpdateType": "Deleted"}]})
    return {"deleted": [os.path.basename(path), *extra]}


if __name__ == "__main__":
    threading.Thread(target=matcher_loop, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
