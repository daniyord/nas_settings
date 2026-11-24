"""How movies, series and the files next to them are named (docs/PLAN_jellyfin_library.md); no disk changes here."""

import difflib
import os
import re

from common.media import EPISODE, LANGUAGES
from config import ALPHABET, MIN_SIMILARITY, SEASON_TOKEN


def bg_sort(name):
    words = []
    for word in name.lower().split():
        code = ""
        for ch in word:
            index = ALPHABET.find(ch)
            if index >= 0:
                code += "ab"[index // 26] + chr(ord("a") + index % 26)
            # After the Cyrillic codes (a?/b?) rather than mixed in with them
            elif "a" <= ch <= "z":
                code += "c" + ch
            # Digits Jellyfin pads itself, other letters (ы, é, kana) it transliterates itself
            elif ch.isdigit() or (ch.isalpha() and not ch.isascii()):
                code += ch
        if code:
            words.append(code)
    return " ".join(words)


def padded(sort_name):
    return re.sub(r"\d+", lambda m: m.group().zfill(10), sort_name)


def norm(text):
    return re.sub(r"[\W_]", "", text.lower().replace("&", "and"))


def release_guesses(name):
    """(title, year or None) a release name may mean: the title is whatever comes before a year, and with several
    ("Blade Runner 2049 2017") each is tried."""
    clean = re.sub(r"[._]+", " ", re.sub(r"\[[^\]]*\]", " ", name))
    cuts = [(m.start(), int(m.group(1))) for m in re.finditer(r"(?<!\w)((?:19|20)\d{2})(?!\w)", clean)]
    guesses = []
    for at, found_year in cuts or [(len(clean), None)]:
        title = clean[:at].strip(" -([")
        if title:
            guesses.append((title, found_year))
    return guesses


def similarity(title, item):
    return max(
        difflib.SequenceMatcher(None, norm(title), norm(t)).ratio()
        for t in (item.get("OriginalTitle"), item.get("Name"))
        if t
    )


def doubt(item, names):
    """Why the match doesn't look like the release name(s), or "" when it does."""
    year = item.get("ProductionYear")
    best = None
    for name in names:
        for title, found_year in release_guesses(name):
            year_ok = not found_year or not year or abs(found_year - year) <= 1
            score = (similarity(title, item) >= MIN_SIMILARITY and year_ok, similarity(title, item))
            if not best or score > best[0]:
                best = (score, title, found_year)
    if not best or best[0][0]:
        return ""
    _, title, found_year = best
    found = f'„{title}"{f" ({found_year})" if found_year else ""}'
    return f'{found} не прилича на „{item.get("OriginalTitle") or item.get("Name")}" ({year})'


def id_tag(item):
    ids = item.get("ProviderIds") or {}
    return f"imdbid-{ids['Imdb']}" if ids.get("Imdb") else f"tmdbid-{ids['Tmdb']}" if ids.get("Tmdb") else None


def clean_title(title):
    title = re.sub(r"\s*:\s*", " - ", title)
    # "9 1/2 Weeks" and "AC/DC" rather than "9 12 Weeks" and "ACDC"
    title = re.sub(r"\s*\b1/2\b", "½", title).replace("/", "-")
    title = re.sub(r'[\\?*"<>|]', "", title)
    return re.sub(r"\s+", " ", title).strip(" .")


def target_name(item):
    title = item.get("OriginalTitle") or item["Name"]
    # Folders are in Latin or Cyrillic; a Japanese or Korean original gives way to the localized name
    if re.search(r"[^\u0000-ӿ -⁯]", title):
        title = item["Name"]
    return f"{clean_title(title)} ({item['ProductionYear']}) [{id_tag(item)}]"


def release_title(name):
    """The series' name in a release name ("The.Walking.Dead.S01.HDTV" -> "The.Walking.Dead.")."""
    return SEASON_TOKEN.split(name)[0] or name


def series_title(name):
    """The series' name in a release name, without the year some releases carry ("Severance.2022.S01E01" -> "severance")."""
    return norm(re.sub(r"[ ._-]*\(?(?:19|20)\d{2}\)?[ ._-]*$", "", release_title(name)))


def episode_numbers(episode):
    """(season, first, last) by Jellyfin; last is None for a single episode."""
    end = episode.get("IndexNumberEnd")
    return episode["ParentIndexNumber"], episode["IndexNumber"], end if end != episode["IndexNumber"] else None


def episode_name(episode, stem):
    season, first, last = episode_numbers(episode)
    numbers = f"S{season:02d}E{first:02d}" + (f"-E{last:02d}" if last else "")
    title = clean_title(episode.get("Name") or "")
    # Until Jellyfin has the episode's title, its name is the file's
    placeholder = re.fullmatch(r"(?i)(?:episode|епизод)\s*\d+|tba", title)
    if not title or title == clean_title(stem) or EPISODE.search(title) or placeholder:
        return numbers
    return f"{numbers} - {title}"


def season_dir(series_dir, season):
    """The series' own folder for the season when it has one ("Season 1", "S01"), otherwise "Season 01"."""
    if os.path.isdir(series_dir):
        for name in sorted(os.listdir(series_dir)):
            m = re.fullmatch(r"(?:season|сезон|s)[ ._-]?0*(\d+)", name, re.I)
            if m and int(m.group(1)) == season and os.path.isdir(os.path.join(series_dir, name)):
                return name
    return f"Season {season:02d}"


def is_xml(path):
    try:
        with open(path, "rb") as f:
            return f.read(512).lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<")
    except OSError:
        return False


def locked(path):
    """Whether an NFO file says its metadata is locked (Jellyfin's Lock this item)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return "<lockdata>true</lockdata>" in f.read()
    except OSError:
        return False


def foreign_rest(name):
    """The language and extension of a subtitle named after something else ("Some.Release.eng.srt" -> ".en.srt")."""
    ext = os.path.splitext(name)[1].lower()
    m = re.search(r"(?:^|[._ -])(" + "|".join(LANGUAGES) + r")([._ -]forced)?\.\w+$", name, re.I)
    return f".{LANGUAGES[m.group(1).lower()]}{'.forced' if m.group(2) else ''}{ext}" if m else ext


def generic_name(rest):
    # What Jellyfin calls a movie's NFO and artwork when the video is alone in its folder
    if rest.lower() == ".nfo":
        return "movie.nfo"
    art = re.fullmatch(r"-(\w+)(\.\w+)", rest)
    if art:
        return ("folder" if art.group(1).lower() == "poster" else art.group(1).lower()) + art.group(2).lower()
    return None


def owner(name, videos):
    """The video (name without extension) a file next to it belongs to, or None."""
    # The longest matching video name owns the file, so "Movie 2.srt" stays with "Movie 2.mkv" rather than "Movie.mkv"
    owners = [v for v in videos if name.lower().startswith(v.lower()) and name[len(v) : len(v) + 1] in (".", "-")]
    return max(owners, key=len) if owners else None
