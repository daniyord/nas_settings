"""A finished torrent in a qBittorrent category named after a library is moved into that library's folder on the
same disk (a rename, never a copy) and dropped from qBittorrent, which is where the movies and series steps take
over. That category is kept pointed at the download folder on the library's disk with the most free space; a
downloading episode of a series that's already organized is moved to the download folder on that series' disk."""

import os
import shutil
import time

from common.jellyfin import jellyfin
from common.media import ORGANIZED
from common.qbit import qbit, translate
from config import (
    DOWNLOAD_DIRS,
    DOWNLOAD_INTERVAL,
    LIBRARIES,
    QBIT_PATHS,
    SERIES_LIBRARIES,
    VOLUME_PATHS,
)
from names import norm, series_title
from state import event, failed_downloads, followed, log, wake


def library_devices(library):
    return {
        os.stat(host).st_dev
        for loc in library["Locations"]
        if (host := translate(loc, VOLUME_PATHS)) and os.path.isdir(host)
    }


def local_dir(path):
    """A qBittorrent download folder as seen here, when it is there."""
    local = translate(path, QBIT_PATHS)
    return local if local and os.path.isdir(local) else None


def choose_save_paths(folders):
    """Points each library's category at the download folder with the most free space among the disks the library is on."""
    categories = qbit("torrents/categories")
    for name in LIBRARIES + SERIES_LIBRARIES:
        if name not in folders:
            continue
        devices = library_devices(folders[name])
        options = [
            (shutil.disk_usage(local).free, path)
            for path in DOWNLOAD_DIRS
            if (local := local_dir(path)) and os.stat(local).st_dev in devices
        ]
        if not options:
            continue
        free, best = max(options)
        current = categories.get(name)
        if current and current.get("savePath", "").rstrip("/") == best.rstrip("/"):
            continue
        qbit("torrents/editCategory" if current else "torrents/createCategory", category=name, savePath=best)
        log(f"qBittorrent: {name} се сваля в {best} ({free / 1e12:.1f} TB свободни)")


def import_downloads(folders, torrents):
    for torrent in torrents:
        library, key = torrent["category"], torrent["hash"]
        if library not in LIBRARIES + SERIES_LIBRARIES or key in failed_downloads or torrent["state"] == "moving":
            continue
        label = f"{library}/{torrent['name']}"
        src = translate(torrent["content_path"], QBIT_PATHS)
        try:
            if library not in folders:
                raise OSError("библиотеката я няма в Jellyfin")
            if not src or not os.path.exists(src):
                raise OSError(f"{torrent['content_path']} не се вижда в контейнера (QBIT_PATHS)")
            device = os.stat(src).st_dev
            location, folder = next(
                (
                    (loc, host)
                    for loc in folders[library]["Locations"]
                    if (host := translate(loc, VOLUME_PATHS)) and os.path.isdir(host) and os.stat(host).st_dev == device
                ),
                (None, None),
            )
            if not location or not folder:
                raise OSError("библиотеката няма папка на диска, на който е свалено")
            name = os.path.basename(src.rstrip("/"))
            if os.path.lexists(os.path.join(folder, name)):
                raise OSError(f"{name} вече е в {location}")
            os.rename(src, os.path.join(folder, name))
        except OSError as e:
            failed_downloads.add(key)
            log(f"error {label}: {e}")
            continue
        qbit("torrents/delete", hashes=key, deleteFiles="false")
        path = os.path.join(location, name)
        jellyfin("/Library/Media/Updated", "POST", {"Updates": [{"Path": path, "UpdateType": "Created"}]})
        event(path, "downloaded", f"{torrent['content_path']} → {path}", label)
        wake.set()


def follow_series(folders, torrents):
    """Moves a downloading episode of a series that's already in its library to the download folder on that series' disk."""
    series = {}
    for torrent in torrents:
        name, key = torrent["category"], torrent["hash"]
        if name not in SERIES_LIBRARIES or name not in folders or torrent["progress"] >= 1 or key in followed:
            continue
        if name not in series:
            series[name] = jellyfin(
                "/Items",
                ParentId=folders[name]["ItemId"],
                Recursive="true",
                IncludeItemTypes="Series",
                Fields="Path,OriginalTitle",
                Limit=100000,
            )["Items"]
        followed.add(key)
        title = series_title(torrent["name"])
        homes = {
            host
            for s in series[name]
            if ORGANIZED.fullmatch(os.path.basename(s.get("Path", "").rstrip("/")))
            and title in (norm(s.get("OriginalTitle") or ""), norm(s["Name"]))
            and (host := translate(s["Path"], VOLUME_PATHS))
            and os.path.isdir(host)
        }
        if len(homes) != 1:
            continue
        home = homes.pop()
        device = os.stat(home).st_dev
        target = next(
            (path for path in DOWNLOAD_DIRS if (local := local_dir(path)) and os.stat(local).st_dev == device), None
        )
        current = translate(torrent["save_path"], QBIT_PATHS)
        if not target or (current and os.path.isdir(current) and os.stat(current).st_dev == device):
            continue
        qbit("torrents/setLocation", hashes=key, location=target)
        detail = f"{torrent['name']}: сваля се в {target}, при {os.path.basename(home)}"
        event(home, "followed", detail, f"{name}/{torrent['name']}")


def download_loop():
    while True:
        try:
            folders = {f["Name"]: f for f in jellyfin("/Library/VirtualFolders")}
            choose_save_paths(folders)
            torrents = qbit("torrents/info")
            follow_series(folders, torrents)
            import_downloads(folders, [t for t in torrents if t["progress"] >= 1])
        except Exception as e:
            log(f"error qBittorrent: {e!r}")
        time.sleep(DOWNLOAD_INTERVAL)
