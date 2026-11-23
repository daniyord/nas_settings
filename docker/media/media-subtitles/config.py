import os
import re


def names(text):
    return [s.strip() for s in text.split(",") if s.strip()]


LIBRARIES = names(os.environ.get("LIBRARIES", "Филми,Филми - детски,Филми - български,Филми - руски"))
SERIES_LIBRARIES = names(os.environ.get("SERIES_LIBRARIES", "Сериали,Сериали - детски,Сериали - български"))
# How long the loop rests when every subtitle is checked and nothing waits to be downloaded
INTERVAL = int(os.environ.get("INTERVAL", "300"))
PORT = int(os.environ.get("PORT", "8099"))
DATA_DIR = os.environ.get("DATA_DIR", "/data")

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
# Containers that can't name a sound track's language; a dub in one is rewritten as MKV
REMUX_EXTS = (".avi", ".mp4", ".m4v", ".mpg", ".mpeg")
