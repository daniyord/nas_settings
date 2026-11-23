"""What the user says about a movie's or series' folder whose one sound track names no language: a Bulgarian dub
(then marked as such in the files) or a film that wants subtitles."""

import json
import os
import subprocess
import threading
import time
import urllib.error

from common.jellyfin import jellyfin
from common.media import Conflict
from config import REMUX_EXTS
from state import db, event, lock, log, refresh_video, subtitle_label

DECISIONS = {"bg": "български дублаж", "subs": "трябват субтитри", None: "без отговор"}
# One answer at a time, so quick changes of mind are applied in order
tag_lock = threading.Lock()


def audio_untagged(path):
    """Positions (from 1, in mkvpropedit's track:aN) of a file's sound tracks with no language, as Jellyfin reads them."""
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream_tags=language",
            "-of",
            "json",
            path,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    streams = json.loads(out.stdout).get("streams", []) if out.stdout else []
    return [i + 1 for i, s in enumerate(streams) if (s.get("tags", {}).get("language") or "und").lower() == "und"]


def duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True,
        text=True,
        timeout=120,
    )
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
    probe = subprocess.run(["mkvmerge", "-J", path], capture_output=True, text=True, timeout=120)
    tracks = json.loads(probe.stdout or "{}").get("tracks", [])
    audio = [t["id"] for t in tracks if t["type"] == "audio"]
    if len(audio) < position:
        raise Conflict(f"mkvmerge sees {len(audio)} sound tracks")
    part = target + ".part"
    r = subprocess.run(
        ["mkvmerge", "-q", "-o", part, "--language", f"{audio[position - 1]}:bg", path],
        capture_output=True,
        text=True,
        timeout=3600,
    )
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
                untagged = audio_untagged(path)
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
                r = subprocess.run(
                    ["mkvpropedit", path, "--edit", f"track:a{untagged[0]}", "--set", "language=bul"]
                    + ["--set", "language-ietf=bg"],
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                if r.returncode == 0:
                    done.append([path, untagged[0]])
                    changed.append(path)
                else:
                    log(f"error {subtitle_label(path)}: mkvpropedit: {(r.stdout + r.stderr).strip()[-300:]}")
    else:
        for path, number in done:
            if os.path.isfile(path):
                subprocess.run(
                    [
                        "mkvpropedit",
                        path,
                        "--edit",
                        f"track:a{number}",
                        "--set",
                        "language=und",
                        "--delete",
                        "language-ietf",
                    ],
                    capture_output=True,
                    timeout=300,
                )
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
        language = "bul" if decision == "bg" else "und"
        log(f"{language} audio: {len(changed)} MKV, {remuxed} rewritten as MKV in {os.path.basename(folder)}")


def answer(folder, decision, group):
    """Records the user's answer and applies it to the files in the background."""
    with lock:
        if decision:
            db.execute(
                """INSERT INTO audio (folder, decision, decided_at) VALUES (?, ?, ?)
                   ON CONFLICT(folder) DO UPDATE SET decision = excluded.decision, decided_at = excluded.decided_at""",
                (folder, decision, time.time()),
            )
        group["decision"] = decision
        event(folder, "audio", f"{group['title']}: {DECISIONS[decision]}", group["title"])

    def apply():
        with tag_lock:
            tag_audio(folder, decision)
            if not decision:
                with lock:
                    db.execute("DELETE FROM audio WHERE folder = ?", (folder,))

    threading.Thread(target=apply, daemon=True).start()
