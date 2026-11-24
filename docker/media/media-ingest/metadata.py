"""A title's metadata from TMDB, and the NFO and images written for it before its video moves into the library, so
Jellyfin reads everything from the folder (PLAN_media_services.md, stage 2)."""

import difflib
import json
import os
import re
import shutil

from common import nfo, tmdb
from common.nfo import element as E
from config import MAX_CAST, MIN_SIMILARITY, PEOPLE_DIR
from names import bg_sort, is_xml, locked, release_guesses, similarity
from state import db, lock

# An id in a release or folder name, as Jellyfin's [imdbid-tt...] or just the IMDb id
ID_IN_NAME = re.compile(r"(?<![a-z\d])(?:imdbid-)?(tt\d{6,})(?!\d)|tmdbid-(\d+)", re.I)
# What may be pasted on the status page: a TMDB or IMDb link, an IMDb id or a bare TMDB id
LINK = re.compile(r"themoviedb\.org/(movie|tv)/(\d+)|(tt\d{6,})|^\s*(\d+)\s*$", re.I)
# Artwork Jellyfin saved next to a video or in its folder while it still wrote into the libraries
ART = re.compile(
    r"(?:.*-)?(?:poster|folder|cover|fanart|backdrop|logo|clearlogo|landscape|thumb|banner|clearart|disc|discart)"
    r"\.(?:jpe?g|png|webp|tbn)",
    re.I,
)
IMAGES = (".jpg", ".jpeg", ".png", ".webp", ".tbn")
CYRILLIC = re.compile(r"[А-Яа-яЁёЀ-ЏѐѢѣ]")
# Where names are written in Cyrillic, as TMDB writes a place of birth (in English or in the place's own language)
CYRILLIC_COUNTRY = re.compile(
    r"bulgar|russia|ussr|soviet|ukrain|belarus|byelorussia|serbia|macedonia|montenegro|kazakh|kyrgyz|tajik"
    r"|mongolia|българия|россия|ссср|україна|беларус|србија|сербия|македонија|казахстан",
    re.I,
)
CYRILLIC_LANGUAGES = ("bg", "ru", "uk", "sr", "mk", "be")
# Latin to Bulgarian, longest first
TRANSLITERATION = [
    ("sht", "щ"),
    ("dzh", "дж"),
    ("dj", "дж"),
    ("zh", "ж"),
    ("ts", "ц"),
    ("tz", "ц"),
    ("ch", "ч"),
    ("sh", "ш"),
    ("yu", "ю"),
    ("ya", "я"),
    ("yo", "йо"),
    ("ia", "ия"),
    ("kh", "х"),
    ("iy", "ий"),
    ("a", "а"),
    ("b", "б"),
    ("v", "в"),
    ("w", "в"),
    ("g", "г"),
    ("d", "д"),
    ("e", "е"),
    ("z", "з"),
    ("i", "и"),
    ("y", "й"),
    ("j", "й"),
    ("k", "к"),
    ("l", "л"),
    ("m", "м"),
    ("n", "н"),
    ("o", "о"),
    ("p", "п"),
    ("r", "р"),
    ("s", "с"),
    ("t", "т"),
    ("u", "у"),
    ("f", "ф"),
    ("h", "х"),
    ("c", "ц"),
    ("x", "кс"),
    ("q", "к"),
]


def details(kind, tmdb_id):
    return tmdb.movie(tmdb_id) if kind == "movie" else tmdb.tv(tmdb_id)


def from_link(kind, text):
    """The TMDB id of the "movie" or "tv" that a pasted link or id names, or None."""
    m = LINK.search(text)
    if not m:
        return None
    if m.group(1):
        return int(m.group(2)) if m.group(1).lower() == kind else None
    if m.group(3):
        found = tmdb.find_imdb(m.group(3))
        return found[1] if found and found[0] == kind else None
    return int(m.group(4))


def as_item(item, kind, d):
    """A Jellyfin item with TMDB's title, year and ids in place of Jellyfin's, for the naming code."""
    date = d.get("release_date" if kind == "movie" else "first_air_date") or ""
    imdb = d.get("imdb_id") or d.get("external_ids", {}).get("imdb_id")
    return {
        **item,
        "Name": tmdb.bulgarian_title(d) or d.get("title") or d.get("name"),
        "OriginalTitle": d.get("original_title") or d.get("original_name"),
        "ProductionYear": int(date[:4]) if date[:4].isdigit() else None,
        "ProviderIds": {"Tmdb": str(d["id"]), **({"Imdb": imdb} if imdb else {})},
    }


def identify(kind, path, names):
    """TMDB's details of the release at path (a movie's video, a series' folder), or None if TMDB has nothing like
    it: the title picked on the status page, else an id in one of the names, else the best search result."""
    with lock:
        row = db.execute("SELECT tmdb_id FROM chosen WHERE path = ?", (path,)).fetchone()
    if row:
        return details(kind, row[0])
    for name in names:
        m = ID_IN_NAME.search(name)
        if m and m.group(2):
            return details(kind, int(m.group(2)))
        if m:
            found = tmdb.find_imdb(m.group(1))
            if found and found[0] == kind:
                return details(kind, found[1])
    best = None
    for name in names:
        for title, year in release_guesses(name):
            results = tmdb.search(kind, title, year) or (tmdb.search(kind, title) if year else [])
            for rank, result in enumerate(results[:5]):
                found = as_item({}, kind, result)
                found_year = found["ProductionYear"]
                year_ok = not year or not found_year or abs(found_year - year) <= 1
                close = similarity(title, found)
                score = (close >= MIN_SIMILARITY and year_ok, year_ok, close, -rank)
                if not best or score > best[0]:
                    best = (score, result["id"])
    return details(kind, best[1]) if best else None


def own(path, like):
    """Owner and mode as the library's other files, which UGOS reaches through ACLs."""
    parent = os.stat(like)
    os.chown(path, parent.st_uid, parent.st_gid)
    os.chmod(path, 0o666)


def save_image(file_path, destination, size="original"):
    if file_path:
        tmdb.download_image(file_path, destination, size)
        own(destination, os.path.dirname(destination))


def save_nfo(kind, path, elements, replace=(), actor_replace=(), file=None):
    """True if the NFO changed."""
    changed = nfo.save(kind, path, elements, replace, actor_replace, file)
    if changed:
        own(file or nfo.path_for(kind, path), path if kind != "episode" else os.path.dirname(path))
    return changed


def clear_leftovers(folder, stem=None, whole=False):
    """Removes what Jellyfin saved for a release while it still wrote into the libraries: its NFOs (never one locked
    by hand, nor release notes, which the move deletes) and its artwork, named after the video, or any in the folder
    when it is the title's own."""
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        base, ext = os.path.splitext(name)
        if not os.path.isfile(path):
            continue
        if ext.lower() == ".nfo" and (whole or base == stem) and is_xml(path) and not locked(path):
            os.remove(path)
        elif ext.lower() in IMAGES and ART.fullmatch(name) and (whole or (stem and name.startswith(stem + "-"))):
            os.remove(path)


def photo(person):
    """The local path of a person's photo, downloaded if missing; None without one."""
    if not person.get("profile_path") or not os.path.isdir(PEOPLE_DIR):
        return None
    path = os.path.join(PEOPLE_DIR, f"{person['id']}.jpg")
    if not os.path.exists(path):
        try:
            tmdb.download_image(person["profile_path"], path, "h632")
        except OSError:
            return None
    return path


def _add(elements, tag, value, **attributes):
    if value not in (None, "", 0):
        elements.append(E(tag, value, **attributes))


def _translation(d, field, language="en"):
    for translation in d.get("translations", {}).get("translations", []):
        if translation.get("iso_639_1") == language:
            return (translation.get("data") or {}).get(field) or None
    return None


def _certification(d, kind):
    """Jellyfin's form: BG-<rating> for Bulgaria's, else the US one as it is."""
    found = {}
    if kind == "movie":
        for country in d.get("release_dates", {}).get("results", []):
            ratings = [r["certification"] for r in country.get("release_dates", []) if r.get("certification")]
            if ratings:
                found[country["iso_3166_1"]] = ratings[0]
    else:
        found = {
            r["iso_3166_1"]: r["rating"] for r in d.get("content_ratings", {}).get("results", []) if r.get("rating")
        }
    if found.get("BG"):
        return f"BG-{found['BG']}"
    return found.get("US")


def local_names(kind, d):
    """The people's names in TMDB's Bulgarian credits of a title (Cyrillic where TMDB has them), by TMDB id."""
    if kind == "movie":
        return {c["id"]: c["name"] for c in d.get("credits", {}).get("cast", [])}
    return {c["id"]: c["name"] for c in d.get("aggregate_credits", {}).get("cast", [])}


# TMDB's genre id for animation
ANIMATION = 16


def animated(d):
    """An animated title gets no actors: TMDB has the original voices, while it is watched dubbed."""
    return any(g.get("id") == ANIMATION for g in d.get("genres", []))


def drop_cast(root):
    """Takes the actors (not the directors or writers) out of an NFO; True if there were any."""
    found = [a for a in root.findall("actor") if (a.findtext("type") or "Actor").strip() in ("Actor", "GuestStar")]
    for actor in found:
        root.remove(actor)
    return bool(found)


def names_file():
    return os.path.join(PEOPLE_DIR, "names.json")


def confirmed_names():
    """Names entered on the status page, by TMDB id: {"12345": "Кирил Варийски"}; kept next to the photos."""
    try:
        with open(names_file(), encoding="utf-8") as f:
            return {int(k): v for k, v in json.load(f).items()}
    except (OSError, ValueError):
        return {}


def transliterate(latin):
    """A Bulgarian spelling of a Latin name, as a suggestion to confirm: Bulgarian names in TMDB are mostly written
    the streamlined way, which reads back well except for ъ, ь and doubled letters."""
    text, out, i = latin.lower(), "", 0
    while i < len(text):
        for source, target in TRANSLITERATION:
            if text.startswith(source, i):
                out, i = out + target, i + len(source)
                break
        else:
            out, i = out + text[i], i + 1
    out = re.sub(r"([бвгдзклмнпрстфхц])\1", r"\1", out)
    # "Nikolai", "Krystev": й after a vowel at the end of a syllable, ъ for a y between consonants
    out = re.sub(r"(?<=[аеоуъ])и(?![аеиоуъюя])", "й", out)
    out = re.sub(r"(?<=[бвгджзклмнпрстфхцчшщ])й(?=[бвгджзклмнпрстфхцчшщ])", "ъ", out)
    return " ".join(w[:1].upper() + w[1:] for w in out.split())


def alias(person, latin):
    """The Cyrillic one of TMDB's other names of a person that reads as the Latin name, or None ("Каффе" for Orlin
    Pavlov is a band, not him)."""
    guess = transliterate(latin).lower()
    found = [(difflib.SequenceMatcher(None, guess, a.lower()).ratio(), a) for a in person.get("also_known_as") or []]
    best = max((f for f in found if CYRILLIC.search(f[1])), default=(0, None))
    return best[1] if best[0] >= 0.6 else None


def actor_name(tmdb_id, latin, local, cyrillic_title=False, title=None):
    """One name per person, the same in every title, as Jellyfin tells people apart by name. Cyrillic for people born
    where it is written (Bulgaria, Russia, ...), or born no one knows where and playing in a Bulgarian or Russian
    title: TMDB's Bulgarian name, else the one of its other names that reads as the Latin one, else the name confirmed
    on the status page, which until then asks for it (table actor_names) and shows the Latin one. Latin for everyone
    else."""
    confirmed = confirmed_names().get(tmdb_id)
    if confirmed:
        return confirmed
    if CYRILLIC.search(latin):
        return latin
    try:
        person = tmdb.person(tmdb_id)
    except tmdb.NotFound:
        return latin
    born = person.get("place_of_birth") or ""
    if not (CYRILLIC_COUNTRY.search(born) or (not born and cyrillic_title)):
        return latin
    if local and CYRILLIC.search(local):
        return local
    found = alias(person, latin)
    if found:
        return found
    with lock:
        db.execute(
            "INSERT OR IGNORE INTO actor_names VALUES (?, ?, ?, ?, strftime('%s'))",
            (tmdb_id, latin, transliterate(latin), title or ""),
        )
    return latin


def _actors(kind, d):
    """Actors named as actor_name says, with their Bulgarian roles where TMDB has them, their TMDB id and photo
    (downloaded first: a person Jellyfin makes from the NFO takes its photo then or never)."""
    credits = tmdb.credits(kind, d["id"])
    local = local_names(kind, d)
    cyrillic_title = d.get("original_language") in CYRILLIC_LANGUAGES
    title = d.get("title") or d.get("name")
    if kind == "movie":
        roles = {c["id"]: c.get("character") for c in d.get("credits", {}).get("cast", [])}
        cast = [(c, c.get("character")) for c in credits.get("cast", [])]
    else:
        roles = {
            c["id"]: (c.get("roles") or [{}])[0].get("character")
            for c in d.get("aggregate_credits", {}).get("cast", [])
        }
        cast = [(c, (c.get("roles") or [{}])[0].get("character")) for c in credits.get("cast", [])]
    elements = []
    for order, (person, role) in enumerate([] if animated(d) else cast[:MAX_CAST]):
        name = actor_name(person["id"], person["name"], local.get(person["id"]), cyrillic_title, title)
        actor = E("actor", None, E("name", name))
        if roles.get(person["id"]) or role:
            actor.append(E("role", roles.get(person["id"]) or role))
        actor.extend([E("type", "Actor"), E("order", order), E("tmdbid", person["id"])])
        thumb = photo(person)
        if thumb:
            actor.append(E("thumb", thumb))
        elements.append(actor)
    crew = credits.get("crew", [])
    for person in crew:
        if person.get("job") == "Director":
            elements.append(E("director", person["name"]))
    writers = {p["id"]: p["name"] for p in crew if p.get("department") == "Writing"}
    elements += [E("writer", name) for name in writers.values()]
    return elements


def _ids(d, default="tmdb"):
    ids = d.get("external_ids", {})
    elements = [E("uniqueid", d["id"], type="tmdb", default="true")]
    if ids.get("imdb_id") or d.get("imdb_id"):
        elements.append(E("uniqueid", ids.get("imdb_id") or d.get("imdb_id"), type="imdb"))
    if ids.get("tvdb_id"):
        elements.append(E("uniqueid", ids["tvdb_id"], type="tvdb"))
    return elements


def _titles(d, original):
    title = tmdb.bulgarian_title(d) or d.get("title") or d.get("name") or original
    return [E("title", title), E("originaltitle", original), E("sorttitle", bg_sort(title))]


def movie_nfo(d):
    elements = _titles(d, d["original_title"])
    _add(elements, "plot", d.get("overview") or _translation(d, "overview"))
    _add(elements, "tagline", d.get("tagline"))
    _add(elements, "year", (d.get("release_date") or "")[:4])
    _add(elements, "premiered", d.get("release_date"))
    _add(elements, "runtime", d.get("runtime"))
    _add(elements, "rating", round(d.get("vote_average") or 0, 1))
    _add(elements, "mpaa", _certification(d, "movie"))
    elements += [E("genre", g["name"]) for g in d.get("genres", [])]
    elements += [E("studio", c["name"]) for c in d.get("production_companies", [])]
    elements += [E("country", c["name"]) for c in d.get("production_countries", [])]
    collection = d.get("belongs_to_collection")
    if collection:
        elements.append(E("set", None, E("name", collection["name"]), tmdbcolid=collection["id"]))
    return elements + _ids(d) + _actors("movie", d)


def series_nfo(d):
    elements = _titles(d, d["original_name"])
    _add(elements, "plot", d.get("overview") or _translation(d, "overview"))
    _add(elements, "year", (d.get("first_air_date") or "")[:4])
    _add(elements, "premiered", d.get("first_air_date"))
    ended = d.get("status") in ("Ended", "Canceled")
    _add(elements, "status", "Ended" if ended else "Continuing")
    if ended:
        _add(elements, "enddate", d.get("last_air_date"))
    _add(elements, "runtime", (d.get("episode_run_time") or [None])[0])
    _add(elements, "rating", round(d.get("vote_average") or 0, 1))
    _add(elements, "mpaa", _certification(d, "tv"))
    elements += [E("genre", g["name"]) for g in d.get("genres", [])]
    elements += [E("studio", n["name"]) for n in d.get("networks", [])]
    return elements + _ids(d) + _actors("tv", d)


def episode_title(e):
    return tmdb.bulgarian_title(e) or _translation(e, "name") or e.get("name")


def episode_nfo(e, show_title, last=None):
    elements = []
    _add(elements, "title", episode_title(e))
    _add(elements, "showtitle", show_title)
    elements += [E("season", e["season_number"]), E("episode", e["episode_number"])]
    _add(elements, "episodenumberend", last)
    _add(elements, "plot", e.get("overview") or _translation(e, "overview"))
    _add(elements, "aired", e.get("air_date"))
    _add(elements, "runtime", e.get("runtime"))
    _add(elements, "rating", round(e.get("vote_average") or 0, 1))
    for person in e.get("crew", []):
        if person.get("job") == "Director":
            elements.append(E("director", person["name"]))
        elif person.get("department") == "Writing":
            elements.append(E("writer", person["name"]))
    return elements + _ids(e)


# Other names Jellyfin reads for the same image in a title's folder; one of them there is the image
SAME_IMAGE = {
    "folder.jpg": ("folder", "poster", "cover"),
    "backdrop.jpg": ("backdrop", "fanart", "background", "backdrop1", "art"),
    "logo.png": ("logo", "clearlogo"),
    "landscape.jpg": ("landscape", "thumb"),
}


def has_image(folder, name):
    bases = SAME_IMAGE[name]
    for existing in os.listdir(folder):
        base, ext = os.path.splitext(existing.lower())
        if ext in IMAGES and (base in bases or base.rsplit("-", 1)[-1] in bases):
            return True
    return False


def art(d, folder, missing_only=False):
    """The title's poster, background, logo and a background with its name, as Jellyfin names them in a folder;
    with missing_only, only those the folder has none of (under any name Jellyfin reads)."""
    images = d.get("images", {})
    logos = [i for i in images.get("logos", []) if i["file_path"].endswith(".png")]
    titled = [i for i in images.get("backdrops", []) if i.get("iso_639_1") in ("bg", "en")]
    picks = {
        "folder.jpg": tmdb.pick_image(images.get("posters", [])),
        "backdrop.jpg": tmdb.pick_image(images.get("backdrops", []), (None, "bg", "en")),
        "logo.png": tmdb.pick_image(logos),
        "landscape.jpg": tmdb.pick_image(titled),
    }
    added = []
    for name, file_path in picks.items():
        if file_path and not (missing_only and has_image(folder, name)):
            save_image(file_path, os.path.join(folder, name))
            added.append(name)
    return added


def untitled(folder, kind, d):
    """Remembers a title TMDB has no Bulgarian title for, so the status page asks for one."""
    if tmdb.bulgarian_title(d):
        return
    with lock:
        db.execute("INSERT OR REPLACE INTO untitled VALUES (?, ?, strftime('%s'))", (folder, kind))


def write_movie(folder, d, created=False):
    """The NFO and artwork of a movie in the folder that becomes its own. created: the folder was made for it, so it
    goes away again if writing fails."""
    try:
        art(d, folder)
        save_nfo("movie", folder, movie_nfo(d))
    except (OSError, tmdb.NotFound):
        if created:
            shutil.rmtree(folder, ignore_errors=True)
        raise


def write_series(folder, d, seasons):
    """tvshow.nfo, the series' artwork and the seasons' posters, in the folder that becomes the series'."""
    clear_leftovers(folder, whole=True)
    art(d, folder)
    write_season_posters(folder, d, seasons)
    save_nfo("tvshow", folder, series_nfo(d))


def write_season_posters(folder, d, seasons, missing_only=False):
    added = []
    for n in sorted(seasons):
        name = "season-specials-poster.jpg" if n == 0 else f"season{n:02d}-poster.jpg"
        if missing_only and os.path.exists(os.path.join(folder, name)):
            continue
        try:
            season = tmdb.season(d["id"], n)
        except tmdb.NotFound:
            continue
        poster = tmdb.pick_image(season.get("images", {}).get("posters", []))
        if poster:
            save_image(poster, os.path.join(folder, name))
            added.append(name)
    return added


def write_episode(video, d, season, first, last=None):
    """The NFO and still of an episode, next to its video under its current name, so they move with it. Returns the
    episode's title, or None when TMDB doesn't have that episode."""
    try:
        e = tmdb.episode(d["id"], season, first)
    except tmdb.NotFound:
        return None
    folder, filename = os.path.split(video)
    stem = os.path.splitext(filename)[0]
    clear_leftovers(folder, stem)
    still = tmdb.pick_image(e.get("images", {}).get("stills", [])) or e.get("still_path")
    save_image(still, os.path.join(folder, f"{stem}-thumb.jpg"))
    show_title = tmdb.bulgarian_title(d) or d.get("name")
    save_nfo("episode", video, episode_nfo(e, show_title, last))
    return episode_title(e)
