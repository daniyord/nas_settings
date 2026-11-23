"""The text subtitles of every organized movie and episode are aligned to the speech in the video (ffsubsync); a shift
of up to MAX_AUTO_OFFSET, alone or with a usual frame rate conversion, is fixed right away, anything else is fixed
from the status page, keeping the original as <subtitle>.orig either way."""

import collections
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

import pysubs2

from common.media import ORGANIZED, SETTLE_AFTER, VIDEO_EXTS, Conflict, with_language
from config import MAX_AUTO_OFFSET, MICRODVD, MIN_OFFSET, SAFE_SCALES, SYNC_EXTS, SYNC_TIMEOUT, SYNCED_DIR
from state import db, event, lock, locations, log, not_fixed, refresh_video, series_locations, subtitle_label, sync_run


def synced_path(subtitle):
    return os.path.join(
        SYNCED_DIR, hashlib.sha1(subtitle.encode()).hexdigest()[:16] + os.path.splitext(subtitle)[1].lower()
    )


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


def pending_subtitles():
    """The subtitles to check, newest videos first. A subtitle renamed along with its video (media-ingest) keeps its
    check: rename keeps a file's size and mtime, so a new path with those of a path that's gone is that file."""
    with lock:
        rows = {r["path"]: (r["size"], r["mtime"]) for r in db.execute("SELECT path, size, mtime FROM subtitles")}
    files = list(subtitle_files())
    # Only once the folders were read: an empty list means nothing then
    if locations or series_locations:
        found = {subtitle for subtitle, _ in files}
        gone = collections.defaultdict(list)
        for path in set(rows) - found:
            gone[rows[path]].append(path)
        with lock:
            for subtitle, video in files:
                if subtitle in rows:
                    continue
                st = os.stat(subtitle)
                same = gone.get((st.st_size, st.st_mtime))
                if same:
                    old = same.pop()
                    db.execute("UPDATE subtitles SET path = ?, video = ? WHERE path = ?", (subtitle, video, old))
                    if os.path.exists(synced_path(old)):
                        os.rename(synced_path(old), synced_path(subtitle))
                    rows[subtitle] = (st.st_size, st.st_mtime)
            for path in (p for paths in gone.values() for p in paths):
                db.execute("DELETE FROM subtitles WHERE path = ?", (path,))
                if os.path.exists(synced_path(path)):
                    os.remove(synced_path(path))
    todo = []
    for subtitle, video in files:
        st = os.stat(subtitle)
        if rows.get(subtitle) == (st.st_size, st.st_mtime):
            continue
        if time.time() < max(st.st_ctime, os.stat(video).st_ctime) + SETTLE_AFTER:
            continue
        todo.append((os.stat(video).st_ctime, subtitle, video))
    # Newest movies first, so a fresh import is checked before the rest of the library
    return [(subtitle, video) for _, subtitle, video in sorted(todo, reverse=True)]


def describe(offset, scale):
    parts = []
    if abs(offset) >= MIN_OFFSET:
        parts.append(f"{'закъсняват' if offset < 0 else 'избързват'} с {abs(offset):.2f} s")
    if abs(scale - 1) > 0.001:
        conversion = " (25 ↔ 23.976 fps)" if abs(abs(scale - 1) - 0.0427) < 0.003 else ""
        parts.append(f"скоростта се разминава ×{scale:.4f}{conversion}")
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
        raise Conflict("the subtitle is gone") from None
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
    db.execute(
        "UPDATE subtitles SET state = 'fixed', size = ?, mtime = ?, checked_at = ?, detail = ? WHERE id = ?",
        (new.st_size, new.st_mtime, time.time(), detail, row["id"]),
    )
    event(
        path,
        "fixed",
        f"{os.path.basename(path)}: {detail}; оригиналът е в {os.path.basename(orig)}",
        subtitle_label(path),
    )


def auto_fix():
    """Applies every waiting alignment that's safe_to_fix; returns whether it did any."""
    with lock:
        rows = [
            r
            for r in db.execute("SELECT * FROM subtitles WHERE state = 'offset' ORDER BY checked_at DESC")
            if (r["id"], r["size"], r["mtime"]) not in not_fixed and safe_to_fix(r["offset"], r["scale"])
        ]
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
    checker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync_check.py")
    try:
        # Its own process at the lowest priority, so a check never holds up playback or transcoding
        proc = subprocess.run(
            ["nice", "-n", "19", sys.executable, checker, video, subtitle, out],
            capture_output=True,
            text=True,
            timeout=SYNC_TIMEOUT,
        )
        line = next((x for x in reversed(proc.stdout.splitlines()) if x.startswith("RESULT ")), None)
        if line:
            result = json.loads(line[len("RESULT ") :])
        else:
            lines = (proc.stderr + proc.stdout).splitlines()
            error = next((x.strip() for x in reversed(lines) if x.strip()), f"exit code {proc.returncode}")
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
        db.execute(
            """INSERT INTO subtitles (path, video, size, mtime, checked_at, state, offset, scale, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET video = excluded.video, size = excluded.size, mtime = excluded.mtime,
               checked_at = excluded.checked_at, state = excluded.state, offset = excluded.offset,
               scale = excluded.scale, detail = excluded.detail""",
            (subtitle, video, st.st_size, st.st_mtime, time.time(), state, offset, scale, detail),
        )
    if state == "ok":
        log(f"checked {label}: {os.path.basename(subtitle)} {detail}")
    else:
        event(subtitle, "subtitles", f"{os.path.basename(subtitle)}: {detail}", label)
    sync_run["current"] = None


def video_fps(video):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=avg_frame_rate,r_frame_rate"]
        + ["-of", "json", video],
        capture_output=True,
        text=True,
        timeout=120,
    ).stdout
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
    rest = os.path.basename(subtitle)[len(video_stem) : -len(".sub")] + ".srt"
    target = os.path.join(os.path.dirname(subtitle), video_stem + with_language(subtitle, rest))
    if os.path.exists(target) or os.path.exists(subtitle + ".orig"):
        return False
    with open(subtitle, "rb") as f:
        raw = f.read()
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
    subs.events = [
        e for e in subs.events if not (e.start < 100 and re.fullmatch(r"\d+(?:\.\d+)?", e.plaintext.strip()))
    ]
    st = os.stat(subtitle)
    with open(target, "w", encoding="utf-8") as f:
        f.write(subs.to_string("srt"))
    # A new file's mode reads 000 here (UGOS ACLs), see apply_fix
    os.chown(target, st.st_uid, st.st_gid)
    os.chmod(target, st.st_mode & 0o7777 or 0o777)
    os.rename(subtitle, subtitle + ".orig")
    detail = f"{os.path.basename(subtitle)} → {os.path.basename(target)} ({fps:.3f} fps, {len(subs)} реда)"
    event(target, "converted", f"{detail}; оригиналът е в {os.path.basename(subtitle)}.orig", label)
    refresh_video(video, label)
    return True


def convert_microdvd_files():
    """Every MicroDVD .sub (a .sub without an .idx is text) becomes an .srt."""
    for subtitle, video in list(subtitle_files({".sub"})):
        if os.path.splitext(subtitle)[0] + ".idx" in os.listdir(os.path.dirname(subtitle)):
            continue
        with open(subtitle, "rb") as f:
            if MICRODVD.match(f.read(64).lstrip(b"\xef\xbb\xbf")):
                sync_run["current"] = f"{subtitle_label(subtitle)}: превръща {os.path.basename(subtitle)} в .srt"
                convert_microdvd(subtitle, video)
    sync_run["current"] = None
