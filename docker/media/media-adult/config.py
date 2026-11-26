import os

from common.qbit import path_map

# Movies under <Studio>/<Title (Year)> [tpdbid-...]/, matched in ThePornDB; scenes under <Studio>/, never looked up
LIBRARY = os.environ.get("LIBRARY", "Порно")
SCENES_LIBRARY = os.environ.get("SCENES_LIBRARY", "Порно - сцени")
# How long the loop rests between passes over the libraries
INTERVAL = int(os.environ.get("INTERVAL", "300"))
PORT = int(os.environ.get("PORT", "8100"))
DATA_DIR = os.environ.get("DATA_DIR", "/data")
# How many of a movie's performers the NFOs name, as for the films' actors
MAX_CAST = int(os.environ.get("MAX_CAST", "20"))
# The performers' photos, tpdb-<id>.jpg, next to the TMDB actors' that media-ingest keeps
PEOPLE_DIR = os.environ.get("PEOPLE_DIR", "/people")
# A studio's folder -> its name in Jellyfin where the two are spelled apart
STUDIO_NAMES = {"Adam&Eve": "Adam & Eve"}
# Studios whose ThePornDB sites are named in several ways ("Wicked Pictures", ...): matched to this one folder
MERGE_STUDIOS = [s.strip() for s in os.environ.get("MERGE_STUDIOS", "Wicked").split(",") if s.strip()]

# Finished torrents in this qBittorrent category are taken over from qBittorrent and matched where they are, in its
# download folder on the disk of a library folder; the category is kept pointed at the one with the most free space
CATEGORY = os.environ.get("CATEGORY", "Порно")
# The scenes' category, downloaded into SCENES_SUBDIR of the same download folder and placed once their studio is picked
SCENES_CATEGORY = os.environ.get("SCENES_CATEGORY", "Порно - сцени")
SCENES_SUBDIR = "Сцени"
# qBittorrent's download folders (its own paths) and how they are seen here, e.g. /volume6/MoviesAdult1=/moviesadult1
DOWNLOAD_DIRS = [s.strip() for s in os.environ.get("DOWNLOAD_DIRS", "").split(",") if s.strip()]
QBIT_PATHS = path_map(os.environ.get("QBIT_PATHS", ""))
DOWNLOAD_INTERVAL = int(os.environ.get("DOWNLOAD_INTERVAL", "60"))
# A file copied in by hand is left alone until it has not changed for this long
SETTLE_AFTER = 120
