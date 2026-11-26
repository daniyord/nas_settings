"""What is in the two libraries: their studios and videos, counted on every pass."""

import os

from common.media import VIDEO_EXTS


def studios(locations):
    """{studio folder name: number of videos under it}, over all of a library's folders."""
    result = {}
    for location in locations:
        if not os.path.isdir(location):
            continue
        for name in sorted(os.listdir(location)):
            folder = os.path.join(location, name)
            if not os.path.isdir(folder) or name.startswith("."):
                continue
            count = sum(os.path.splitext(f)[1].lower() in VIDEO_EXTS for _, _, files in os.walk(folder) for f in files)
            result[name] = result.get(name, 0) + count
    return result
