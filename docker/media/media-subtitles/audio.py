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

# The folder's own record of the answer: {"audio": "bg" | "subs" | "burned"}
MEDIA_FILE = ".media.json"
DECISIONS = {
    "bg": "български дублаж",
    "subs": "трябват субтитри",
    "burned": "субтитрите са в картината",
    None: "без отговор",
}
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
        before, after = duration(path), duration(target)
        # A restart between writing the MKV and removing the original
        if not before or not after or abs(before - after) > 2:
            raise Conflict(f"{os.path.basename(target)} already exists")
        os.remove(path)
        with lock:
            db.execute("UPDATE subtitles SET video = ? WHERE video = ?", (target, path))
            db.execute("DELETE FROM downloads WHERE video = ?", (path,))
        return target
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


def resume():
    """Goes on with the dub answers a restart cut short (rewriting a series as MKV takes hours); what is done is
    skipped. Answers given before they were kept in the folders are written there first."""
    with lock:
        answers = db.execute("SELECT folder, decision FROM audio").fetchall()
    for folder, decision in answers:
        if os.path.isdir(folder) and read_decision(folder) != decision:
            try:
                write_decision(folder, decision)
            except OSError as e:
                log(f"error {folder}: {MEDIA_FILE} not written: {e}")
    with lock:
        folders = [r[0] for r in db.execute("SELECT folder FROM audio WHERE decision = 'bg' ORDER BY decided_at")]
    for folder in folders:
        with tag_lock:
            with lock:
                still = db.execute("SELECT decision FROM audio WHERE folder = ?", (folder,)).fetchone()
            if still and still[0] == "bg":
                tag_audio(folder, "bg")


def read_decision(folder):
    """The answer kept in the folder itself, so it outlives the database (PLAN_media_services.md, 2.3)."""
    try:
        with open(os.path.join(folder, MEDIA_FILE), encoding="utf-8") as f:
            return json.load(f).get("audio")
    except (OSError, ValueError, AttributeError):
        return None


def write_decision(folder, decision):
    """Sets or (None) removes the answer in the folder's .media.json, keeping anything else in it."""
    path = os.path.join(folder, MEDIA_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if decision:
        data["audio"] = decision
    else:
        data.pop("audio", None)
    if not data:
        if os.path.exists(path):
            os.remove(path)
        return
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    parent = os.stat(folder)
    os.chown(temporary, parent.st_uid, parent.st_gid)
    os.chmod(temporary, 0o666)
    os.replace(temporary, path)


def restore(folders, decisions):
    """Takes the answers kept in the folders into the database where it has none (a new or lost database)."""
    for folder in folders:
        if folder in decisions:
            continue
        decision = read_decision(folder)
        if decision and decision in DECISIONS:
            with lock:
                db.execute(
                    "INSERT OR IGNORE INTO audio (folder, decision, decided_at) VALUES (?, ?, ?)",
                    (folder, decision, os.path.getmtime(os.path.join(folder, MEDIA_FILE))),
                )
            decisions[folder] = decision


def answer(folder, decision, group):
    """Records the user's answer and applies it to the files in the background."""
    with lock:
        if decision:
            db.execute(
                """INSERT INTO audio (folder, decision, decided_at) VALUES (?, ?, ?)
                   ON CONFLICT(folder) DO UPDATE SET decision = excluded.decision, decided_at = excluded.decided_at""",
                (folder, decision, time.time()),
            )
            write_decision(folder, decision)
        group["decision"] = decision
        event(folder, "audio", f"{group['title']}: {DECISIONS[decision]}", group["title"])

    def apply():
        with tag_lock:
            tag_audio(folder, decision)
            if not decision:
                with lock:
                    db.execute("DELETE FROM audio WHERE folder = ?", (folder,))
                write_decision(folder, None)

    threading.Thread(target=apply, daemon=True).start()
