"""Our NFOs and artwork for the adult libraries (PLAN_media_services.md, stage 4): a movie's from ThePornDB, with its
first performers and their photos; a movie or scene no database knows gets its title, its studio and a poster made
from a frame of the video. No genres or tags, no collections."""

import os

import poster
from common import nfo, tmdb, tpdb
from common.jellyfin import jellyfin, reread
from config import MAX_CAST, PEOPLE_DIR, STUDIO_NAMES

E = nfo.element
# False for a dry run (migrate.py without --apply): the photos are named as they would be, but not downloaded
DOWNLOAD = True


def own(path, like):
    """Owner and mode as the library's other files, which UGOS reaches through ACLs."""
    parent = os.stat(like)
    os.chown(path, parent.st_uid, parent.st_gid)
    os.chmod(path, 0o666)


def studio_name(folder):
    """How a studio's folder is spelled in Jellyfin: as the databases write it ("Adam&Eve" is "Adam & Eve")."""
    return STUDIO_NAMES.get(folder, folder)


def photo(performer_id, url):
    """The local path of a performer's photo, downloaded if missing; None without one. Named apart from the TMDB
    actors' <id>.jpg in the same folder."""
    if not url or not os.path.isdir(PEOPLE_DIR):
        return None
    path = os.path.join(PEOPLE_DIR, f"tpdb-{performer_id}.jpg")
    if not os.path.exists(path) and DOWNLOAD:
        try:
            tpdb.download(url, path)
        except (OSError, tpdb.NotFound):
            return None
    return path


def performers(found):
    """The <actor> of the movie's first MAX_CAST performers, each photo downloaded first: a person Jellyfin makes
    from the NFO takes its photo then or never."""
    actors = []
    for order, (performer_id, name, image) in enumerate(tpdb.people(found)[:MAX_CAST]):
        actor = E("actor", None, E("name", name), E("type", "Actor"), E("order", order), E("tpdbid", performer_id))
        thumb = photo(performer_id, image)
        if thumb:
            actor.append(E("thumb", thumb))
        actors.append(actor)
    return actors


def movie_nfo(found, studio):
    """The NFO of a ThePornDB movie; `studio` is its folder, which decides the studio rather than ThePornDB's site."""
    elements = [E("title", found["title"].strip())]
    date = found.get("date") or ""
    if date:
        elements += [E("year", date[:4]), E("premiered", date)]
    if (found.get("description") or "").strip():
        elements.append(E("plot", found["description"].strip()))
    minutes = round(int(found.get("duration") or 0) / 60)
    if minutes:
        elements.append(E("runtime", minutes))
    elements += [
        E("mpaa", "XXX"),
        E("studio", studio_name(studio)),
        E("uniqueid", f"movies/{found['id']}", type="theporndb", default="true"),
    ]
    return elements + performers(found)


def save_image(url, destination):
    if url:
        tpdb.download(url, destination)
        own(destination, os.path.dirname(destination))


def write_movie(folder, found, studio):
    """The NFO and artwork of a ThePornDB movie in its own folder."""
    save_image(tpdb.cover(found), os.path.join(folder, "folder.jpg"))
    save_image(tpdb.backdrop(found), os.path.join(folder, "backdrop.jpg"))
    path = nfo.path_for("movie", folder)
    if nfo.save("movie", folder, movie_nfo(found, studio)):
        own(path, folder)


def tmdb_details(tmdb_id):
    """A TMDB movie in English, with its cast: the last resort when ThePornDB has nothing."""
    return tmdb.get(f"movie/{tmdb_id}", language="en-US", append_to_response="credits")


def tmdb_photo(person):
    """A TMDB actor's photo as media-ingest keeps them (<id>.jpg), downloaded if missing."""
    if not person.get("profile_path") or not os.path.isdir(PEOPLE_DIR):
        return None
    path = os.path.join(PEOPLE_DIR, f"{person['id']}.jpg")
    if not os.path.exists(path) and DOWNLOAD:
        try:
            tmdb.download_image(person["profile_path"], path, "h632")
        except (OSError, tmdb.NotFound):
            return None
    return path


def tmdb_movie_nfo(d, studio):
    elements = [E("title", d.get("title") or d["original_title"])]
    date = d.get("release_date") or ""
    if date:
        elements += [E("year", date[:4]), E("premiered", date)]
    if (d.get("overview") or "").strip():
        elements.append(E("plot", d["overview"].strip()))
    if d.get("runtime"):
        elements.append(E("runtime", d["runtime"]))
    elements += [
        E("mpaa", "XXX"),
        E("studio", studio_name(studio)),
        E("uniqueid", d["id"], type="tmdb", default="true"),
    ]
    for order, person in enumerate((d.get("credits") or {}).get("cast", [])[:MAX_CAST]):
        actor = E("actor", None, E("name", person["name"]))
        if person.get("character"):
            actor.append(E("role", person["character"]))
        actor.extend([E("type", "Actor"), E("order", order), E("tmdbid", person["id"])])
        thumb = tmdb_photo(person)
        if thumb:
            actor.append(E("thumb", thumb))
        elements.append(actor)
    return elements


def write_tmdb_movie(folder, d, studio):
    """The NFO and artwork of a movie found on TMDB, in its own folder."""
    for name, file_path in (("folder.jpg", d.get("poster_path")), ("backdrop.jpg", d.get("backdrop_path"))):
        if file_path:
            tmdb.download_image(file_path, os.path.join(folder, name))
            own(os.path.join(folder, name), folder)
    path = nfo.path_for("movie", folder)
    if nfo.save("movie", folder, tmdb_movie_nfo(d, studio)):
        own(path, folder)


def write_poster(video, title, studio, image, at=0.25):
    """The poster with the title, over the frame at `at` of the video."""
    temporary = image + ".part"
    with open(temporary, "wb") as f:
        f.write(poster.make(poster.video_frame(video, at), title, studio_name(studio)))
    os.replace(temporary, image)
    own(image, os.path.dirname(image))


def write_unknown(video, title, studio, at=0.25, alone=True):
    """The NFO and poster of a movie or scene no database knows: its title, its studio and a poster from the frame
    at `at` of the video. alone: the video has its folder to itself (movie.nfo, folder.jpg), otherwise its files are
    named after it, as the scenes lie side by side in their studio's folder. A scene's NFO is ours alone: what Jellyfin
    wrote there before goes, but for when it was added."""
    folder = os.path.dirname(video)
    stem = os.path.splitext(video)[0]
    write_poster(video, title, studio, os.path.join(folder, "folder.jpg") if alone else stem + "-poster.jpg", at)
    elements = [E("title", title), E("studio", studio_name(studio)), E("mpaa", "XXX")]
    if alone:
        path = nfo.path_for("movie", folder)
        if nfo.save("movie", folder, elements, replace=("title", "studio"), file=path):
            own(path, folder)
        return
    path = stem + ".nfo"
    old = nfo.read(path)
    root = E("movie", None, *elements, *(old.findall("dateadded") if old is not None else []))
    if nfo.write(path, root, None if old is None else nfo.serialize(old)):
        own(path, folder)


def read_again(item_id, admin):
    """Jellyfin reads the NFO again; the genres and tags it has go first, as it keeps what an NFO doesn't name."""
    item = jellyfin(f"/Items/{item_id}", userId=admin)
    item.update(Genres=[], Tags=[])
    jellyfin(f"/Items/{item_id}", "POST", item)
    reread(item_id, admin)
