import os
import sys
import tempfile

# The services read their settings and open their database when imported; a throwaway folder stands in for /data
os.environ.setdefault("JELLYFIN_URL", "http://jellyfin.invalid")
os.environ.setdefault("JELLYFIN_API_KEY", "test")
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="media-test-")
# As in the images: the service's own modules by name from /app, the shared ones as common.*
here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [here, os.path.dirname(here)]

import pytest  # noqa: E402

ANT_MAN = {
    "id": 102899,
    "title": "Ант-Мен",
    "original_title": "Ant-Man",
    "original_language": "en",
    "release_date": "2015-07-14",
    "overview": "",
    "runtime": 117,
    "vote_average": 7.067,
    "genres": [{"name": "Екшън"}],
    "production_companies": [{"name": "Marvel Studios"}],
    "production_countries": [{"name": "United States of America"}],
    "belongs_to_collection": {"id": 422834, "name": "Ант-Мен - колекция"},
    "external_ids": {"imdb_id": "tt0478970"},
    "release_dates": {
        "results": [
            {"iso_3166_1": "US", "release_dates": [{"certification": "PG-13"}]},
            {"iso_3166_1": "BG", "release_dates": [{"certification": "B"}]},
        ]
    },
    "translations": {
        "translations": [
            {"iso_639_1": "bg", "data": {"title": "Ант-Мен", "overview": ""}},
            {"iso_639_1": "en", "data": {"title": "Ant-Man", "overview": "Armed with a super-suit..."}},
        ]
    },
    "credits": {"cast": [{"id": 22226, "name": "Пол Ръд", "character": "Скот Ланг"}]},
    "images": {
        "posters": [
            {"file_path": "/en-poster.jpg", "iso_639_1": "en"},
            {"file_path": "/bg-poster.jpg", "iso_639_1": "bg"},
        ],
        "backdrops": [
            {"file_path": "/en-back.jpg", "iso_639_1": "en"},
            {"file_path": "/clean-back.jpg", "iso_639_1": None},
        ],
        "logos": [{"file_path": "/logo.svg", "iso_639_1": "en"}, {"file_path": "/logo.png", "iso_639_1": "en"}],
    },
}
ANT_MAN_CREDITS = {
    "cast": [
        {"id": 22226, "name": "Paul Rudd", "character": "Scott Lang", "profile_path": "/rudd.jpg"},
        {"id": 3392, "name": "Michael Douglas", "character": "Hank Pym", "profile_path": None},
    ],
    "crew": [
        {"id": 1, "name": "Peyton Reed", "job": "Director", "department": "Directing"},
        {"id": 2, "name": "Edgar Wright", "job": "Screenplay", "department": "Writing"},
        {"id": 2, "name": "Edgar Wright", "job": "Story", "department": "Writing"},
    ],
}
FRIENDS = {
    "id": 1668,
    "name": "Friends",
    "original_name": "Friends",
    "original_language": "en",
    "first_air_date": "1994-09-22",
    "last_air_date": "2004-05-06",
    "status": "Ended",
    "networks": [{"name": "NBC"}],
    "genres": [{"name": "Комедия"}],
    "external_ids": {"imdb_id": "tt0108778", "tvdb_id": 79168},
    "translations": {"translations": []},
    "aggregate_credits": {"cast": []},
    "images": {"posters": [{"file_path": "/friends.jpg", "iso_639_1": None}]},
}


@pytest.fixture
def fake(monkeypatch, tmp_path):
    """TMDB answering from the dicts above; images are files holding their TMDB path. Returns the searches made."""
    import metadata
    import movies
    import series
    from common import tmdb
    from state import db

    searches = []

    def known(titles):
        def get(tmdb_id):
            if tmdb_id not in titles:
                raise tmdb.NotFound(tmdb_id)
            return titles[tmdb_id]

        return get

    places = {
        22226: "Passaic, New Jersey, USA",
        3392: "New Brunswick, New Jersey, USA",
        86940: "Ольгино, СССР",
        1: "Sofia, Bulgaria",
        2: None,
    }
    aliases = {3: ["Стефан Данаилов", "Стефан Ламбов Данаилов"], 4: ["Каффе"]}
    places.update({3: "Sofia, Bulgaria", 4: "Sofia, Bulgaria", 5: None})
    monkeypatch.setattr(
        tmdb,
        "person",
        lambda i: {"place_of_birth": places[i], "also_known_as": aliases.get(i, [])} if i in places else known({})(i),
    )
    db.execute("DELETE FROM actor_names")
    monkeypatch.setattr(tmdb, "movie", known({102899: ANT_MAN}))
    monkeypatch.setattr(tmdb, "tv", known({1668: FRIENDS}))
    monkeypatch.setattr(
        tmdb, "credits", lambda kind, i: ANT_MAN_CREDITS if kind == "movie" else {"cast": [], "crew": []}
    )
    monkeypatch.setattr(tmdb, "season", lambda i, n: {"images": {"posters": [{"file_path": f"/s{n}.jpg"}]}})

    def episode(i, s, e):
        if e > 2:
            raise tmdb.NotFound(e)
        return {
            "season_number": s,
            "episode_number": e,
            "name": ["", "The Pilot", "The Sonogram"][e],
            "external_ids": {},
            "id": 100 + e,
            "translations": {"translations": [{"iso_639_1": "bg", "data": {"name": "Пилотен" if e == 1 else ""}}]},
            "still_path": f"/still{e}.jpg",
        }

    monkeypatch.setattr(tmdb, "episode", episode)

    def search(kind, query, year=None):
        searches.append((kind, query, year))
        if kind == "movie" and "ant" in query.lower():
            return [
                {"id": 999, "title": "Ant Farm", "original_title": "Ant Farm", "release_date": "1999-01-01"},
                {"id": 102899, "title": "Ант-Мен", "original_title": "Ant-Man", "release_date": "2015-07-14"},
            ]
        return []

    monkeypatch.setattr(tmdb, "search", search)
    monkeypatch.setattr(tmdb, "find_imdb", lambda i: {"tt0478970": ("movie", 102899), "tt0108778": ("tv", 1668)}.get(i))

    def download(file_path, destination, size="original"):
        with open(destination, "w") as f:
            f.write(file_path)

    monkeypatch.setattr(tmdb, "download_image", download)
    people = tmp_path / "people"
    people.mkdir()
    monkeypatch.setattr(metadata, "PEOPLE_DIR", str(people))
    updates = []
    monkeypatch.setattr(movies, "jellyfin", lambda *a, **k: updates.append(a))
    monkeypatch.setattr(series, "jellyfin", lambda *a, **k: updates.append(a))
    db.execute("DELETE FROM chosen")
    db.execute("DELETE FROM untitled")
    return searches
