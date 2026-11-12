# Matches new items in the adult Jellyfin library to ThePornDB movies. The plugin's own search
# ignores the studio and picks random scenes, so items are matched here by title within the
# studio of their folder (<library>/<Studio>/<Title>.mp4) and runtime, and the plugin is then
# pointed at the right ThePornDB id. Items that can't be matched with confidence are logged
# and left alone; locked items are never touched. A status page on PORT shows what was matched.

import difflib
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse

JELLYFIN_URL = os.environ["JELLYFIN_URL"].rstrip("/")
JELLYFIN_API_KEY = os.environ["JELLYFIN_API_KEY"]
LIBRARY = os.environ.get("LIBRARY", "Порно")
INTERVAL = int(os.environ.get("INTERVAL", "300"))
PORT = int(os.environ.get("PORT", "8097"))
# ThePornDB names a studio's sites differently ("Wicked", "Wicked Pictures", ...); these collapse to one name
MERGE_STUDIOS = [s.strip() for s in os.environ.get("MERGE_STUDIOS", "").split(",") if s.strip()]
RETRY_AFTER = 24 * 3600
MIN_SIMILARITY = 0.9
MAX_RUNTIME_OFF_MINUTES = 15

skipped = {}
status = {}
status_lock = threading.Lock()
last_run = {"started": None, "finished": None, "error": None, "interval": INTERVAL}


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


def norm(text):
    return re.sub(r"[^a-z0-9]", "", text.lower().replace("&", "and"))


# Prefix rather than substring, so "Wicked" covers "Wicked Pictures" but not "Raw Wicked Love Affair"
def same_studio(folder, name):
    a, b = norm(folder), norm(name)
    return bool(a and b) and (b.startswith(a) or a.startswith(b))


def find_match(token, title, studio, year, minutes):
    sites = {studio} | {s["name"] for s in tpdb(token, "sites", q=studio, per_page=50) if same_studio(studio, s["name"])}
    found = {}
    for query in {title, title.replace(" - ", ": "), title.split(" - ")[0]}:
        for params in [{"q": query, "site": site} for site in sites] + [{"q": query}]:
            for movie in tpdb(token, "movies", per_page=50, **params):
                if same_studio(studio, (movie.get("site") or {}).get("name", "")):
                    found[movie["id"]] = movie

    scored = []
    for movie in found.values():
        similarity = round(difflib.SequenceMatcher(None, norm(title), norm(movie["title"])).ratio(), 2)
        movie_year = int(movie["date"][:4]) if movie.get("date") else None
        movie_minutes = (movie.get("duration") or 0) / 60
        off = abs(movie_minutes - minutes) if movie_minutes and minutes else None
        scored.append((similarity, movie_year, off, movie))

    best_similarity = max((s[0] for s in scored), default=0)
    if best_similarity < MIN_SIMILARITY:
        return None, "no title match within the studio"
    scored = [s for s in scored if s[0] == best_similarity]
    if year and any(s[1] == year for s in scored):
        scored = [s for s in scored if s[1] == year]
    scored.sort(key=lambda s: s[2] if s[2] is not None else float("inf"))

    best = scored[0]
    if best[2] is None:
        return (best[3], None) if len(scored) == 1 else (None, f"{len(scored)} candidates and no runtime to tell them apart")
    if best[2] > MAX_RUNTIME_OFF_MINUTES:
        return None, f"runtime differs by {best[2]:.0f} min"
    # ThePornDB often lists one release several times (re-releases, "Wicked" vs "Wicked Pictures");
    # candidates this close in runtime are the same movie, so take the original release
    same_movie = [s for s in scored if s[2] is not None and s[2] - best[2] < 5]
    return min(same_movie, key=lambda s: s[3].get("date") or "9999")[3], None


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


def record(item_id, studio, name, state, detail):
    with status_lock:
        status[item_id] = {"id": item_id, "studio": studio, "file": name, "state": state, "detail": detail, "checked": time.time()}


def current_state(item_id):
    with status_lock:
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
            record(item["Id"], "", os.path.basename(item["Path"]), "ignored", "not inside a studio folder")
            continue
        studio = parts[0]
        name = os.path.splitext(parts[-1])[0]
        if item.get("ProviderIds", {}).get("ThePornDB") and any(same_studio(studio, s["Name"]) for s in item.get("Studios", [])):
            if current_state(item["Id"]) != "matched":
                record(item["Id"], studio, name, "matched", item["Name"])
            merge_studios(item, admin, f"{studio}/{name}")
            continue
        if current_state(item["Id"]) not in ("skipped", "locked"):
            record(item["Id"], studio, name, "pending", "waiting for the matcher")
        todo.append((item, studio, name))

    with status_lock:
        for gone in set(status) - {item["Id"] for item in items}:
            del status[gone]

    for item, studio, name in todo:
        if time.time() - skipped.get(item["Id"], 0) < RETRY_AFTER:
            continue

        full = jellyfin(f"/Items/{item['Id']}", userId=admin["Id"])
        if full.get("LockData"):
            record(item["Id"], studio, name, "locked", "metadata is locked in Jellyfin")
            continue

        year_match = re.search(r"\((\d{4})\)\s*$", name)
        year = int(year_match.group(1)) if year_match else None
        title = re.sub(r"\s*\(\d{4}\)\s*$", "", name)
        minutes = (full.get("RunTimeTicks") or 0) / 600_000_000

        movie, reason = find_match(token, title, studio, year, minutes)
        if not movie:
            print(f"skip {studio}/{name}: {reason}", flush=True)
            skipped[item["Id"]] = time.time()
            record(item["Id"], studio, name, "skipped", reason)
            continue

        full["ProviderIds"] = {"ThePornDB": f"movies/{movie['id']}"}
        jellyfin(f"/Items/{item['Id']}", "POST", full)
        jellyfin(f"/Items/{item['Id']}/Refresh", "POST", MetadataRefreshMode="FullRefresh", ImageRefreshMode="FullRefresh", ReplaceAllMetadata="true", ReplaceAllImages="true")
        match = f"{movie['title']} ({movie['site']['name']}, {(movie.get('date') or '')[:4]})"
        print(f"matched {studio}/{name} -> {match}", flush=True)
        record(item["Id"], studio, name, "matched", match)


def matcher_loop():
    while True:
        last_run["started"] = time.time()
        try:
            run_once()
            last_run["error"] = None
        except Exception as e:
            print(f"error: {e!r}", flush=True)
            last_run["error"] = repr(e)
        last_run["finished"] = time.time()
        time.sleep(INTERVAL)


app = FastAPI(title="jellyfin-tpdb-match")


@app.get("/")
def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))


@app.get("/api/status")
def api_status():
    with status_lock:
        items = list(status.values())
    return {"run": last_run, "items": items}


if __name__ == "__main__":
    threading.Thread(target=matcher_loop, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
