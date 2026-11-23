"""What both services know about the library's files (docs/PLAN_jellyfin_library.md)."""

import os
import re

VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".wmv", ".m4v", ".mov", ".mpg", ".mpeg", ".ts", ".m2ts", ".webm", ".flv", ".divx", ".vob",
    ".iso", ".3gp",
}  # fmt: skip
SUBTITLE = re.compile(r"(?:\.[\w-]+)*\.(?:srt|ass|ssa|sub|idx|vtt|sup|smi)", re.I)
ORGANIZED = re.compile(r".+ \(\d{4}\) \[(?:imdbid-tt\d+|tmdbid-\d+)\]")
EPISODE = re.compile(r"(?<![a-z0-9])s(\d{1,2})[ ._-]?e(\d{1,3})(?:-?e(\d{1,3}))?(?!\d)", re.I)
LANGUAGES = {
    "bg": "bg", "bul": "bg", "bulgarian": "bg",
    "en": "en", "eng": "en", "english": "en",
    "ru": "ru", "rus": "ru", "russian": "ru",
}  # fmt: skip
# A file is left alone this long after its last change: ctime rather than mtime, as copy tools restore the source's
# mtime but every write moves ctime
SETTLE_AFTER = 120


class Conflict(Exception):
    pass


def bulgarian_text(text):
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum("Ѐ" <= c <= "ӿ" for c in letters) / len(letters) > 0.3


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
    # Cyrillic subtitles here are Bulgarian, also those of the Russian films
    return "bg" if bulgarian_text(text) else None


def with_language(path, rest):
    """rest is what follows the movie's name (".srt", ".bg.srt", ".en.forced.srt")."""
    if rest.count(".") > 1:
        return rest
    language = subtitle_language(path)
    return f".{language}{rest}" if language else rest
