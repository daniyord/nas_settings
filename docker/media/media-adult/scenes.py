"""„Порно - сцени“ (PLAN_media_services.md, stage 4): the scenes lie side by side under <Studio>/ and are never looked
up. Each has <name>.nfo with the title from its file's name, the studio from its folder, and <name>-poster.jpg with
the title. A scene copied in by hand (or still with the NFO Jellyfin wrote) gets them on the next pass, once settled."""

import os
import subprocess
import time
import xml.etree.ElementTree as ET

import match
import metadata
from common import nfo
from common.jellyfin import jellyfin
from common.media import VIDEO_EXTS
from config import SETTLE_AFTER
from state import event, log

OURS = {"title", "studio", "mpaa", "dateadded"}


def videos(locations):
    """(studio, video) of every scene."""
    for location in locations:
        if not os.path.isdir(location):
            continue
        for studio in sorted(os.listdir(location)):
            folder = os.path.join(location, studio)
            if studio.startswith(".") or not os.path.isdir(folder):
                continue
            for name in sorted(os.listdir(folder)):
                if os.path.splitext(name)[1].lower() in VIDEO_EXTS:
                    yield studio, os.path.join(folder, name)


def title_of(video):
    return match.guess(os.path.splitext(os.path.basename(video))[0])[0]


def has_ours(video):
    stem = os.path.splitext(video)[0]
    if not os.path.exists(stem + "-poster.jpg"):
        return False
    root = nfo.read(stem + ".nfo")
    return root is not None and root.find("studio") is not None and {e.tag for e in root} <= OURS


def fill(locations, library_id=None):
    """Writes the NFO and poster of every settled scene that lacks ours, and Jellyfin reads them again; returns the
    scenes written."""
    written = []
    for studio, video in videos(locations):
        try:
            if has_ours(video):
                continue
            stat = os.stat(video)
            if time.time() - max(stat.st_ctime, stat.st_mtime) < SETTLE_AFTER:
                continue
            metadata.write_unknown(video, title_of(video), studio, alone=False)
        except (OSError, ET.ParseError, subprocess.SubprocessError) as e:
            log(f"error {os.path.basename(video)}: {e!r}")
            continue
        written.append(video)
        event(video, "scene", f"NFO и постер: {studio}/{os.path.basename(video)}", os.path.basename(video))
    if written and library_id:
        read_again(library_id, written)
    return written


def read_again(library_id, scenes):
    """Jellyfin reads the scenes' NFOs again: those it has; a new one is read when Jellyfin finds it."""
    items = jellyfin("/Items", ParentId=library_id, Recursive="true", IncludeItemTypes="Video,Movie", Fields="Path")
    ids = {i["Path"]: i["Id"] for i in items["Items"] if i.get("Path")}
    admin = next(u for u in jellyfin("/Users") if u["Policy"]["IsAdministrator"])["Id"]
    for video in scenes:
        if video in ids:
            metadata.read_again(ids[video], admin)
