# Matches new items in the adult Jellyfin library to ThePornDB movies. The plugin's own search
# ignores the studio and picks random scenes, so items are matched here by title within the
# studio of their folder (<library>/<Studio>/<Title>.mp4) and runtime, and the plugin is then
# pointed at the right ThePornDB id. Items that can't be matched with confidence are logged
# and left alone; locked items are never touched. A status page on PORT shows what was matched
# and takes per-item keywords (extra titles to search and compare) to retry a failed item with.

import difflib
import json
import os
import re
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
KEYWORDS_FILE = os.path.join(os.environ.get("DATA_DIR", "/data"), "keywords.json")
# ThePornDB names a studio's sites differently ("Wicked", "Wicked Pictures", ...); these collapse to one name
MERGE_STUDIOS = [s.strip() for s in os.environ.get("MERGE_STUDIOS", "").split(",") if s.strip()]
RETRY_AFTER = 24 * 3600
MIN_SIMILARITY = 0.9
MAX_RUNTIME_OFF_MINUTES = 15
# Gives the plugin's refresh time to write the NFO and artwork next to the file before it is moved
ORGANIZE_AFTER = 600
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# Like Jellyfin's [imdbid-tt...]: a ThePornDB id in a file or folder name pins the match, no search involved
ID_TAG = re.compile(r"\s*\[tpdbid-(" + UUID + r")\]", re.I)
# A keyword that is a ThePornDB movie link (uuid, slug or numeric id) or a bare uuid pins the match too
TPDB_REF = re.compile(r"(?:https?://)?(?:www\.)?theporndb\.net/movies/([\w-]+)/?|(" + UUID + ")", re.I)
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".wmv", ".m4v", ".mov", ".mpg", ".mpeg", ".ts", ".webm"}

skipped = {}
matched_at = {}
status = {}
lock = threading.RLock()
wake = threading.Event()
last_run = {"started": None, "finished": None, "error": None, "interval": INTERVAL}


def load_keywords():
    try:
        with open(KEYWORDS_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


# Keyed by file path rather than item id, so keywords survive the item being re-added to the library
keywords_by_path = load_keywords()


def save_keywords():
    os.makedirs(os.path.dirname(KEYWORDS_FILE), exist_ok=True)
    with open(KEYWORDS_FILE + ".tmp", "w") as f:
        json.dump(keywords_by_path, f, ensure_ascii=False, indent=1)
    os.replace(KEYWORDS_FILE + ".tmp", KEYWORDS_FILE)


def request(url, headers, method="GET", body=None):
    data = None if body is None else json.dumps(body).encode()
    headers = {**headers, "Accept": "application/json", "Content-Type": "application/json", "User-Agent": "jellyfin-tpdb-match"}
    with urllib.request.urlopen(urllib.request.Request(url, data, headers, method=method), timeout=60) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


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


def norm(text):
    return re.sub(r"[^a-z0-9]", "", text.lower().replace("&", "and"))


# Prefix rather than substring, so "Wicked" covers "Wicked Pictures" but not "Raw Wicked Love Affair"
def same_studio(folder, name):
    a, b = norm(folder), norm(name)
    return bool(a and b) and (b.startswith(a) or a.startswith(b))


def find_match(token, title, studio, year, minutes, keywords=()):
    # A keyword is compared on its own (a full alternative title) and appended to the file title
    # ("Cinderella" + "XXX Axel Braun Parody"), so either style of hint works
    texts = [title, *keywords] + ([f"{title} {' '.join(keywords)}"] if keywords else [])
    sites = {studio} | {s["name"] for s in tpdb(token, "sites", q=studio, per_page=50) if same_studio(studio, s["name"])}
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
    print(f"studios {label}: {', '.join(names)} -> {', '.join(merged)}", flush=True)


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


# Gives a matched movie its own folder named "<Title (Year)> [tpdbid-<id>]": a movie lying directly in its
# studio folder moves there with its sidecar files, a movie already alone in a folder has the tag appended
def organize(item, parts, label):
    provider = re.fullmatch(r"movies/(" + UUID + ")", item.get("ProviderIds", {}).get("ThePornDB", ""), re.I)
    if not provider or not os.path.isfile(item["Path"]):
        return
    tag = f"[tpdbid-{provider.group(1).lower()}]"
    folder, filename = os.path.split(item["Path"])
    stem = os.path.splitext(filename)[0]

    if len(parts) == 2:
        year = item.get("ProductionYear")
        base = stem if not year or re.search(r"\(\d{4}\)\s*$", stem) else f"{stem} ({year})"
        source, target = item["Path"], os.path.join(folder, f"{base} {tag}")
        moves = [(os.path.join(folder, n), os.path.join(target, n)) for n in [filename, *sidecars(folder, stem)]]
    elif len(parts) == 3 and not ID_TAG.search(parts[1]):
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
    with lock:
        if len(parts) == 2:
            moved = {item["Path"]: os.path.join(target, filename)} if item["Path"] in keywords_by_path else {}
        else:
            moved = {p: target + p[len(source):] for p in keywords_by_path if p.startswith(source + "/")}
        for old, new in moved.items():
            keywords_by_path[new] = keywords_by_path.pop(old)
        if moved:
            save_keywords()
    jellyfin("/Library/Media/Updated", "POST", {"Updates": [
        {"Path": source, "UpdateType": "Deleted"},
        {"Path": target, "UpdateType": "Created"},
    ]})
    print(f"organize {label} -> {os.path.basename(target)}/", flush=True)


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

    items = jellyfin("/Items", ParentId=library["ItemId"], Recursive="true", IncludeItemTypes="Movie", Fields="Path,ProviderIds,Studios", Limit=100000)["Items"]
    # Classify everything first so the status page shows the whole library before the slow ThePornDB lookups
    todo = []
    for item in items:
        location = next((l for l in library["Locations"] if item["Path"].startswith(l.rstrip("/") + "/")), None)
        parts = item["Path"][len(location.rstrip("/")) + 1:].split("/") if location else []
        if len(parts) < 2:
            record(item, "", os.path.basename(item["Path"]), "ignored", "not inside a studio folder")
            continue
        studio = parts[0]
        name = ID_TAG.sub("", os.path.splitext(parts[-1])[0]).strip()
        tag = ID_TAG.search(item["Path"])
        provider = item.get("ProviderIds", {}).get("ThePornDB")
        # A tagged item is matched only once it points at the tagged id; retagging a folder re-points it
        if provider and (provider.lower() == f"movies/{tag.group(1).lower()}" if tag else any(same_studio(studio, s["Name"]) for s in item.get("Studios", []))):
            if current_state(item["Id"]) != "matched":
                record(item, studio, name, "matched", item["Name"])
            merge_studios(item, admin, f"{studio}/{name}")
            if time.time() - matched_at.get(item["Id"], 0) > ORGANIZE_AFTER:
                organize(item, parts, f"{studio}/{name}")
            continue
        if current_state(item["Id"]) not in ("skipped", "locked"):
            record(item, studio, name, "pending", "waiting for the matcher")
        todo.append((item, studio, name, tag))

    with lock:
        for gone in set(status) - {item["Id"] for item in items}:
            del status[gone]

    for item, studio, name, tag in todo:
        if time.time() - skipped.get(item["Id"], 0) < RETRY_AFTER:
            continue

        full = jellyfin(f"/Items/{item['Id']}", userId=admin["Id"])
        if full.get("LockData"):
            record(item, studio, name, "locked", "metadata is locked in Jellyfin")
            continue

        year_match = re.search(r"\((\d{4})\)\s*$", name)
        year = int(year_match.group(1)) if year_match else None
        title = re.sub(r"\s*\(\d{4}\)\s*$", "", name)
        minutes = (full.get("RunTimeTicks") or 0) / 600_000_000
        with lock:
            keywords = list(keywords_by_path.get(item["Path"], []))

        refs = [m.group(1) or m.group(2) for k in keywords if (m := TPDB_REF.fullmatch(k.strip()))]
        ref = tag.group(1) if tag else (refs[0] if refs else None)
        if ref:
            movie, reason, candidates = tpdb_movie(token, ref), None, []
            if not movie:
                reason = f"ThePornDB has no movie {ref}"
        else:
            movie, reason, candidates = find_match(token, title, studio, year, minutes, keywords)
        if not movie:
            print(f"skip {studio}/{name}: {reason}", flush=True)
            skipped[item["Id"]] = time.time()
            record(item, studio, name, "skipped", reason, candidates)
            continue

        full["ProviderIds"] = {"ThePornDB": f"movies/{movie['id']}"}
        jellyfin(f"/Items/{item['Id']}", "POST", full)
        jellyfin(f"/Items/{item['Id']}/Refresh", "POST", MetadataRefreshMode="FullRefresh", ImageRefreshMode="FullRefresh", ReplaceAllMetadata="true", ReplaceAllImages="true")
        match = f"{movie['title']} ({movie['site']['name']}, {(movie.get('date') or '')[:4]})"
        print(f"matched {studio}/{name} -> {match}", flush=True)
        matched_at[item["Id"]] = time.time()
        record(item, studio, name, "matched", match)


def matcher_loop():
    while True:
        wake.clear()
        last_run["started"] = time.time()
        try:
            run_once()
            last_run["error"] = None
        except Exception as e:
            print(f"error: {e!r}", flush=True)
            last_run["error"] = repr(e)
        last_run["finished"] = time.time()
        wake.wait(INTERVAL)


app = FastAPI(title="jellyfin-tpdb-match")


class Retry(BaseModel):
    keywords: list[str] = []


@app.get("/")
def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))


@app.get("/api/status")
def api_status():
    with lock:
        items = [{**s, "keywords": keywords_by_path.get(s["path"], [])} for s in status.values()]
    return {"run": last_run, "items": items}


@app.post("/api/items/{item_id}/retry")
def retry(item_id: str, body: Retry):
    with lock:
        entry = status.get(item_id)
        if not entry:
            raise HTTPException(404, "unknown item")
        if entry["state"] not in ("skipped", "pending"):
            raise HTTPException(409, f"item is {entry['state']}")
        keywords = [k.strip() for k in body.keywords if k.strip()]
        if keywords:
            keywords_by_path[entry["path"]] = keywords
        else:
            keywords_by_path.pop(entry["path"], None)
        save_keywords()
        skipped.pop(item_id, None)
        entry.update(state="pending", detail="queued for retry", checked=time.time())
    wake.set()
    return {**entry, "keywords": keywords}


if __name__ == "__main__":
    threading.Thread(target=matcher_loop, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
