"""Bulgarian subtitles for every organized movie and episode with sound other than Bulgarian and no good Bulgarian
subtitle, from subs.sab.bz and subsunacs.net (bg_subtitles.py); one is kept only when it lines up with the speech."""

import collections
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error

import pysubs2

import bg_subtitles
from common.jellyfin import jellyfin
from common.media import EPISODE, LANGUAGES, SUBTITLE, Conflict, bulgarian_text, subtitle_language
from config import (
    LIBRARIES,
    MAX_TRIES,
    MICRODVD,
    RETRY_NOMATCH,
    RETRY_NONE,
    SEARCH_TTL,
    SERIES_LIBRARIES,
    SUBTITLE_CACHE,
    SYNC_EXTS,
    WANTED_REFRESH,
)
from state import db, downloads_run, event, lock, log, refresh_video, searches, subtitle_label, sync_run
from sync import align, safe_to_fix


def bulgarian_stream(stream):
    language = (stream.get("Language") or "").lower()
    return language in ("bul", "bg") or bool(re.search(r"bulgar|бълг|\bbg\b", stream.get("Title") or "", re.I))


def bulgarian_files(video):
    """The Bulgarian subtitle files next to a video: a bg language in the name, or Cyrillic text."""
    folder, stem = os.path.dirname(video), os.path.splitext(os.path.basename(video))[0]
    found = []
    for name in os.listdir(folder):
        rest = name[len(stem) :]
        if not name.startswith(stem + ".") or not SUBTITLE.fullmatch(rest):
            continue
        path = os.path.join(folder, name)
        named = any(LANGUAGES.get(part.lower()) == "bg" for part in rest.split(".")[1:-1])
        if named or subtitle_language(path) == "bg":
            found.append(path)
    return found


def note_review(review, folder, library, match, decision, video, item):
    group = review.setdefault(
        folder,
        {
            "folder": folder,
            "library": library,
            "title": match.group(1),
            "year": int(match.group(2)),
            "decision": decision,
            "videos": 0,
            "sample": os.path.basename(video),
            "audio": collections.Counter(),
            "added": "",
        },
    )
    group["videos"] += 1
    audio = [s for s in item.get("MediaStreams") or [] if s["Type"] == "Audio"]
    tracks = ", ".join(f"{s.get('Language') or 'und'} {s.get('Codec')} {s.get('Channels')}ch" for s in audio)
    group["audio"][tracks or "без звук"] += 1
    group["added"] = max(group["added"], item.get("DateCreated") or "")


def wanted_subtitles():
    """Every organized movie and episode with sound other than Bulgarian and no good Bulgarian subtitle, newest first,
    and per folder the ones whose one sound track names no language, for the user to say whether that is a Bulgarian
    dub. A Bulgarian subtitle that failed its check or is too far off to fix on its own is in "replace"."""
    with lock:
        bad = {
            r["path"]
            for r in db.execute("SELECT path, state, offset, scale FROM subtitles WHERE state IN ('failed', 'offset')")
            if r["state"] == "failed" or not safe_to_fix(r["offset"], r["scale"])
        }
        decisions = dict(db.execute("SELECT folder, decision FROM audio").fetchall())
    folders = {f["Name"]: f for f in jellyfin("/Library/VirtualFolders")}
    wanted, review = [], {}
    # The Bulgarian libraries are dubbed
    for name in [n for n in LIBRARIES + SERIES_LIBRARIES if n in folders and not n.endswith("български")]:
        library, series = folders[name], name in SERIES_LIBRARIES
        items = jellyfin(
            "/Items",
            ParentId=library["ItemId"],
            Recursive="true",
            IncludeItemTypes="Episode" if series else "Movie",
            Fields="Path,MediaStreams,DateCreated",
            Limit=100000,
        )["Items"]
        for item in items:
            video = item.get("Path") or ""
            location = next((loc for loc in library["Locations"] if video.startswith(loc.rstrip("/") + "/")), None)
            if not location:
                continue
            top = os.path.relpath(video, location).split("/")[0]
            m = re.fullmatch(r"(.+) \((\d{4})\) \[imdbid-(tt\d+)\]", top)
            if not m or not os.path.isfile(video):
                continue
            if series and (
                item.get("ParentIndexNumber") is None or item.get("IndexNumber") is None or item.get("IndexNumberEnd")
            ):
                continue
            folder = os.path.join(location.rstrip("/"), top)
            streams = item.get("MediaStreams") or []
            audio = [s for s in streams if s["Type"] == "Audio"]
            # One sound track without a language is often a Bulgarian dub, kids' films most of all: the user says which.
            # Next to a Bulgarian track another one is the original, which is watched with subtitles
            single = (
                len(audio) == 1
                and (audio[0].get("Language") or "und").lower() == "und"
                and not bulgarian_stream(audio[0])
            )
            if folder in decisions:
                note_review(review, folder, name, m, decisions[folder], video, item)
            if single and (decisions.get(folder) == "bg" or re.search(r"bg[ ._-]?audio", video, re.I)):
                continue
            if audio and all(bulgarian_stream(s) for s in audio):
                continue
            if any(s["Type"] == "Subtitle" and not s.get("IsExternal") and bulgarian_stream(s) for s in streams):
                continue
            files = bulgarian_files(video)
            if files and not set(files) <= bad:
                continue
            if single and folder not in decisions:
                note_review(review, folder, name, m, None, video, item)
                continue
            fps = next(
                (s.get("RealFrameRate") or s.get("AverageFrameRate") for s in streams if s["Type"] == "Video"), None
            )
            wanted.append(
                {
                    "video": video,
                    "title": m.group(1),
                    "year": int(m.group(2)),
                    "imdb": m.group(3),
                    "fps": fps,
                    "season": item["ParentIndexNumber"] if series else None,
                    "episode": item["IndexNumber"] if series else None,
                    "replace": files,
                    "added": item.get("DateCreated") or "",
                }
            )
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
    entries = [
        (n, raw)
        for n, raw in bg_subtitles.files(c, SUBTITLE_CACHE)
        if os.path.splitext(n)[1].lower() in SYNC_EXTS | {".sub"}
    ]
    if w["season"] is None:
        # A film split over several files doesn't fit one video
        split = len(entries) > 1 and any(re.search(r"(?:cd|part|disc)[ ._-]?\d", n, re.I) for n, _ in entries)
        return [] if split else entries

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
        db.execute(
            """INSERT INTO subtitles (path, video, size, mtime, checked_at, state, offset, scale, detail)
               VALUES (?, ?, ?, ?, ?, 'ok', ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET video = excluded.video, size = excluded.size, mtime = excluded.mtime,
               checked_at = excluded.checked_at, state = 'ok', offset = excluded.offset, scale = excluded.scale,
               detail = excluded.detail""",
            (target, video, new.st_size, new.st_mtime, time.time(), offset, scale, "изтеглени от " + detail),
        )
        replaced = f"; заменени {', '.join(os.path.basename(p) for p in w['replace'])}" if w["replace"] else ""
        event(target, "bgsub", f"{os.path.basename(target)} от {detail}{replaced}", subtitle_label(target))
    refresh_video(video, subtitle_label(target))
    return target


def record_download(video, state, tried, detail, subtitle=None):
    with lock:
        db.execute(
            """INSERT INTO downloads (video, state, subtitle, tried, checked_at, detail) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(video) DO UPDATE SET state = excluded.state, subtitle = excluded.subtitle,
               tried = excluded.tried, checked_at = excluded.checked_at, detail = excluded.detail""",
            (video, state, subtitle, json.dumps(sorted(tried)), time.time(), detail),
        )


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
                source = aligned if state == "offset" else candidate + ext
                target = install_subtitle(w, source, ext, c, state, offset, scale, detail)
                return "found", f"{c['site']}: {c['title']} ({name})", target
            log(f"rejected {label}: {c['site']}: {name}: {detail}")
    if not listed:
        return "none", f"няма в сайтовете; пак след {RETRY_NONE // 86400} дни", None
    return (
        "nomatch",
        f"пробвани {len(tried)}, никои не съвпаднаха с говора; пак след {RETRY_NOMATCH // 86400} дни",
        None,
    )


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
        if row and (
            row["checked_at"] > downloads_run["listed_at"]
            or time.time() - row["checked_at"] < retry.get(row["state"], 0)
        ):
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
