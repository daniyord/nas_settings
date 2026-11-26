"""Which ThePornDB movie a download is (PLAN_media_services.md, 4.1), with the rules of jellyfin-tpdb-match. Its
studio isn't known, so ThePornDB is searched whole and a match is taken on its own only when there is no doubt:
one title of similarity MIN_SIMILARITY or more, from one studio, with the file's runtime within
MAX_RUNTIME_OFF_MINUTES. Everything else waits on the status page with the candidates found."""

import difflib
import re

from common import tmdb, tpdb

MIN_SIMILARITY = 0.9
MAX_RUNTIME_OFF_MINUTES = 15
# Releases this close in runtime are one movie listed again (ThePornDB has many re-releases)
SAME_MOVIE_MINUTES = 5
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# Like Jellyfin's [imdbid-tt...]: an id in the name pins the match, no search involved
ID_TAG = re.compile(r"\[(tpdbid-" + UUID + r"|tmdbid-\d+)\]", re.I)
TPDB_LINK = re.compile(r"theporndb\.net/movies/([\w-]+)|^\s*(" + UUID + r")\s*$", re.I)
TMDB_LINK = re.compile(r"themoviedb\.org/movie/(\d+)", re.I)
# What a release name has after the title
RELEASE_TOKENS = re.compile(
    r"\b(xxx|\d{3,4}p|[48]k|uhd|dvdrip|dvd|web-?dl|webrip|web|hdtv|bluray|bdrip|x26[45]|h\.?26[45]|hevc|avc|aac|"
    r"mp4|split scenes|repack|proper)\b",
    re.I,
)
COMPOUND = re.compile(r"\b(blow|hand|foot|rim|boob|tit)\s+(jobs?)\b", re.I)


def norm(text):
    return re.sub(r"[^a-z0-9]", "", text.lower().replace("&", "and"))


def same_studio(a, b):
    """By the start of the name, so "Wicked" covers "Wicked Pictures" but not "Raw Wicked Love Affair"."""
    a, b = norm(a), norm(b)
    return bool(a and b) and (b.startswith(a) or a.startswith(b))


def guess(name):
    """(title, year or None) of a release or file name: the title is what comes before the year or the first release
    token ("Carmen.And.Austyn.2006.XXX.DVDRip" -> ("Carmen And Austyn", 2006))."""
    clean = re.sub(r"\s+", " ", re.sub(r"[._]+", " ", re.sub(r"\[[^\]]*\]", " ", name))).strip()
    year = re.search(r"(?<!\w)\(?((?:19|20)\d{2})\)?(?!\w)", clean)
    token = RELEASE_TOKENS.search(clean)
    cut = min(m.start() for m in (year, token) if m) if (year or token) else len(clean)
    title = clean[:cut].strip(" -([")
    return (title or clean), (int(year.group(1)) if year else None)


def pinned(text):
    """("tpdb", ref) or ("tmdb", id) of an id tag in a name or a link typed on the status page, or None."""
    tag = ID_TAG.search(text)
    if tag:
        kind, _, value = tag.group(1).partition("-")
        return ("tpdb", value.lower()) if kind.lower() == "tpdbid" else ("tmdb", value)
    link = TPDB_LINK.search(text)
    if link:
        return "tpdb", link.group(1) or link.group(2)
    link = TMDB_LINK.search(text)
    return ("tmdb", link.group(1)) if link else None


def studio_free(title, studio):
    """The title as a file name would have it: without its studio in front ("The Penthouse Pet's Guide to ...") or
    in parentheses after it ("Friends With Benefits (Penthouse)"), and with a trailing article in front
    ("Housekeeper, the")."""
    article = re.fullmatch(r"(.+), (the|a|an)", title, re.I)
    if article:
        title = f"{article.group(2)} {article.group(1)}"
    names = {title}
    if not studio:
        return names
    suffix = re.fullmatch(r"(.+?)\s*\(([^()]+)\)", title)
    if suffix and same_studio(studio, suffix.group(2)):
        names.add(suffix.group(1))
    words = re.sub(r"^the\s+", "", title, flags=re.I).split()
    for k in range(1, min(3, len(words) - 1) + 1):
        if norm(re.sub(r"'s$", "", " ".join(words[:k]), flags=re.I)) == norm(studio):
            names.add(" ".join(words[k:]))
    return names


def texts(title, keywords):
    """The file's title and each keyword alone (a full other title) and added to the title ("Cinderella" + "XXX
    Axel Braun Parody")."""
    return [title, *keywords] + ([f"{title} {' '.join(keywords)}"] if keywords else [])


def queries(title, keywords):
    found = {title.replace(" - ", ": "), title.split(" - ")[0], *texts(title, keywords)}
    # ThePornDB's search doesn't treat "&" and "and" alike, nor a compound written apart ("Blow Jobs")
    found |= {re.sub(r"\s+and\s+", " & ", q, flags=re.I) for q in found} | {q.replace(" & ", " and ") for q in found}
    return {COMPOUND.sub(r"\1\2", q) for q in found} | found


def similarity(wanted, title, studio):
    return round(
        max(
            difflib.SequenceMatcher(None, norm(t), norm(n)).ratio() for t in wanted for n in studio_free(title, studio)
        ),
        2,
    )


def candidate(found, score, minutes):
    return {
        "source": "tpdb",
        "id": found["id"],
        "title": found["title"],
        "site": (found.get("site") or {}).get("name") or "?",
        "year": int(found["date"][:4]) if found.get("date") else None,
        "minutes": minutes or None,
        "similarity": score,
        "cover": tpdb.cover(found),
        "link": f"https://theporndb.net/movies/{found['id']}",
    }


def search_tpdb(title, year, minutes, keywords):
    """(movie or None, why not, candidates): ThePornDB across all of its sites."""
    wanted = texts(title, keywords)
    found = {}
    for query in queries(title, keywords):
        for movie in tpdb.search(query):
            found[movie["id"]] = movie
    scored = []
    for movie in found.values():
        site = (movie.get("site") or {}).get("name") or ""
        movie_minutes = round(int(movie.get("duration") or 0) / 60)
        off = abs(movie_minutes - minutes) if movie_minutes and minutes else None
        movie_year = int(movie["date"][:4]) if movie.get("date") else None
        scored.append((similarity(wanted, movie["title"], site), movie_year, off, movie, movie_minutes))
    scored.sort(key=lambda s: (-s[0], s[1] != year, s[2] if s[2] is not None else 999))
    candidates = [candidate(m, score, mm) for score, _, _, m, mm in scored[:5]]
    best_score = scored[0][0] if scored else 0
    if best_score < MIN_SIMILARITY:
        return None, "няма заглавие с такова име в ThePornDB", candidates
    best = [s for s in scored if s[0] == best_score]
    if year and any(s[1] == year for s in best):
        best = [s for s in best if s[1] == year]
    best.sort(key=lambda s: s[2] if s[2] is not None else 999)
    off = best[0][2]
    if off is None:
        return None, "няма продължителност, с която да се провери", candidates
    if off > MAX_RUNTIME_OFF_MINUTES:
        return None, f"продължителността се различава с {off} мин", candidates
    same = [s for s in best if s[2] is not None and s[2] - off < SAME_MOVIE_MINUTES]
    sites = {(s[3].get("site") or {}).get("name") or "" for s in same}
    if any(not same_studio(a, b) for a in sites for b in sites):
        return None, f"същото заглавие от {len(sites)} студиа", candidates
    # One movie listed again: the original release, among those with a cover
    pick = min(same, key=lambda s: (not tpdb.cover(s[3]), s[3].get("date") or "9999"))[3]
    return pick, None, candidates


def search_tmdb(title, year):
    """Candidates from TMDB, which has a few adult movies: never taken on their own, as without the studio a parody
    would match the mainstream movie of the same title."""
    found = tmdb.get(
        "search/movie", language="en-US", query=title, include_adult="true", **({"year": year} if year else {})
    )
    candidates = []
    for movie in found.get("results", [])[:5]:
        score = similarity([title], movie.get("title") or "", "")
        if score >= MIN_SIMILARITY:
            candidates.append(
                {
                    "source": "tmdb",
                    "id": movie["id"],
                    "title": movie["title"],
                    "site": "TMDB",
                    "year": int(movie["release_date"][:4]) if movie.get("release_date") else None,
                    "minutes": None,
                    "similarity": score,
                    "cover": tmdb.image_url(movie["poster_path"], "w185") if movie.get("poster_path") else None,
                    "link": f"https://www.themoviedb.org/movie/{movie['id']}",
                }
            )
    return candidates


def studio_folder(site, folders, merge=()):
    """The existing studio folder a ThePornDB site belongs to ("Wicked Pictures" -> "Wicked"), or None."""
    site = next((m for m in merge if same_studio(m, site)), site)
    return next((f for f in sorted(folders, key=len, reverse=True) if same_studio(f, site)), None)
