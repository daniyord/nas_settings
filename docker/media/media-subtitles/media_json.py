"""A folder's .media.json: the user's answers about the videos in it, kept with them so they outlive the database
(PLAN_media_services.md, 2.3). Keys: "audio" (the answer about sound with no language), "tagged" (the sound tracks
then set to Bulgarian, [path, track], to undo it by), "dismissed" (subtitles left as they are, {name: [size, mtime]})."""

import json
import os

NAME = ".media.json"


def read(folder):
    try:
        with open(os.path.join(folder, NAME), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def update(folder, **values):
    """Sets keys (an empty value removes one), keeping the rest; the file goes once nothing is left in it."""
    path = os.path.join(folder, NAME)
    data = read(folder)
    for key, value in values.items():
        if value:
            data[key] = value
        else:
            data.pop(key, None)
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


def remember_dismissed(subtitle):
    """A subtitle left as it is, by its size and mtime (a rename keeps them; a change makes it checked again). Entries
    no file in the folder matches any more are dropped."""
    folder = os.path.dirname(subtitle)
    present = set()
    for name in os.listdir(folder):
        try:
            st = os.stat(os.path.join(folder, name))
        except OSError:
            continue
        present.add((st.st_size, st.st_mtime))
    kept = {n: v for n, v in read(folder).get("dismissed", {}).items() if tuple(v) in present}
    st = os.stat(subtitle)
    kept[os.path.basename(subtitle)] = [st.st_size, st.st_mtime]
    update(folder, dismissed=kept)


def was_dismissed(subtitle, st):
    return [st.st_size, st.st_mtime] in read(os.path.dirname(subtitle)).get("dismissed", {}).values()
