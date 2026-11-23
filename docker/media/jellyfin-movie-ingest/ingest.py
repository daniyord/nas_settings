# Ingests new movies into the Jellyfin film libraries. A movie that isn't laid out like the rest
# (docs/PLAN_jellyfin_library.md: <Title> (<Year>) [imdbid-tt...]/<same name>.<ext>) is renamed after
# the match once Jellyfin has identified it and written its NFO: the folder, the video, the artwork and
# the subtitles, which also get a language when they have none. Every identified movie without one gets
# the Bulgarian ForcedSortName of the other movies. A match whose title or year doesn't look like the
# file's name waits for a confirmation on the status page on PORT. A series in a release folder (one with
# a season in its name, or any series folder that came from qBittorrent) is organized the same way into <Title> (<Year>) [imdbid-tt...]/Season NN/SxxEyy - <Title>,
# or added to the series' folder when there is one, once every episode's SxxEyy matches Jellyfin's. Separately, the text subtitles of every
# organized movie are aligned to the speech in the video (ffsubsync); a shift of up to MAX_AUTO_OFFSET, alone or with
# a usual frame rate conversion, is fixed right away, anything else is fixed from the status page, keeping the
# original as <subtitle>.orig either way. The history of
# every action, the confirmations and the subtitle checks live in a SQLite database in DATA_DIR. A finished
# torrent in a qBittorrent category named after a library is moved into that library's folder on the same
# disk (a rename, never a copy) and dropped from qBittorrent, which is where the steps above take over.
# That category is kept pointed at the download folder on the library's disk with the most free space; a
# downloading episode of a series that's already organized is moved to the download folder on that series' disk.

import collections
import difflib
import hashlib
import http.cookiejar
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pysubs2

import bg_subtitles
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

JELLYFIN_URL = os.environ["JELLYFIN_URL"].rstrip("/")
JELLYFIN_API_KEY = os.environ["JELLYFIN_API_KEY"]
LIBRARIES = [s.strip() for s in os.environ.get("LIBRARIES", "Филми,Филми - детски,Филми - български,Филми - руски").split(",") if s.strip()]
# Only series in a release folder (its name has a season in it) are organized; the rest only get their subtitles checked
SERIES_LIBRARIES = [s.strip() for s in os.environ.get("SERIES_LIBRARIES", "Сериали,Сериали - детски,Сериали - български").split(",") if s.strip()]
INTERVAL = int(os.environ.get("INTERVAL", "300"))
PORT = int(os.environ.get("PORT", "8098"))
DATA_DIR = os.environ.get("DATA_DIR", "/data")
# Finished torrents whose qBittorrent category is the name of one of LIBRARIES or SERIES_LIBRARIES are moved
# into it; empty turns this off
QBIT_URL = os.environ.get("QBIT_URL", "").rstrip("/")
QBIT_USER = os.environ.get("QBIT_USER", "")
QBIT_PASSWORD = os.environ.get("QBIT_PASSWORD", "")
DOWNLOAD_INTERVAL = int(os.environ.get("DOWNLOAD_INTERVAL", "60"))


def path_map(text):
    return [tuple(p.strip().split("=", 1)) for p in text.split(",") if "=" in p]


# qBittorrent's paths as seen here, e.g. /downloads=/volume1/Downloads
QBIT_PATHS = path_map(os.environ.get("QBIT_PATHS", ""))
# qBittorrent's download folder on each disk; a library's category is pointed at the one with the most free space
DOWNLOAD_DIRS = [s.strip() for s in os.environ.get("DOWNLOAD_DIRS", "").split(",") if s.strip()]
# Jellyfin's library folders as seen through the whole-volume mounts: a move within one mount is a rename,
# while across two mounts (even of the same disk) it would be a copy
VOLUME_PATHS = path_map(os.environ.get("VOLUME_PATHS", ""))
# ctime rather than mtime: copy tools restore the source's mtime, but every write moves ctime
SETTLE_AFTER = 120
# Jellyfin writes the NFO first and the artwork right after; moving in between would leave images behind
ORGANIZE_AFTER = 120
STALE_RESCAN = 300
MIN_SIMILARITY = 0.8
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".wmv", ".m4v", ".mov", ".mpg", ".mpeg", ".ts", ".m2ts", ".webm", ".flv", ".divx", ".vob", ".iso", ".3gp"}
SUBTITLE = re.compile(r"(?:\.[\w-]+)*\.(?:srt|ass|ssa|sub|idx|vtt|sup|smi)", re.I)
ORGANIZED = re.compile(r".+ \(\d{4}\) \[(?:imdbid-tt\d+|tmdbid-\d+)\]")
EPISODE = re.compile(r"(?<![a-z0-9])s(\d{1,2})[ ._-]?e(\d{1,3})(?:-?e(\d{1,3}))?(?!\d)", re.I)
# Where a release name stops being the series' name; series folders named by hand have none of it and are left alone
SEASON_TOKEN = re.compile(r"(?<![a-zа-я0-9])(?:s\d{1,2}(?:[ ._-]?e\d{1,3})?|season|сезон)(?![a-zа-я])", re.I)
# What Jellyfin saves next to episodes and seasons; it alone would keep an emptied release folder alive
JELLYFIN_FILES = {".nfo", ".jpg", ".jpeg", ".png", ".webp", ".tbn"}
LANGUAGES = {"bg": "bg", "bul": "bg", "bulgarian": "bg", "en": "en", "eng": "en", "english": "en", "ru": "ru", "rus": "ru", "russian": "ru"}
# Order of the Bulgarian alphabet; Jellyfin transliterates Cyrillic, so sort names spell it out as codes (PLAN_jellyfin_library.md)
ALPHABET = "абвгдежзийклмнопрстуфхцчшщъьюя"
# Text subtitles ffsubsync can align; MicroDVD .sub files are converted to .srt first, other .sub files are images
SYNC_EXTS = {".srt", ".ass", ".ssa"}
MICRODVD = re.compile(rb"\s*\{\d+\}\{\d*\}")
# Less than this is within what ffsubsync gets wrong itself and nobody notices
MIN_OFFSET = 0.5
# An alignment within these is applied without waiting on the status page (safe_to_fix)
MAX_AUTO_OFFSET = 20
SAFE_SCALES = {a / b for a in (23.976, 24, 25) for b in (23.976, 24, 25)}
SYNC_TIMEOUT = 1800
SYNCED_DIR = os.path.join(DATA_DIR, "synced")
# Downloaded listings, kept so the episodes of one season pack don't fetch it again
SUBTITLE_CACHE = os.path.join(DATA_DIR, "subtitle-cache")
# Subtitles aligned with ffsubsync for one video in one go; the rest wait for the next pass over the library
MAX_TRIES = 4
WANTED_REFRESH = 3600
SEARCH_TTL = 3600
RETRY_NONE = 7 * 86400
RETRY_NOMATCH = 30 * 86400

# In memory only: rebuilt from Jellyfin on every run
status = {}
ready_since = {}
missing_since = {}
last_rescan = [0.0]
lock = threading.RLock()
wake = threading.Event()
last_run = {"started": None, "finished": None, "error": None, "interval": INTERVAL, "current": None}
# The library folders, from Jellyfin on every run; the subtitle check walks them
locations = []
series_locations = []
sync_wake = threading.Event()
# Torrents that couldn't be moved; tried again after a restart
failed_downloads = set()
# Downloading series episodes already looked up in Jellyfin
followed = set()
qbit_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
sync_run = {"current": None, "queued": 0}
# The videos that want Bulgarian subtitles, from Jellyfin every WANTED_REFRESH; the site searches by IMDb id
downloads_run = {"wanted": [], "review": {}, "listed_at": 0.0, "paused_until": 0.0}
searches = {}
# Jellyfin's item id of every movie and episode path, for refresh_video
item_ids = {"at": 0.0, "ids": {}}
# Alignments auto_fix couldn't apply; left on the status page until a restart
not_fixed = set()
activity = collections.deque(maxlen=500)
activity_seq = [0]
# The numbering above starts over with every start; the page compares this to know its last line is stale
BOOT = str(time.time())

os.makedirs(DATA_DIR, exist_ok=True)
db = sqlite3.connect(os.path.join(DATA_DIR, "state.db"), check_same_thread=False, isolation_level=None)
db.row_factory = sqlite3.Row
db.executescript("""
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    at REAL NOT NULL,
    path TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_at ON events(at);
-- A doubtful match confirmed on the status page, for the file and the id Jellyfin had then
CREATE TABLE IF NOT EXISTS confirmed (
    path TEXT NOT NULL,
    tag TEXT NOT NULL,
    at REAL NOT NULL,
    PRIMARY KEY (path, tag)
);
-- The last check of each subtitle; a file whose size or mtime differs is checked again
CREATE TABLE IF NOT EXISTS subtitles (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    video TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    checked_at REAL NOT NULL,
    -- ok, offset (waits on the status page), failed, fixed, dismissed, restored (the original put back)
    state TEXT NOT NULL,
    offset REAL,
    scale REAL,
    detail TEXT NOT NULL
);
-- The last search for Bulgarian subtitles for a video: found, none (nothing listed), nomatch (none lined up),
-- more (tried MAX_TRIES, the rest on the next pass) or failed; tried holds every file tried, so none is tried twice
CREATE TABLE IF NOT EXISTS downloads (
    video TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    subtitle TEXT,
    tried TEXT NOT NULL,
    checked_at REAL NOT NULL,
    detail TEXT NOT NULL
);
-- What the user said about a movie's or series' folder whose sound names no language: bg (dubbed) or subs (wants
-- Bulgarian subtitles); tagged holds the MKV tracks then set to Bulgarian, [path, sound track position], to undo it by
CREATE TABLE IF NOT EXISTS audio (
    folder TEXT PRIMARY KEY,
    decision TEXT NOT NULL,
    decided_at REAL NOT NULL,
    tagged TEXT NOT NULL DEFAULT '[]'
);
""")
# A failed check is retried after a restart, which is when the checker itself may have changed
db.execute("DELETE FROM subtitles WHERE state = 'failed'")
os.makedirs(SYNCED_DIR, exist_ok=True)
os.makedirs(SUBTITLE_CACHE, exist_ok=True)


def log(text):
    print(text, flush=True)
    with lock:
        activity_seq[0] += 1
        activity.append({"seq": activity_seq[0], "at": time.time(), "text": text})


# Shown on the status page only, so in its language
def step(text):
    last_run["current"] = text


def event(path, kind, detail, label):
    log(f"{kind} {label}: {detail}")
    with lock:
        db.execute("INSERT INTO events (at, path, kind, detail) VALUES (?, ?, ?, ?)", (time.time(), path, kind, detail))


# The live panel starts from the latest events on record rather than empty after a restart
def seed_activity():
    for row in reversed(db.execute("SELECT at, path, kind, detail FROM events ORDER BY at DESC, id DESC LIMIT 200").fetchall()):
        activity_seq[0] += 1
        activity.append({"seq": activity_seq[0], "at": row["at"], "text": f"{row['kind']} {os.path.basename(os.path.dirname(row['path']))}: {row['detail']}"})


seed_activity()


def jellyfin(path, method="GET", body=None, **params):
    if path == "/Items" and "IncludeItemTypes" in params:
        # Otherwise the movies of a collection are listed as the collection alone
        params.setdefault("CollapseBoxSetItems", "false")
    query = "?" + urllib.parse.urlencode(params) if params else ""
    headers = {"Authorization": f'MediaBrowser Token="{JELLYFIN_API_KEY}"', "Accept": "application/json", "Content-Type": "application/json"}
    data = None if body is None else json.dumps(body).encode()
    with urllib.request.urlopen(urllib.request.Request(JELLYFIN_URL + path + query, data, headers, method=method), timeout=60) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


def bg_sort(name):
    words = []
    for word in name.lower().split():
        code = ""
        for ch in word:
            index = ALPHABET.find(ch)
            if index >= 0:
                code += "ab"[index // 26] + chr(ord("a") + index % 26)
            # After the Cyrillic codes (a?/b?) rather than mixed in with them
            elif "a" <= ch <= "z":
                code += "c" + ch
            # Digits Jellyfin pads itself, other letters (ы, é, kana) it transliterates itself
            elif ch.isdigit() or (ch.isalpha() and not ch.isascii()):
                code += ch
        if code:
            words.append(code)
    return " ".join(words)


def padded(sort_name):
    return re.sub(r"\d+", lambda m: m.group().zfill(10), sort_name)


def norm(text):
    return re.sub(r"[\W_]", "", text.lower().replace("&", "and"))


def doubt(item, names):
    """Why Jellyfin's match doesn't look like the release name(s), or "" when it does."""
    year = item.get("ProductionYear")
    titles = [norm(t) for t in (item.get("OriginalTitle"), item.get("Name")) if t]
    best = None
    for name in names:
        clean = re.sub(r"[._]+", " ", re.sub(r"\[[^\]]*\]", " ", name))
        # The title is whatever comes before a year; with several ("Blade Runner 2049 2017") each is tried
        cuts = [(m.start(), int(m.group(1))) for m in re.finditer(r"(?<!\w)((?:19|20)\d{2})(?!\w)", clean)] or [(len(clean), None)]
        for at, found_year in cuts:
            title = clean[:at].strip(" -([")
            if not title:
                continue
            similarity = max(difflib.SequenceMatcher(None, norm(title), t).ratio() for t in titles)
            year_ok = not found_year or not year or abs(found_year - year) <= 1
            score = (similarity >= MIN_SIMILARITY and year_ok, similarity)
            if not best or score > best[0]:
                best = (score, title, found_year)
    if not best or best[0][0]:
        return ""
    _, title, found_year = best
    return f'„{title}"{f" ({found_year})" if found_year else ""} не прилича на „{item.get("OriginalTitle") or item.get("Name")}" ({year})'


def id_tag(item):
    ids = item.get("ProviderIds") or {}
    return f"imdbid-{ids['Imdb']}" if ids.get("Imdb") else f"tmdbid-{ids['Tmdb']}" if ids.get("Tmdb") else None


def clean_title(title):
    title = re.sub(r"\s*:\s*", " - ", title)
    # "9 1/2 Weeks" and "AC/DC" rather than "9 12 Weeks" and "ACDC"
    title = re.sub(r"\s*\b1/2\b", "½", title).replace("/", "-")
    title = re.sub(r'[\\?*"<>|]', "", title)
    return re.sub(r"\s+", " ", title).strip(" .")


def target_name(item):
    title = item.get("OriginalTitle") or item["Name"]
    # Folders are in Latin or Cyrillic; a Japanese or Korean original gives way to the localized name
    if re.search(r"[^\u0000-ӿ -⁯]", title):
        title = item["Name"]
    return f"{clean_title(title)} ({item['ProductionYear']}) [{id_tag(item)}]"


def release_title(name):
    """The series' name in a release name ("The.Walking.Dead.S01.HDTV" -> "The.Walking.Dead.")."""
    return SEASON_TOKEN.split(name)[0] or name


def episode_numbers(episode):
    """(season, first, last) by Jellyfin; last is None for a single episode."""
    end = episode.get("IndexNumberEnd")
    return episode["ParentIndexNumber"], episode["IndexNumber"], end if end != episode["IndexNumber"] else None


def episode_name(episode, stem):
    season, first, last = episode_numbers(episode)
    numbers = f"S{season:02d}E{first:02d}" + (f"-E{last:02d}" if last else "")
    title = clean_title(episode.get("Name") or "")
    # Until Jellyfin has the episode's title, its name is the file's
    if not title or title == clean_title(stem) or EPISODE.search(title) or re.fullmatch(r"(?i)(?:episode|епизод)\s*\d+|tba", title):
        return numbers
    return f"{numbers} - {title}"


def season_dir(series_dir, season):
    """The series' own folder for the season when it has one ("Season 1", "S01"), otherwise "Season 01"."""
    if os.path.isdir(series_dir):
        for name in sorted(os.listdir(series_dir)):
            m = re.fullmatch(r"(?:season|сезон|s)[ ._-]?0*(\d+)", name, re.I)
            if m and int(m.group(1)) == season and os.path.isdir(os.path.join(series_dir, name)):
                return name
    return f"Season {season:02d}"


def is_xml(path):
    try:
        with open(path, "rb") as f:
            return f.read(512).lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<")
    except OSError:
        return False


def subtitle_language(path):
    lower = path.lower()
    # VobSub (.idx with a binary .sub) and PGS are images
    if lower.endswith((".idx", ".sup")) or (lower.endswith(".sub") and os.path.exists(path[:-4] + ".idx")):
        return None
    with open(path, "rb") as f:
        raw = f.read(200_000)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp1251", errors="replace")
    letters = [c for c in text if c.isalpha()]
    cyrillic = sum("Ѐ" <= c <= "ӿ" for c in letters)
    # Cyrillic subtitles here are Bulgarian, also those of the Russian films
    return "bg" if letters and cyrillic / len(letters) > 0.3 else None


def with_language(path, rest):
    """rest is what follows the movie's name (".srt", ".bg.srt", ".en.forced.srt")."""
    if rest.count(".") > 1:
        return rest
    language = subtitle_language(path)
    return f".{language}{rest}" if language else rest


def foreign_rest(name):
    """The language and extension of a subtitle named after something else ("Some.Release.eng.srt" -> ".en.srt")."""
    ext = os.path.splitext(name)[1].lower()
    m = re.search(r"(?:^|[._ -])(" + "|".join(LANGUAGES) + r")([._ -]forced)?\.\w+$", name, re.I)
    return f".{LANGUAGES[m.group(1).lower()]}{'.forced' if m.group(2) else ''}{ext}" if m else ext


def generic_name(rest):
    # What Jellyfin calls a movie's NFO and artwork when the video is alone in its folder
    if rest.lower() == ".nfo":
        return "movie.nfo"
    art = re.fullmatch(r"-(\w+)(\.\w+)", rest)
    if art:
        return ("folder" if art.group(1).lower() == "poster" else art.group(1).lower()) + art.group(2).lower()
    return None


class Conflict(Exception):
    pass


def owner(name, videos):
    """The video (name without extension) a file next to it belongs to, or None."""
    # The longest matching video name owns the file, so "Movie 2.srt" stays with "Movie 2.mkv" rather than "Movie.mkv"
    owners = [v for v in videos if name.lower().startswith(v.lower()) and name[len(v):len(v) + 1] in (".", "-")]
    return max(owners, key=len) if owners else None


def make_dir(path, like):
    os.mkdir(path)
    parent = os.stat(like)
    os.chown(path, parent.st_uid, parent.st_gid)
    # UGOS grants access to the library folders through ACLs, so their mode reads 000 or 770 from here; every folder in them is 777
    os.chmod(path, 0o777)


def plan(folder, stem, target, own):
    """What happens to each file next to the video: (renames as (old, new)), (deletions).

    own: the video has its own folder, so everything in it belongs to the movie; otherwise only the
    files named after it move along.
    """
    names = sorted(n for n in os.listdir(folder) if os.path.isfile(os.path.join(folder, n)))
    videos = [os.path.splitext(n)[0] for n in names if os.path.splitext(n)[1].lower() in VIDEO_EXTS]
    renames, deletions = [], []
    for name in names:
        path = os.path.join(folder, name)
        base, ext = os.path.splitext(name)
        if ext.lower() in VIDEO_EXTS:
            if base == stem:
                renames.append((name, target + ext))
            continue
        mine = owner(name, videos) == stem
        if not (own or mine):
            continue
        if ext.lower() == ".nfo" and not is_xml(path):
            # Release notes of the download; Jellyfin would try to read them as the movie's NFO
            deletions.append(name)
        elif mine:
            rest = name[len(stem):]
            if SUBTITLE.fullmatch(rest):
                renames.append((name, target + with_language(path, rest)))
            else:
                renames.append((name, (not own and generic_name(rest)) or target + rest))
        elif SUBTITLE.fullmatch(ext):
            renames.append((name, target + with_language(path, foreign_rest(name))))
    renames = [(old, new) for old, new in renames if old != new or not own]
    news = [new.lower() for _, new in renames]
    if len(set(news)) != len(news):
        raise Conflict(f"две неща биха се казали еднакво: {', '.join(sorted(n for n in news if news.count(n) > 1))}")
    if own:
        moving = {old for old, _ in renames} | set(deletions)
        taken = [new for _, new in renames if new in names and new not in moving]
        if taken:
            raise Conflict(f"вече има {', '.join(taken)}")
    return renames, deletions


def ingest(item, parts, location, label):
    folder, filename = os.path.split(item["Path"])
    stem = os.path.splitext(filename)[0]
    target = target_name(item)
    own = len(parts) == 2
    target_dir = os.path.join(location, target)
    if os.path.exists(target_dir) and target_dir != folder:
        raise Conflict(f"{target}/ вече съществува")
    renames, deletions = plan(folder, stem, target, own)

    if not own:
        make_dir(target_dir, location)
    for name in deletions:
        os.remove(os.path.join(folder, name))
    for old, new in renames:
        os.rename(os.path.join(folder, old), os.path.join(folder if own else target_dir, new))
    if own and folder != target_dir:
        os.rename(folder, target_dir)

    new_path = os.path.join(target_dir, target + os.path.splitext(filename)[1])
    renamed_folder = own and folder != target_dir
    jellyfin("/Library/Media/Updated", "POST", {"Updates": [
        {"Path": folder if renamed_folder else item["Path"], "UpdateType": "Deleted"},
        {"Path": new_path if own and not renamed_folder else target_dir, "UpdateType": "Created"},
    ]})
    changes = [f"{old} → {new}" for old, new in renames if old != filename] + [f"изтрит {name}" for name in deletions]
    event(new_path, "moved", f"{'/'.join(parts[:-1] or parts)} → {target}/" + (f" ({'; '.join(changes)})" if changes else ""), label)
    return new_path


def set_sort_name(item, admin, label):
    if item.get("SortName") == padded(bg_sort(item["Name"])):
        return
    full = jellyfin(f"/Items/{item['Id']}", userId=admin["Id"])
    sort_name = bg_sort(full["Name"])
    # Never over one set by hand; a name that is the folder's means TMDB had no title in the metadata language
    if full.get("ForcedSortName") or not sort_name or ORGANIZED.fullmatch(full["Name"]):
        return
    full["ForcedSortName"] = sort_name
    jellyfin(f"/Items/{item['Id']}", "POST", full)
    event(item["Path"], "sorted", f"{full['Name']} → {sort_name}", label)


def record(item, library, name, state, detail, target=None, confirm=False):
    with lock:
        status[item["Id"]] = {
            "id": item["Id"], "path": item["Path"], "library": library, "file": name, "state": state,
            "detail": detail, "target": target, "tag": id_tag(item), "confirm": confirm, "checked": time.time(),
        }


def check(item, library, location, admin):
    """Looks at one movie and ingests it when it's ready; returns when it wants to be looked at again."""
    parts = item["Path"][len(location.rstrip("/")) + 1:].split("/")
    name = parts[0]
    label = f"{library['Name']}/{name}"
    folder = os.path.dirname(item["Path"])
    stem = os.path.splitext(parts[-1])[0]
    identified = bool(id_tag(item) and item.get("ProductionYear"))
    nfo = any(is_xml(os.path.join(folder, n)) for n in ("movie.nfo", stem + ".nfo"))

    if len(parts) == 2 and ORGANIZED.fullmatch(parts[0]) and (stem == parts[0] or stem.startswith(parts[0] + " - ")):
        if identified and nfo and ORGANIZED.fullmatch(item["Name"]):
            record(item, library["Name"], name, "review", "Jellyfin го показва с името на папката (TMDB няма заглавие на български): сложи заглавие в Jellyfin и го заключи")
            return float("inf")
        with lock:
            status.pop(item["Id"], None)
        # Only once the metadata is in, as the name is the filename's until then
        if identified and nfo:
            set_sort_name(item, admin, label)
        return float("inf")

    if not os.path.isfile(item["Path"]):
        record(item, library["Name"], name, "pending", "Jellyfin още показва стария път")
        missing_since.setdefault(item["Id"], time.time())
        return float("inf")
    settles = os.stat(item["Path"]).st_ctime + SETTLE_AFTER
    if time.time() < settles:
        record(item, library["Name"], name, "pending", "файлът още се копира")
        return settles
    if not identified:
        record(item, library["Name"], name, "unidentified", "Jellyfin не е разпознал филма (или няма година): Identify в Jellyfin")
        return float("inf")
    target = target_name(item)
    if len(parts) > 2:
        record(item, library["Name"], name, "review", "видеото е в подпапка: подреди го на ръка", target)
        return float("inf")
    if len(parts) == 2 and sum(os.path.splitext(n)[1].lower() in VIDEO_EXTS for n in os.listdir(folder)) > 1:
        record(item, library["Name"], name, "review", "в папката има няколко видеа: подреди ги на ръка", target)
        return float("inf")
    if not nfo:
        record(item, library["Name"], name, "pending", "чака Jellyfin да запише NFO файла", target)
        return float("inf")
    due = ready_since.setdefault(item["Id"], time.time()) + ORGANIZE_AFTER
    if time.time() < due:
        record(item, library["Name"], name, "pending", "изчаква Jellyfin да запише картинките", target)
        return due
    reason = doubt(item, [parts[0], stem] if len(parts) == 2 else [stem])
    with lock:
        confirmed = db.execute("SELECT 1 FROM confirmed WHERE path = ? AND tag = ?", (item["Path"], id_tag(item))).fetchone()
    if reason and not confirmed:
        record(item, library["Name"], name, "review", f"{reason}: провери match-а в Jellyfin и потвърди", target, confirm=True)
        return float("inf")
    step(f"{label}: преименува на {target}")
    try:
        ingest(item, parts, location, label)
    except (Conflict, OSError) as e:
        log(f"error {label}: {e}")
        record(item, library["Name"], name, "review", str(e), target)
        return float("inf")
    record(item, library["Name"], name, "pending", "преименуван, чака Jellyfin да види новата папка", target)
    return time.time() + 60


def episode_plan(folder, stem, new_stem):
    """(old, new) names of an episode's video and the files named after it, and the release notes to delete."""
    names = sorted(n for n in os.listdir(folder) if os.path.isfile(os.path.join(folder, n)))
    videos = [os.path.splitext(n)[0] for n in names if os.path.splitext(n)[1].lower() in VIDEO_EXTS]
    renames, deletions = [], []
    for name in names:
        path = os.path.join(folder, name)
        base, ext = os.path.splitext(name)
        if ext.lower() in VIDEO_EXTS:
            if base == stem:
                renames.append((name, new_stem + ext))
        elif owner(name, videos) == stem:
            rest = name[len(stem):]
            if ext.lower() == ".nfo" and not is_xml(path):
                deletions.append(name)
            elif SUBTITLE.fullmatch(rest):
                renames.append((name, new_stem + with_language(path, rest)))
            else:
                renames.append((name, new_stem + rest))
    return renames, deletions


def series_problem(folder, videos, episodes):
    """Why a release folder can't be organized on its own, or "" when it can."""
    paths = {e["Path"] for e in episodes}
    for video in videos:
        if video not in paths:
            return f"{os.path.relpath(video, folder)} не е епизод в Jellyfin: подреди го на ръка"
    for e in episodes:
        rel = os.path.relpath(e["Path"], folder)
        if rel.count("/") > 1:
            return f"{rel} е в подпапка на подпапка: подреди го на ръка"
        if e.get("ParentIndexNumber") is None or e.get("IndexNumber") is None:
            return f"{rel} е без номер на сезон или епизод в Jellyfin"
        # The file's name is the source of truth for the numbers (PLAN_jellyfin_library.md)
        m = EPISODE.search(os.path.basename(e["Path"]))
        if not m:
            return f"{rel} няма SxxEyy в името"
        found = (int(m.group(1)), int(m.group(2)), int(m.group(3)) if m.group(3) else None)
        if found != episode_numbers(e):
            return f"{rel}: Jellyfin го има като S{e['ParentIndexNumber']:02d}E{e['IndexNumber']:02d}"
    return ""


def organize_series(folder, episodes, target_dir, label):
    """Moves the episodes of a release folder to target_dir/Season NN as SxxEyy - Title, renaming the
    folder itself into target_dir when the series has no folder yet, otherwise adding to it."""
    merge = os.path.exists(target_dir)
    by_dir = collections.defaultdict(list)
    for e in episodes:
        by_dir[os.path.dirname(e["Path"])].append(e)
    seasons = {}
    dir_moves = {}

    def season_path(n):
        if n not in seasons:
            seasons[n] = os.path.join(target_dir, season_dir(target_dir if merge else folder, n))
        return seasons[n]

    def renamed(path):
        return path if merge else target_dir + path[len(folder):]

    def before(path):
        """Where a path of the result is now."""
        for src, dst in dir_moves.items():
            if path == dst or path.startswith(dst + "/"):
                return src + path[len(dst):]
        return path if merge else folder + path[len(target_dir):]

    # A subfolder with the only episodes of one season becomes its folder, keeping what Jellyfin saved in it
    for d, eps in by_dir.items():
        numbers = {e["ParentIndexNumber"] for e in eps}
        if d == folder or len(numbers) != 1:
            continue
        n = numbers.pop()
        dest = season_path(n)
        shared = any(o != d and any(e["ParentIndexNumber"] == n for e in other) for o, other in by_dir.items())
        if not shared and renamed(d) != dest and not os.path.exists(before(dest)):
            dir_moves[d] = dest

    moves, deletions = [], []
    for d, eps in by_dir.items():
        at = dir_moves.get(d) or renamed(d)
        for e in eps:
            stem = os.path.splitext(os.path.basename(e["Path"]))[0]
            renames, dels = episode_plan(d, stem, episode_name(e, stem))
            dest = season_path(e["ParentIndexNumber"])
            moves += [(os.path.join(at, old), os.path.join(dest, new)) for old, new in renames]
            deletions += [os.path.join(at, name) for name in dels]
    moves = [(src, dst) for src, dst in moves if src != dst]
    news = [dst.lower() for _, dst in moves]
    if len(set(news)) != len(news):
        raise Conflict(f"две неща биха се казали еднакво: {', '.join(sorted({os.path.basename(n) for n in news if news.count(n) > 1}))}")
    sources = {src for src, _ in moves}
    taken = [dst for _, dst in moves if dst not in sources and os.path.exists(before(dst))]
    if taken:
        raise Conflict(f"вече има {', '.join(os.path.relpath(t, target_dir) for t in taken)}")
    if merge:
        for n, dest in seasons.items():
            if not os.path.isdir(dest):
                continue
            have = {i for name in os.listdir(dest) if os.path.splitext(name)[1].lower() in VIDEO_EXTS
                    for m in [EPISODE.search(name)] if m for i in range(int(m.group(2)), int(m.group(3) or m.group(2)) + 1)}
            incoming = {i for e in episodes if e["ParentIndexNumber"] == n
                        for _, first, last in [episode_numbers(e)] for i in range(first, (last or first) + 1)}
            if have & incoming:
                raise Conflict(f"{os.path.basename(dest)} вече има епизод {', '.join(map(str, sorted(have & incoming)))}")

    if not merge:
        os.rename(folder, target_dir)
    for src, dst in dir_moves.items():
        os.rename(renamed(src), dst)
    for dest in seasons.values():
        if not os.path.isdir(dest):
            make_dir(dest, os.path.dirname(target_dir))
    for path in deletions:
        os.remove(path)
    for src, dst in moves:
        os.rename(src, dst)

    # What's left of the release: Jellyfin's NFO files and artwork go, anything else stays for a look
    emptied = {renamed(d) for d in by_dir if d not in dir_moves} | ({folder} if merge else set())
    emptied -= set(seasons.values()) | {target_dir}
    left = []
    for d in sorted(emptied, key=len, reverse=True):
        if not os.path.isdir(d):
            continue
        for root, _, names in os.walk(d, topdown=False):
            for name in names:
                if os.path.splitext(name)[1].lower() in JELLYFIN_FILES:
                    os.remove(os.path.join(root, name))
            if not os.listdir(root):
                os.rmdir(root)
        if os.path.exists(d):
            left.append(os.path.relpath(d, os.path.dirname(target_dir)))

    jellyfin("/Library/Media/Updated", "POST", {"Updates": [
        {"Path": folder, "UpdateType": "Modified" if os.path.exists(folder) else "Deleted"},
        {"Path": target_dir, "UpdateType": "Created"},
    ]})
    detail = f"{os.path.basename(folder)} → {os.path.basename(target_dir)}/ ({len(episodes)} еп."
    detail += ", добавени към сериала" if merge else ""
    detail += f"; остана {', '.join(left)}" if left else ""
    event(os.path.join(target_dir, "tvshow.nfo"), "moved", detail + ")", label)


def check_series(item, library, location, admin):
    """Looks at one series and organizes it when it's a release folder that's ready; returns when it wants to be looked at again."""
    folder = item["Path"].rstrip("/")
    name = os.path.basename(folder)
    label = f"{library['Name']}/{name}"
    identified = bool(id_tag(item) and item.get("ProductionYear"))
    nfo = is_xml(os.path.join(folder, "tvshow.nfo"))

    if ORGANIZED.fullmatch(name):
        with lock:
            status.pop(item["Id"], None)
        if identified and nfo:
            set_sort_name(item, admin, label)
        return float("inf")
    if not SEASON_TOKEN.search(name):
        # A downloaded series pack ("Everybody Loves Raymond/... s01/...") is a release folder too; series
        # folders named by hand never came through qBittorrent
        with lock:
            downloaded = db.execute("SELECT 1 FROM events WHERE kind = 'downloaded' AND path = ?", (folder,)).fetchone()
        if not downloaded:
            return float("inf")

    if not os.path.isdir(folder):
        record(item, library["Name"], name, "pending", "Jellyfin още показва стария път")
        missing_since.setdefault(item["Id"], time.time())
        return float("inf")
    videos = [os.path.join(d, n) for d, _, names in os.walk(folder) for n in names if os.path.splitext(n)[1].lower() in VIDEO_EXTS]
    if not videos:
        record(item, library["Name"], name, "review", "в папката няма видео")
        return float("inf")
    settles = max(os.stat(v).st_ctime for v in videos) + SETTLE_AFTER
    if time.time() < settles:
        record(item, library["Name"], name, "pending", "файловете още се копират")
        return settles
    if not identified:
        record(item, library["Name"], name, "unidentified", "Jellyfin не е разпознал сериала (или няма година): Identify в Jellyfin")
        return float("inf")
    tag = id_tag(item)
    existing = [os.path.join(l.rstrip("/"), n) for l in library["Locations"] if os.path.isdir(l) for n in os.listdir(l)
                if n.endswith(f"[{tag}]") and os.path.isdir(os.path.join(l, n))]
    if len(existing) > 1 or (existing and os.path.dirname(existing[0]) != location.rstrip("/")):
        record(item, library["Name"], name, "review", f"сериалът вече е в {', '.join(existing)}: премести го на ръка")
        return float("inf")
    target_dir = existing[0] if existing else os.path.join(location.rstrip("/"), target_name(item))
    target = os.path.basename(target_dir)
    if not nfo:
        record(item, library["Name"], name, "pending", "чака Jellyfin да запише NFO файла", target)
        return float("inf")
    episodes = [e for e in jellyfin("/Items", ParentId=item["Id"], Recursive="true", IncludeItemTypes="Episode", Fields="Path", Limit=100000)["Items"] if e.get("Path")]
    problem = series_problem(folder, videos, episodes)
    if problem:
        record(item, library["Name"], name, "review", problem, target)
        return float("inf")
    if not all(is_xml(os.path.splitext(e["Path"])[0] + ".nfo") for e in episodes):
        record(item, library["Name"], name, "pending", "чака Jellyfin да запише NFO файловете на епизодите", target)
        return float("inf")
    due = ready_since.setdefault(item["Id"], time.time()) + ORGANIZE_AFTER
    if time.time() < due:
        record(item, library["Name"], name, "pending", "изчаква Jellyfin да запише картинките", target)
        return due
    reason = doubt(item, [release_title(name), release_title(os.path.basename(videos[0]))])
    with lock:
        confirmed = db.execute("SELECT 1 FROM confirmed WHERE path = ? AND tag = ?", (item["Path"], tag)).fetchone()
    if reason and not confirmed:
        record(item, library["Name"], name, "review", f"{reason}: провери match-а в Jellyfin и потвърди", target, confirm=True)
        return float("inf")
    step(f"{label}: подрежда в {target}")
    try:
        organize_series(folder, episodes, target_dir, label)
    except (Conflict, OSError) as e:
        log(f"error {label}: {e}")
        record(item, library["Name"], name, "review", str(e), target)
        return float("inf")
    record(item, library["Name"], name, "pending", "подреден, чака Jellyfin да види новата папка", target)
    return time.time() + 60


def run_once():
    step("чете библиотеките от Jellyfin")
    admin = next(u for u in jellyfin("/Users") if u["Policy"]["IsAdministrator"])
    folders = {f["Name"]: f for f in jellyfin("/Library/VirtualFolders")}
    seen, next_wake = set(), float("inf")
    locations[:] = [l for name in LIBRARIES if name in folders for l in folders[name]["Locations"]]
    series_locations[:] = [l for name in SERIES_LIBRARIES if name in folders for l in folders[name]["Locations"]]
    for name in LIBRARIES:
        library = folders.get(name)
        if not library:
            log(f"error library {name} not found in Jellyfin")
            continue
        items = jellyfin("/Items", ParentId=library["ItemId"], Recursive="true", IncludeItemTypes="Movie",
                         Fields="Path,ProviderIds,OriginalTitle,ProductionYear,SortName", Limit=100000)["Items"]
        step(f"{name}: преглежда {len(items)} филма")
        for item in items:
            location = next((l for l in library["Locations"] if item.get("Path", "").startswith(l.rstrip("/") + "/")), None)
            if not location:
                continue
            seen.add(item["Id"])
            try:
                next_wake = min(next_wake, check(item, library, location, admin))
            except (urllib.error.URLError, TimeoutError) as e:
                log(f"error {name}/{item['Name']}: {e!r}; tried again on the next run")
    for name in SERIES_LIBRARIES:
        library = folders.get(name)
        if not library:
            log(f"error library {name} not found in Jellyfin")
            continue
        items = jellyfin("/Items", ParentId=library["ItemId"], Recursive="true", IncludeItemTypes="Series",
                         Fields="Path,ProviderIds,OriginalTitle,ProductionYear,SortName", Limit=100000)["Items"]
        step(f"{name}: преглежда {len(items)} сериала")
        for item in items:
            location = next((l for l in library["Locations"] if os.path.dirname(item.get("Path", "").rstrip("/")) == l.rstrip("/")), None)
            if not location:
                continue
            seen.add(item["Id"])
            try:
                next_wake = min(next_wake, check_series(item, library, location, admin))
            except (urllib.error.URLError, TimeoutError) as e:
                log(f"error {name}/{item['Name']}: {e!r}; tried again on the next run")

    with lock:
        for gone in set(status) - seen:
            del status[gone]
    for gone in set(missing_since) - {i for i, s in status.items() if s["detail"] == "Jellyfin още показва стария път"}:
        del missing_since[gone]
    for gone in set(ready_since) - set(status):
        del ready_since[gone]
    # Jellyfin sometimes keeps a moved movie at its old path until the library is rescanned
    stale = min(missing_since.values(), default=float("inf"))
    if time.time() - stale > STALE_RESCAN and time.time() - last_rescan[0] > STALE_RESCAN:
        last_rescan[0] = time.time()
        log("rescan: Jellyfin still lists moved files at their old paths")
        jellyfin("/Library/Refresh", "POST")
    if stale < float("inf"):
        next_wake = min(next_wake, max(stale, last_rescan[0]) + STALE_RESCAN)
    # Renamed movies have new subtitle paths to check
    sync_wake.set()
    return next_wake


def ingest_loop():
    while True:
        wake.clear()
        last_run["started"] = time.time()
        next_wake = float("inf")
        try:
            next_wake = run_once()
            last_run["error"] = None
        except Exception as e:
            log(f"error: {e!r}")
            last_run["error"] = repr(e)
        last_run["finished"] = time.time()
        last_run["current"] = None
        wake.wait(max(1, min(INTERVAL, next_wake - time.time() + 1)))


def translate(path, pairs):
    for src, dst in pairs:
        src = src.rstrip("/")
        if path == src or path.startswith(src + "/"):
            return dst.rstrip("/") + path[len(src):]
    return None


def qbit(path, **form):
    data = urllib.parse.urlencode(form).encode() if form else None
    headers = {"Referer": QBIT_URL}
    for attempt in (0, 1):
        try:
            with qbit_opener.open(urllib.request.Request(QBIT_URL + "/api/v2/" + path, data, headers), timeout=30) as response:
                raw = response.read()
            return json.loads(raw) if raw[:1] in (b"[", b"{") else raw.decode()
        except urllib.error.HTTPError as e:
            if e.code != 403 or attempt:
                raise
            login = urllib.parse.urlencode({"username": QBIT_USER, "password": QBIT_PASSWORD}).encode()
            with qbit_opener.open(urllib.request.Request(QBIT_URL + "/api/v2/auth/login", login, headers), timeout=30) as response:
                if response.read().strip() != b"Ok.":
                    raise RuntimeError("qBittorrent rejected QBIT_USER / QBIT_PASSWORD")


def library_devices(library):
    return {os.stat(host).st_dev for l in library["Locations"] if (host := translate(l, VOLUME_PATHS)) and os.path.isdir(host)}


def choose_save_paths(folders):
    """Points each library's category at the download folder with the most free space among the disks the library is on."""
    categories = qbit("torrents/categories")
    for name in LIBRARIES + SERIES_LIBRARIES:
        if name not in folders:
            continue
        devices = library_devices(folders[name])
        options = [(shutil.disk_usage(local).free, path) for path in DOWNLOAD_DIRS
                   if (local := translate(path, QBIT_PATHS)) and os.path.isdir(local) and os.stat(local).st_dev in devices]
        if not options:
            continue
        free, best = max(options)
        current = categories.get(name)
        if current and current.get("savePath", "").rstrip("/") == best.rstrip("/"):
            continue
        qbit("torrents/editCategory" if current else "torrents/createCategory", category=name, savePath=best)
        log(f"qBittorrent: {name} се сваля в {best} ({free / 1e12:.1f} TB свободни)")


def import_downloads(folders, torrents):
    for torrent in torrents:
        library, key = torrent["category"], torrent["hash"]
        if library not in LIBRARIES + SERIES_LIBRARIES or key in failed_downloads or torrent["state"] == "moving":
            continue
        label = f"{library}/{torrent['name']}"
        src = translate(torrent["content_path"], QBIT_PATHS)
        try:
            if library not in folders:
                raise OSError("библиотеката я няма в Jellyfin")
            if not src or not os.path.exists(src):
                raise OSError(f"{torrent['content_path']} не се вижда в контейнера (QBIT_PATHS)")
            device = os.stat(src).st_dev
            location, folder = next(((l, host) for l in folders[library]["Locations"]
                                     if (host := translate(l, VOLUME_PATHS)) and os.path.isdir(host) and os.stat(host).st_dev == device),
                                    (None, None))
            if not location:
                raise OSError("библиотеката няма папка на диска, на който е свалено")
            name = os.path.basename(src.rstrip("/"))
            if os.path.lexists(os.path.join(folder, name)):
                raise OSError(f"{name} вече е в {location}")
            os.rename(src, os.path.join(folder, name))
        except OSError as e:
            failed_downloads.add(key)
            log(f"error {label}: {e}")
            continue
        qbit("torrents/delete", hashes=key, deleteFiles="false")
        path = os.path.join(location, name)
        jellyfin("/Library/Media/Updated", "POST", {"Updates": [{"Path": path, "UpdateType": "Created"}]})
        event(path, "downloaded", f"{torrent['content_path']} → {path}", label)
        wake.set()


def series_title(name):
    """The series' name in a release name, without the year some releases carry ("Severance.2022.S01E01" -> "severance")."""
    return norm(re.sub(r"[ ._-]*\(?(?:19|20)\d{2}\)?[ ._-]*$", "", release_title(name)))


def follow_series(folders, torrents):
    """Moves a downloading episode of a series that's already in its library to the download folder on that series' disk."""
    series = {}
    for torrent in torrents:
        name, key = torrent["category"], torrent["hash"]
        if name not in SERIES_LIBRARIES or name not in folders or torrent["progress"] >= 1 or key in followed:
            continue
        if name not in series:
            series[name] = jellyfin("/Items", ParentId=folders[name]["ItemId"], Recursive="true", IncludeItemTypes="Series",
                                    Fields="Path,OriginalTitle", Limit=100000)["Items"]
        followed.add(key)
        title = series_title(torrent["name"])
        homes = {host for s in series[name] if ORGANIZED.fullmatch(os.path.basename(s.get("Path", "").rstrip("/")))
                 and title in (norm(s.get("OriginalTitle") or ""), norm(s["Name"]))
                 and (host := translate(s["Path"], VOLUME_PATHS)) and os.path.isdir(host)}
        if len(homes) != 1:
            continue
        home = homes.pop()
        device = os.stat(home).st_dev
        target = next((path for path in DOWNLOAD_DIRS if (local := translate(path, QBIT_PATHS)) and os.path.isdir(local) and os.stat(local).st_dev == device), None)
        current = translate(torrent["save_path"], QBIT_PATHS)
        if not target or (current and os.path.isdir(current) and os.stat(current).st_dev == device):
            continue
        qbit("torrents/setLocation", hashes=key, location=target)
        event(home, "followed", f"{torrent['name']}: сваля се в {target}, при {os.path.basename(home)}", f"{name}/{torrent['name']}")


def download_loop():
    while True:
        try:
            folders = {f["Name"]: f for f in jellyfin("/Library/VirtualFolders")}
            choose_save_paths(folders)
            torrents = qbit("torrents/info")
            follow_series(folders, torrents)
            import_downloads(folders, [t for t in torrents if t["progress"] >= 1])
        except Exception as e:
            log(f"error qBittorrent: {e!r}")
        time.sleep(DOWNLOAD_INTERVAL)


def synced_path(subtitle):
    return os.path.join(SYNCED_DIR, hashlib.sha1(subtitle.encode()).hexdigest()[:16] + os.path.splitext(subtitle)[1].lower())


def subtitle_files(exts=SYNC_EXTS):
    """(subtitle, video) for every subtitle with one of exts in an organized movie folder or next to a series episode."""
    folders = []
    for location in list(locations):
        if os.path.isdir(location):
            folders += [os.path.join(location, f) for f in os.listdir(location) if ORGANIZED.fullmatch(f)]
    for location in list(series_locations):
        folders += [root for root, _, _ in os.walk(location)]
    for folder in folders:
        if not os.path.isdir(folder):
            continue
        names = os.listdir(folder)
        videos = [n for n in names if os.path.splitext(n)[1].lower() in VIDEO_EXTS]
        for name in names:
            if os.path.splitext(name)[1].lower() not in exts:
                continue
            # "X - English.srt" goes with "X - English.mkv" rather than "X.mkv"
            owners = [v for v in videos if name.lower().startswith(os.path.splitext(v)[0].lower() + ".")]
            if owners:
                yield os.path.join(folder, name), os.path.join(folder, max(owners, key=len))


def subtitle_label(path):
    """The folder of a subtitle within its library: the movie, or the series and season."""
    folder = os.path.dirname(path)
    location = next((l for l in locations + series_locations if folder.startswith(l.rstrip("/") + "/")), None)
    return os.path.relpath(folder, location) if location else os.path.basename(folder)


def pending_subtitles():
    with lock:
        rows = {r["path"]: r for r in db.execute("SELECT path, size, mtime FROM subtitles")}
    found, todo = set(), []
    for subtitle, video in subtitle_files():
        found.add(subtitle)
        st = os.stat(subtitle)
        row = rows.get(subtitle)
        if row and row["size"] == st.st_size and row["mtime"] == st.st_mtime:
            continue
        if time.time() < max(st.st_ctime, os.stat(video).st_ctime) + SETTLE_AFTER:
            continue
        todo.append((os.stat(video).st_ctime, subtitle, video))
    # Subtitles renamed or deleted since; only once the folders were read, an empty list means nothing then
    if locations or series_locations:
        with lock:
            for gone in set(rows) - found:
                db.execute("DELETE FROM subtitles WHERE path = ?", (gone,))
                if os.path.exists(synced_path(gone)):
                    os.remove(synced_path(gone))
    # Newest movies first, so a fresh import is checked before the rest of the library
    return [(subtitle, video) for _, subtitle, video in sorted(todo, reverse=True)]


def describe(offset, scale):
    parts = []
    if abs(offset) >= MIN_OFFSET:
        parts.append(f"{'закъсняват' if offset < 0 else 'избързват'} с {abs(offset):.2f} s")
    if abs(scale - 1) > 0.001:
        parts.append(f"скоростта се разминава ×{scale:.4f}" + (" (25 ↔ 23.976 fps)" if abs(abs(scale - 1) - 0.0427) < 0.003 else ""))
    return "; ".join(parts) or f"наред ({offset:+.2f} s)"


def safe_to_fix(offset, scale):
    """Whether an alignment can be applied without a look: a plain shift, or a shift with one of the usual frame
    rate conversions. Far shifts are mostly ffsubsync locking onto the wrong speech (it searches only ±60 s)."""
    return abs(offset) <= MAX_AUTO_OFFSET and any(abs(scale - s) < 0.0005 for s in SAFE_SCALES)


def apply_fix(row, auto=False):
    """Replaces a subtitle with its aligned copy, keeping the original as <subtitle>.orig; call with lock held."""
    path, out, orig = row["path"], synced_path(row["path"]), row["path"] + ".orig"
    try:
        st = os.stat(path)
    except FileNotFoundError:
        raise Conflict("the subtitle is gone")
    # The aligned copy is of the file as it was checked
    if st.st_size != row["size"] or st.st_mtime != row["mtime"] or not os.path.isfile(out):
        raise Conflict("the subtitle changed since the check; it is checked again")
    if os.path.exists(orig):
        raise Conflict(f"{os.path.basename(orig)} already exists")
    # The subtitle is rewritten in place, keeping its owner, mode and the NAS's ACLs: a new file would take
    # its mode from stat, which reads 000 here for files whose access UGOS grants through ACLs only
    shutil.copyfile(path, orig)
    os.chown(orig, st.st_uid, st.st_gid)
    os.chmod(orig, st.st_mode & 0o7777 or 0o777)
    shutil.copyfile(out, path)
    os.remove(out)
    new = os.stat(path)
    detail = row["detail"] + ("; поправени автоматично" if auto else "")
    db.execute("UPDATE subtitles SET state = 'fixed', size = ?, mtime = ?, checked_at = ?, detail = ? WHERE id = ?",
               (new.st_size, new.st_mtime, time.time(), detail, row["id"]))
    event(path, "fixed", f"{os.path.basename(path)}: {detail}; оригиналът е в {os.path.basename(orig)}", subtitle_label(path))


def auto_fix():
    """Applies every waiting alignment that's safe_to_fix; returns whether it did any."""
    with lock:
        rows = [r for r in db.execute("SELECT * FROM subtitles WHERE state = 'offset' ORDER BY checked_at DESC")
                if (r["id"], r["size"], r["mtime"]) not in not_fixed and safe_to_fix(r["offset"], r["scale"])]
    for row in rows:
        with lock:
            try:
                apply_fix(row, auto=True)
            except (Conflict, OSError) as e:
                not_fixed.add((row["id"], row["size"], row["mtime"]))
                log(f"error {subtitle_label(row['path'])}: {os.path.basename(row['path'])} not fixed: {e}")
                continue
        refresh_video(row["video"], subtitle_label(row["path"]))
    return bool(rows)


def align(video, subtitle, out):
    """(state, offset, scale, detail) of a subtitle against the speech in the video, its aligned copy in out:
    state is ok, offset (out holds the fix) or failed."""
    result, error = None, None
    try:
        # Its own process at the lowest priority, so a check never holds up playback or transcoding
        proc = subprocess.run(["nice", "-n", "19", sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync_check.py"), video, subtitle, out],
                              capture_output=True, text=True, timeout=SYNC_TIMEOUT)
        line = next((l for l in reversed(proc.stdout.splitlines()) if l.startswith("RESULT ")), None)
        if line:
            result = json.loads(line[len("RESULT "):])
        else:
            error = next((l.strip() for l in reversed((proc.stderr + proc.stdout).splitlines()) if l.strip()), f"exit code {proc.returncode}")
    except subprocess.TimeoutExpired:
        error = f"не приключи за {SYNC_TIMEOUT // 60} мин"
    if result and result["sync_was_successful"]:
        offset, scale = result["offset_seconds"], result["framerate_scale_factor"]
        state = "offset" if abs(offset) >= MIN_OFFSET or abs(scale - 1) > 0.001 else "ok"
        return state, offset, scale, describe(offset, scale)
    return "failed", None, None, error or "ffsubsync не намери съвпадение с говора"


def check_subtitle(subtitle, video):
    label = subtitle_label(subtitle)
    sync_run["current"] = f"{label}: проверява {os.path.basename(subtitle)}"
    st = os.stat(subtitle)
    out = synced_path(subtitle)
    state, offset, scale, detail = align(video, subtitle, out)
    if state != "offset" and os.path.exists(out):
        os.remove(out)
    with lock:
        db.execute("""INSERT INTO subtitles (path, video, size, mtime, checked_at, state, offset, scale, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                      ON CONFLICT(path) DO UPDATE SET video = excluded.video, size = excluded.size, mtime = excluded.mtime, checked_at = excluded.checked_at,
                      state = excluded.state, offset = excluded.offset, scale = excluded.scale, detail = excluded.detail""",
                   (subtitle, video, st.st_size, st.st_mtime, time.time(), state, offset, scale, detail))
    if state == "ok":
        log(f"checked {label}: {os.path.basename(subtitle)} {detail}")
    else:
        event(subtitle, "subtitles", f"{os.path.basename(subtitle)}: {detail}", label)
    sync_run["current"] = None


def refresh_video(video, label):
    # Jellyfin keeps an external subtitle's stream from the last scan and doesn't notice a rewrite or a new file
    try:
        # Listing every item takes seconds, and fixes and downloads come in runs
        if video not in item_ids["ids"] or time.time() - item_ids["at"] > 600:
            items = jellyfin("/Items", Recursive="true", IncludeItemTypes="Movie,Episode", Fields="Path", Limit=100000)["Items"]
            item_ids.update(at=time.time(), ids={i["Path"]: i["Id"] for i in items if i.get("Path")})
        if video in item_ids["ids"]:
            jellyfin(f"/Items/{item_ids['ids'][video]}/Refresh", "POST", MetadataRefreshMode="Default", ImageRefreshMode="Default")
        else:
            log(f"error {label}: no Jellyfin item for {os.path.basename(video)}, refresh it by hand")
    except (urllib.error.URLError, TimeoutError) as e:
        log(f"error {label}: Jellyfin refresh failed: {e!r}")


def video_fps(video):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=avg_frame_rate,r_frame_rate", "-of", "json", video],
                         capture_output=True, text=True, timeout=120).stdout
    stream = (json.loads(out or "{}").get("streams") or [{}])[0]
    for key in ("avg_frame_rate", "r_frame_rate"):
        num, _, den = stream.get(key, "0/0").partition("/")
        if float(den or 0) and float(num) / float(den) > 0:
            return float(num) / float(den)
    return None


def convert_microdvd(subtitle, video):
    """MicroDVD counts in frames, so it can't be aligned as is; it becomes an .srt, the original <name>.sub.orig."""
    label = subtitle_label(subtitle)
    video_stem = os.path.splitext(os.path.basename(video))[0]
    rest = os.path.basename(subtitle)[len(video_stem):-len(".sub")] + ".srt"
    target = os.path.join(os.path.dirname(subtitle), video_stem + with_language(subtitle, rest))
    if os.path.exists(target) or os.path.exists(subtitle + ".orig"):
        return False
    raw = open(subtitle, "rb").read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1251")
    # "{1}{1}23.976" names the frame rate the frames were counted at; without it they are the video's frames
    header = re.match(r"\s*\{[01]\}\{[01]\}(\d+(?:\.\d+)?)", text)
    fps = float(header.group(1)) if header and 10 <= float(header.group(1)) <= 61 else video_fps(video)
    if not fps:
        log(f"error {label}: no frame rate for {os.path.basename(subtitle)}")
        return False
    subs = pysubs2.SSAFile.from_string(text, format_="microdvd", fps=fps)
    subs.events = [e for e in subs.events if not (e.start < 100 and re.fullmatch(r"\d+(?:\.\d+)?", e.plaintext.strip()))]
    st = os.stat(subtitle)
    with open(target, "w", encoding="utf-8") as f:
        f.write(subs.to_string("srt"))
    # A new file's mode reads 000 here (UGOS ACLs), see fix_subtitle
    os.chown(target, st.st_uid, st.st_gid)
    os.chmod(target, st.st_mode & 0o7777 or 0o777)
    os.rename(subtitle, subtitle + ".orig")
    event(target, "converted", f"{os.path.basename(subtitle)} → {os.path.basename(target)} ({fps:.3f} fps, {len(subs)} реда); оригиналът е в {os.path.basename(subtitle)}.orig", label)
    refresh_video(video, label)
    return True


def bulgarian_stream(stream):
    return (stream.get("Language") or "").lower() in ("bul", "bg") or bool(re.search(r"bulgar|бълг|\bbg\b", stream.get("Title") or "", re.I))


def bulgarian_text(text):
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum("Ѐ" <= c <= "ӿ" for c in letters) / len(letters) > 0.3


def bulgarian_files(video):
    """The Bulgarian subtitle files next to a video: a bg language in the name, or Cyrillic text."""
    folder, stem = os.path.dirname(video), os.path.splitext(os.path.basename(video))[0]
    found = []
    for name in os.listdir(folder):
        rest = name[len(stem):]
        if not name.startswith(stem + ".") or not SUBTITLE.fullmatch(rest):
            continue
        path = os.path.join(folder, name)
        if any(LANGUAGES.get(part.lower()) == "bg" for part in rest.split(".")[1:-1]) or subtitle_language(path) == "bg":
            found.append(path)
    return found


def note_review(review, folder, library, match, decision, video, item):
    group = review.setdefault(folder, {"folder": folder, "library": library, "title": match.group(1), "year": int(match.group(2)),
                                       "decision": decision, "videos": 0, "sample": os.path.basename(video),
                                       "audio": collections.Counter(), "added": ""})
    group["videos"] += 1
    audio = [s for s in item.get("MediaStreams") or [] if s["Type"] == "Audio"]
    group["audio"][", ".join(f"{s.get('Language') or 'und'} {s.get('Codec')} {s.get('Channels')}ch" for s in audio) or "без звук"] += 1
    group["added"] = max(group["added"], item.get("DateCreated") or "")


def wanted_subtitles():
    """Every organized movie and episode without Bulgarian sound or a good Bulgarian subtitle, newest first, and per
    folder the ones whose sound names no language, for the user to say whether that is a Bulgarian dub. A Bulgarian
    subtitle that failed its check or is too far off to fix on its own is in "replace"."""
    with lock:
        bad = {r["path"] for r in db.execute("SELECT path, state, offset, scale FROM subtitles WHERE state IN ('failed', 'offset')")
               if r["state"] == "failed" or not safe_to_fix(r["offset"], r["scale"])}
        decisions = dict(db.execute("SELECT folder, decision FROM audio").fetchall())
    folders = {f["Name"]: f for f in jellyfin("/Library/VirtualFolders")}
    wanted, review = [], {}
    # The Bulgarian libraries are dubbed
    for name in [n for n in LIBRARIES + SERIES_LIBRARIES if n in folders and not n.endswith("български")]:
        library, series = folders[name], name in SERIES_LIBRARIES
        items = jellyfin("/Items", ParentId=library["ItemId"], Recursive="true", IncludeItemTypes="Episode" if series else "Movie",
                         Fields="Path,MediaStreams,DateCreated", Limit=100000)["Items"]
        for item in items:
            video = item.get("Path") or ""
            location = next((l for l in library["Locations"] if video.startswith(l.rstrip("/") + "/")), None)
            top = location and os.path.relpath(video, location).split("/")[0]
            m = top and re.fullmatch(r"(.+) \((\d{4})\) \[imdbid-(tt\d+)\]", top)
            if not m or not os.path.isfile(video):
                continue
            if series and (item.get("ParentIndexNumber") is None or item.get("IndexNumber") is None or item.get("IndexNumberEnd")):
                continue
            folder = os.path.join(location.rstrip("/"), top)
            streams = item.get("MediaStreams") or []
            audio = [s for s in streams if s["Type"] == "Audio"]
            untagged = any((s.get("Language") or "und").lower() == "und" for s in audio)
            if folder in decisions:
                note_review(review, folder, name, m, decisions[folder], video, item)
            if decisions.get(folder) == "bg":
                continue
            if re.search(r"bg[ ._-]?audio", video, re.I) or any(bulgarian_stream(s) for s in audio):
                continue
            if any(s["Type"] == "Subtitle" and not s.get("IsExternal") and bulgarian_stream(s) for s in streams):
                continue
            files = bulgarian_files(video)
            if files and not set(files) <= bad:
                continue
            # Sound without a language is often a Bulgarian dub, kids' films most of all: the user says which
            if untagged and folder not in decisions:
                note_review(review, folder, name, m, None, video, item)
                continue
            fps = next((s.get("RealFrameRate") or s.get("AverageFrameRate") for s in streams if s["Type"] == "Video"), None)
            wanted.append({"video": video, "title": m.group(1), "year": int(m.group(2)), "imdb": m.group(3), "fps": fps,
                           "season": item["ParentIndexNumber"] if series else None, "episode": item["IndexNumber"] if series else None,
                           "replace": files, "added": item.get("DateCreated") or ""})
    wanted.sort(key=lambda w: w["added"], reverse=True)
    for group in review.values():
        group["audio"] = group["audio"].most_common(1)[0][0]
    return wanted, review


def search_subtitles(w):
    """The listings for a video's title, best first; a series is searched once for all its episodes."""
    key = w["imdb"]
    cached = searches.get(key)
    if not cached or time.time() - cached[0] > SEARCH_TTL:
        found = bg_subtitles.sab_search(w["imdb"])
        found += bg_subtitles.unacs_search(w["title"].split(" - ")[0], None, w["imdb"])
        searches[key] = cached = (time.time(), found)

    def rank(c):
        if w["season"] is None:
            numbers = 0
        else:
            numbers = {(w["season"], w["episode"]): 0, (w["season"], None): 1, (None, None): 2}.get(c["numbers"], 9)
        fps_differs = bool(w["fps"] and c["fps"] and abs(w["fps"] - c["fps"]) > 0.01)
        return numbers, fps_differs, -c["downloads"]

    return [c for c in sorted(cached[1], key=rank) if rank(c)[0] < 9]


def subtitle_entries(c, w):
    """The files of a listing that could be the video's subtitle."""
    entries = [(n, raw) for n, raw in bg_subtitles.files(c, SUBTITLE_CACHE) if os.path.splitext(n)[1].lower() in SYNC_EXTS | {".sub"}]
    if w["season"] is None:
        # A film split over several files doesn't fit one video
        return [] if len(entries) > 1 and any(re.search(r"(?:cd|part|disc)[ ._-]?\d", n, re.I) for n, _ in entries) else entries

    def numbers(name):
        m = EPISODE.search(name) or re.search(r"(?<!\d)(\d{1,2})x(\d{2,3})(?!\d)", name)
        return (int(m.group(1)), int(m.group(2))) if m else None

    wanted = (w["season"], w["episode"])
    matching = [(n, raw) for n, raw in entries if numbers(n) == wanted]
    # A listing for one episode whose file doesn't say which
    if not matching and c["numbers"] == wanted and len(entries) == 1 and numbers(entries[0][0]) is None:
        matching = entries
    return matching


def subtitle_text(name, raw, fps):
    """A downloaded subtitle as (extension, UTF-8 text), MicroDVD turned into .srt; None when it isn't Bulgarian text."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1251", errors="replace")
    if not bulgarian_text(text[:200_000]):
        return None
    ext = os.path.splitext(name)[1].lower()
    if ext == ".sub":
        if not MICRODVD.match(text.encode()[:64]) or not fps:
            return None
        subs = pysubs2.SSAFile.from_string(text, format_="microdvd", fps=fps)
        return ".srt", subs.to_string("srt")
    return ext, text


def install_subtitle(w, source, ext, c, state, offset, scale, detail):
    video = w["video"]
    target = os.path.splitext(video)[0] + ".bg" + ext
    for old in w["replace"]:
        aside = old + ".replaced"
        if os.path.exists(aside):
            aside = f"{old}.replaced-{int(time.time())}"
        os.rename(old, aside)
    if os.path.exists(target):
        raise Conflict(f"{os.path.basename(target)} already exists")
    shutil.copyfile(source, target)
    st = os.stat(video)
    # A new file's mode reads 000 here (UGOS ACLs), see apply_fix
    os.chown(target, st.st_uid, st.st_gid)
    os.chmod(target, 0o666)
    new = os.stat(target)
    detail = f"{c['site']}: {c['title']}" + (f"; {detail}, изравнени" if state == "offset" else "")
    with lock:
        # Already checked here, so the subtitle check doesn't run ffsubsync on it again
        db.execute("""INSERT INTO subtitles (path, video, size, mtime, checked_at, state, offset, scale, detail) VALUES (?, ?, ?, ?, ?, 'ok', ?, ?, ?)
                      ON CONFLICT(path) DO UPDATE SET video = excluded.video, size = excluded.size, mtime = excluded.mtime, checked_at = excluded.checked_at,
                      state = 'ok', offset = excluded.offset, scale = excluded.scale, detail = excluded.detail""",
                   (target, video, new.st_size, new.st_mtime, time.time(), offset, scale, "изтеглени от " + detail))
        replaced = f"; заменени {', '.join(os.path.basename(p) for p in w['replace'])}" if w["replace"] else ""
        event(target, "bgsub", f"{os.path.basename(target)} от {detail}{replaced}", subtitle_label(target))
    refresh_video(video, subtitle_label(target))
    return target


def record_download(video, state, tried, detail, subtitle=None):
    with lock:
        db.execute("""INSERT INTO downloads (video, state, subtitle, tried, checked_at, detail) VALUES (?, ?, ?, ?, ?, ?)
                      ON CONFLICT(video) DO UPDATE SET state = excluded.state, subtitle = excluded.subtitle, tried = excluded.tried,
                      checked_at = excluded.checked_at, detail = excluded.detail""",
                   (video, state, subtitle, json.dumps(sorted(tried)), time.time(), detail))


def download_subtitle(w, tried):
    """Tries the listings for one video until a subtitle lines up with its speech."""
    label = subtitle_label(w["video"])
    candidate = os.path.join(SUBTITLE_CACHE, "candidate")
    attempts, listed = 0, 0
    for c in search_subtitles(w):
        try:
            entries = subtitle_entries(c, w)
        except (OSError, ValueError, urllib.error.URLError, subprocess.SubprocessError) as e:
            log(f"error {label}: {c['site']} {c['id']}: {e!r}")
            continue
        for name, raw in entries:
            key = f"{c['site']}:{c['id']}:{name}"
            listed += 1
            if key in tried:
                continue
            if attempts == MAX_TRIES:
                return "more", f"пробвани {len(tried)} досега, продължава при следващото обхождане", None
            converted = subtitle_text(name, raw, c["fps"] or w["fps"])
            tried.add(key)
            if not converted:
                continue
            attempts += 1
            ext, text = converted
            sync_run["current"] = f"{label}: проверява {c['site']}: {name}"
            with open(candidate + ext, "w", encoding="utf-8") as f:
                f.write(text)
            aligned = candidate + ".aligned" + ext
            state, offset, scale, detail = align(w["video"], candidate + ext, aligned)
            if state == "ok" or (state == "offset" and safe_to_fix(offset, scale)):
                target = install_subtitle(w, aligned if state == "offset" else candidate + ext, ext, c, state, offset, scale, detail)
                return "found", f"{c['site']}: {c['title']} ({name})", target
            log(f"rejected {label}: {c['site']}: {name}: {detail}")
    if not listed:
        return "none", f"няма в сайтовете; пак след {RETRY_NONE // 86400} дни", None
    return "nomatch", f"пробвани {len(tried)}, никои не съвпаднаха с говора; пак след {RETRY_NOMATCH // 86400} дни", None


def download_step():
    """Looks for Bulgarian subtitles for the next video that needs them; returns whether there was one."""
    if time.time() < downloads_run["paused_until"]:
        return False
    if time.time() - downloads_run["listed_at"] > WANTED_REFRESH:
        downloads_run["wanted"], downloads_run["review"] = wanted_subtitles()
        downloads_run["listed_at"] = time.time()
    with lock:
        rows = {r["video"]: r for r in db.execute("SELECT * FROM downloads")}
    retry = {"none": RETRY_NONE, "nomatch": RETRY_NOMATCH, "found": RETRY_NOMATCH}
    for w in downloads_run["wanted"]:
        row = rows.get(w["video"])
        if row and (row["checked_at"] > downloads_run["listed_at"] or time.time() - row["checked_at"] < retry.get(row["state"], 0)):
            continue
        label = subtitle_label(w["video"])
        sync_run["current"] = f"{label}: търси български субтитри"
        tried = set(json.loads(row["tried"])) if row else set()
        try:
            state, detail, target = download_subtitle(w, tried)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            log(f"error subtitle sites: {e!r}; tried again in an hour")
            downloads_run["paused_until"] = time.time() + 3600
            return False
        except (Conflict, OSError) as e:
            state, detail, target = "failed", str(e), None
            log(f"error {label}: {e}")
        record_download(w["video"], state, tried, detail, target)
        if state != "found":
            log(f"{state} {label}: {os.path.basename(w['video'])}: {detail}")
        return True
    return False


def subtitle_loop():
    while True:
        sync_wake.clear()
        try:
            for subtitle, video in list(subtitle_files({".sub"})):
                if os.path.splitext(subtitle)[0] + ".idx" not in os.listdir(os.path.dirname(subtitle)):
                    with open(subtitle, "rb") as f:
                        if MICRODVD.match(f.read(64).lstrip(b"\xef\xbb\xbf")):
                            sync_run["current"] = f"{subtitle_label(subtitle)}: превръща {os.path.basename(subtitle)} в .srt"
                            convert_microdvd(subtitle, video)
            sync_run["current"] = None
            auto_fix()
            todo = pending_subtitles()
            sync_run["queued"] = len(todo)
            if todo:
                check_subtitle(*todo[0])
                continue
            # Only when every subtitle on disk is checked: a new download's own check comes first
            if download_step():
                continue
        except Exception as e:
            log(f"error subtitles: {e!r}")
            sync_run["current"] = None
        sync_wake.wait(INTERVAL)


app = FastAPI(title="jellyfin-movie-ingest")


@app.get("/")
def index():
    # Revalidated on every load, so a redeploy shows up without a hard reload
    return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"), headers={"Cache-Control": "no-cache"})


@app.get("/api/status")
def api_status():
    with lock:
        items = list(status.values())
        recent = [dict(r) for r in db.execute("SELECT at, path, detail FROM events WHERE kind = 'moved' ORDER BY at DESC, id DESC LIMIT 30")]
        subtitles = [{**dict(r), "label": subtitle_label(r["path"])} for r in db.execute("SELECT id, path, checked_at, state, offset, scale, detail FROM subtitles WHERE state != 'ok' ORDER BY checked_at DESC")]
        counts = dict(db.execute("SELECT state, COUNT(*) FROM subtitles GROUP BY state").fetchall())
        downloads = [{**dict(r), "label": subtitle_label(r["video"])} for r in db.execute(
            "SELECT video, state, checked_at, detail FROM downloads WHERE state != 'none' ORDER BY checked_at DESC LIMIT 200")]
        download_counts = dict(db.execute("SELECT state, COUNT(*) FROM downloads GROUP BY state").fetchall())
        review = sorted(downloads_run["review"].values(), key=lambda g: (g["decision"] is not None, g["library"], g["title"]))
    return {"review": review, "run": last_run, "items": items, "recent": recent, "subtitles": {"run": sync_run, "counts": counts, "items": subtitles},
            "downloads": {"counts": download_counts, "wanted": len(downloads_run["wanted"]), "items": downloads}}


@app.get("/api/activity")
def api_activity(after: int = 0):
    with lock:
        lines = [a for a in activity if a["seq"] > after]
    return {"run": last_run, "sync": sync_run, "lines": lines, "boot": BOOT}


@app.post("/api/run")
def run_now():
    wake.set()
    return {"run": last_run}


@app.post("/api/items/{item_id}/confirm")
def confirm(item_id: str):
    with lock:
        entry = status.get(item_id)
        if not entry:
            raise HTTPException(404, "unknown item")
        if not entry["confirm"]:
            raise HTTPException(409, f"item is {entry['state']}")
        db.execute("INSERT OR REPLACE INTO confirmed VALUES (?, ?, ?)", (entry["path"], entry["tag"], time.time()))
        event(entry["path"], "confirmed", f"{entry['tag']} → {entry['target']}", f"{entry['library']}/{entry['file']}")
        entry.update(state="pending", detail="потвърден, чака реда си", confirm=False)
    wake.set()
    return entry


def subtitle_row(sub_id, state):
    row = db.execute("SELECT * FROM subtitles WHERE id = ?", (sub_id,)).fetchone()
    if not row:
        raise HTTPException(404, "unknown subtitle")
    if row["state"] != state:
        raise HTTPException(409, f"subtitle is {row['state']}")
    return row


@app.post("/api/subtitles/{sub_id}/fix")
def fix_subtitle(sub_id: int):
    with lock:
        row = subtitle_row(sub_id, "offset")
        try:
            apply_fix(row)
        except Conflict as e:
            raise HTTPException(409, str(e))
    refresh_video(row["video"], subtitle_label(row["path"]))
    return {"ok": True}


@app.post("/api/subtitles/{sub_id}/dismiss")
def dismiss_subtitle(sub_id: int):
    with lock:
        row = subtitle_row(sub_id, "offset")
        db.execute("UPDATE subtitles SET state = 'dismissed' WHERE id = ?", (sub_id,))
        if os.path.exists(synced_path(row["path"])):
            os.remove(synced_path(row["path"]))
        event(row["path"], "dismissed", f"{os.path.basename(row['path'])}: {row['detail']}", subtitle_label(row["path"]))
    return {"ok": True}


@app.post("/api/subtitles/{sub_id}/restore")
def restore_subtitle(sub_id: int):
    with lock:
        row = subtitle_row(sub_id, "fixed")
        path, orig = row["path"], row["path"] + ".orig"
        if not os.path.isfile(orig):
            raise HTTPException(409, f"{os.path.basename(orig)} is gone")
        # Rewritten in place for the same reason as in apply_fix
        shutil.copyfile(orig, path)
        os.remove(orig)
        st = os.stat(path)
        # Recorded as checked, so the restored file isn't aligned again
        db.execute("UPDATE subtitles SET state = 'restored', size = ?, mtime = ?, checked_at = ? WHERE id = ?", (st.st_size, st.st_mtime, time.time(), sub_id))
        event(path, "restored", f"{os.path.basename(path)}: върнат оригиналът", subtitle_label(path))
    refresh_video(row["video"], subtitle_label(path))
    return {"ok": True}


tag_lock = threading.Lock()
AUDIO_DECISIONS = {"bg": "български дублаж", "subs": "трябват субтитри", None: "без отговор"}


def mkv_audio_untagged(path):
    """Positions (from 1, in mkvpropedit's track:aN) of a file's sound tracks with no language, as Jellyfin reads them."""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream_tags=language", "-of", "json", path],
                         capture_output=True, text=True, timeout=120)
    streams = json.loads(out.stdout).get("streams", []) if out.stdout else []
    return [i + 1 for i, s in enumerate(streams) if (s.get("tags", {}).get("language") or "und").lower() == "und"]


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                         capture_output=True, text=True, timeout=120)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return None


def remux_bulgarian(path, position):
    """Rewrites a video whose container can't name a track's language as MKV with that sound track (its position
    among the sound tracks) set to Bulgarian; returns the MKV's path. The streams are copied, not encoded again,
    and the MKV keeps the name, so subtitles, NFO and images next to it still go with it."""
    target = os.path.splitext(path)[0] + ".mkv"
    if os.path.exists(target):
        raise Conflict(f"{os.path.basename(target)} already exists")
    tracks = json.loads(subprocess.run(["mkvmerge", "-J", path], capture_output=True, text=True, timeout=120).stdout or "{}").get("tracks", [])
    audio = [t["id"] for t in tracks if t["type"] == "audio"]
    if len(audio) < position:
        raise Conflict(f"mkvmerge sees {len(audio)} sound tracks")
    part = target + ".part"
    r = subprocess.run(["mkvmerge", "-q", "-o", part, "--language", f"{audio[position - 1]}:bg", path], capture_output=True, text=True, timeout=3600)
    try:
        # 1 is warnings only
        if r.returncode > 1:
            raise Conflict(f"mkvmerge: {(r.stdout + r.stderr).strip()[-300:]}")
        before, after = duration(path), duration(part)
        if not before or not after or abs(before - after) > 2:
            raise Conflict(f"MKV lasts {after} s, the original {before} s")
        st = os.stat(path)
        os.chown(part, st.st_uid, st.st_gid)
        os.chmod(part, 0o666)
        os.rename(part, target)
    finally:
        if os.path.exists(part):
            os.remove(part)
    os.remove(path)
    with lock:
        db.execute("UPDATE subtitles SET video = ? WHERE video = ?", (target, path))
        db.execute("DELETE FROM downloads WHERE video = ?", (path,))
        event(target, "remuxed", f"{os.path.basename(path)} → MKV с българско аудио", subtitle_label(target))
    return target


REMUX_EXTS = (".avi", ".mp4", ".m4v", ".mpg", ".mpeg")


def tag_audio(folder, decision):
    """For a dub, sets the one sound track with no language in every video under folder to Bulgarian, so Jellyfin
    and players see it too: in place in an MKV, by rewriting the rest as MKV. Other answers put the MKV tracks set
    this way back to no language; a rewritten video stays MKV."""
    with lock:
        row = db.execute("SELECT tagged FROM audio WHERE folder = ?", (folder,)).fetchone()
    done, changed, remuxed = json.loads(row["tagged"]) if row else [], [], 0

    def save():
        with lock:
            db.execute("UPDATE audio SET tagged = ? WHERE folder = ?", (json.dumps(done), folder))

    if decision == "bg":
        for root, _, names in os.walk(folder):
            for n in sorted(names):
                path, ext = os.path.join(root, n), os.path.splitext(n)[1].lower()
                if ext not in (".mkv",) + REMUX_EXTS or any(p == path for p, _ in done):
                    continue
                untagged = mkv_audio_untagged(path)
                # With two untagged tracks it can't be told which is the dub
                if len(untagged) != 1:
                    continue
                if ext != ".mkv":
                    try:
                        done.append([remux_bulgarian(path, untagged[0]), untagged[0]])
                        remuxed += 1
                        save()
                    except (Conflict, OSError, subprocess.TimeoutExpired) as e:
                        log(f"error {subtitle_label(path)}: {os.path.basename(path)} not rewritten as MKV: {e}")
                    continue
                r = subprocess.run(["mkvpropedit", path, "--edit", f"track:a{untagged[0]}", "--set", "language=bul", "--set", "language-ietf=bg"],
                                   capture_output=True, text=True, timeout=300)
                if r.returncode == 0:
                    done.append([path, untagged[0]])
                    changed.append(path)
                else:
                    log(f"error {subtitle_label(path)}: mkvpropedit: {(r.stdout + r.stderr).strip()[-300:]}")
    else:
        for path, number in done:
            if os.path.isfile(path):
                subprocess.run(["mkvpropedit", path, "--edit", f"track:a{number}", "--set", "language=und", "--delete", "language-ietf"],
                               capture_output=True, timeout=300)
                changed.append(path)
        done = []
    save()
    for path in changed:
        refresh_video(path, subtitle_label(path))
    # Jellyfin only finds the new files on a scan
    if remuxed:
        try:
            jellyfin("/Library/Refresh", "POST")
        except (urllib.error.URLError, TimeoutError) as e:
            log(f"error Jellyfin scan after rewriting as MKV failed: {e!r}")
    if changed or remuxed:
        log(f"{'bul' if decision == 'bg' else 'und'} audio: {len(changed)} MKV, {remuxed} rewritten as MKV in {os.path.basename(folder)}")


@app.post("/api/audio")
def audio_decision(body: dict):
    folder, decision = body.get("folder"), body.get("decision")
    if decision not in ("bg", "subs", None):
        raise HTTPException(400, "decision is bg, subs or null")
    # Only folders from the list, so no request names an arbitrary path
    group = downloads_run["review"].get(folder)
    if not group:
        raise HTTPException(404, "unknown folder")
    with lock:
        if decision:
            db.execute("""INSERT INTO audio (folder, decision, decided_at) VALUES (?, ?, ?)
                          ON CONFLICT(folder) DO UPDATE SET decision = excluded.decision, decided_at = excluded.decided_at""",
                       (folder, decision, time.time()))
        group["decision"] = decision
        event(folder, "audio", f"{group['title']}: {AUDIO_DECISIONS[decision]}", group["title"])
    def apply():
        # One at a time, so quick changes of mind are applied in order
        with tag_lock:
            tag_audio(folder, decision)
            if not decision:
                with lock:
                    db.execute("DELETE FROM audio WHERE folder = ?", (folder,))
    threading.Thread(target=apply, daemon=True).start()
    # The list of videos to find subtitles for is made again on the next step
    downloads_run["listed_at"] = 0
    return {"ok": True}


if __name__ == "__main__":
    threading.Thread(target=ingest_loop, daemon=True).start()
    threading.Thread(target=subtitle_loop, daemon=True).start()
    if QBIT_URL:
        threading.Thread(target=download_loop, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
