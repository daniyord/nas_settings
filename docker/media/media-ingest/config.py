import os
import re


def names(text):
    return [s.strip() for s in text.split(",") if s.strip()]


def path_map(text):
    return [tuple(p.strip().split("=", 1)) for p in text.split(",") if "=" in p]


LIBRARIES = names(os.environ.get("LIBRARIES", "Филми,Филми - детски,Филми - български,Филми - руски"))
# Only series in a release folder (its name has a season in it) are organized; the rest are left alone
SERIES_LIBRARIES = names(os.environ.get("SERIES_LIBRARIES", "Сериали,Сериали - детски,Сериали - български"))
INTERVAL = int(os.environ.get("INTERVAL", "300"))
PORT = int(os.environ.get("PORT", "8098"))
DATA_DIR = os.environ.get("DATA_DIR", "/data")
# Finished torrents whose qBittorrent category is the name of one of LIBRARIES or SERIES_LIBRARIES are moved
# into it; empty turns this off
QBIT_URL = os.environ.get("QBIT_URL", "").rstrip("/")
QBIT_USER = os.environ.get("QBIT_USER", "")
QBIT_PASSWORD = os.environ.get("QBIT_PASSWORD", "")
DOWNLOAD_INTERVAL = int(os.environ.get("DOWNLOAD_INTERVAL", "60"))
# qBittorrent's paths as seen here, e.g. /downloads=/volume1/Downloads
QBIT_PATHS = path_map(os.environ.get("QBIT_PATHS", ""))
# qBittorrent's download folder on each disk; a library's category is pointed at the one with the most free space
DOWNLOAD_DIRS = names(os.environ.get("DOWNLOAD_DIRS", ""))
# Jellyfin's library folders as seen through the whole-volume mounts: a move within one mount is a rename,
# while across two mounts (even of the same disk) it would be a copy
VOLUME_PATHS = path_map(os.environ.get("VOLUME_PATHS", ""))

# One photo per actor, <tmdb-id>.jpg, at the same path inside Jellyfin; NFOs point to them. Missing: no photos
PEOPLE_DIR = os.environ.get("PEOPLE_DIR", "/people")
# As Jellyfin's own TMDB provider: the first actors only
MAX_CAST = int(os.environ.get("MAX_CAST", "15"))
# The collections' folders (collection.xml and artwork), mounted where Jellyfin keeps them (boxsets.py)
COLLECTIONS_DIR = os.environ.get("COLLECTIONS_DIR", "/collections")
# Libraries whose folders from before our NFOs are brought to them, in this order (PLAN_media_services.md, stage 3)
MIGRATE_LIBRARIES = names(os.environ.get("MIGRATE_LIBRARIES", ""))
STALE_RESCAN = 300
MIN_SIMILARITY = 0.8
# Where a release name stops being the series' name; series folders named by hand have none of it and are left alone
SEASON_TOKEN = re.compile(r"(?<![a-zа-я0-9])(?:s\d{1,2}(?:[ ._-]?e\d{1,3})?|season|сезон)(?![a-zа-я])", re.I)
# What Jellyfin saves next to episodes and seasons; it alone would keep an emptied release folder alive
JELLYFIN_FILES = {".nfo", ".jpg", ".jpeg", ".png", ".webp", ".tbn"}
# Order of the Bulgarian alphabet; Jellyfin transliterates Cyrillic, so sort names spell it out as codes
# (PLAN_jellyfin_library.md)
ALPHABET = "абвгдежзийклмнопрстуфхцчшщъьюя"
