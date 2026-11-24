from datetime import datetime, timedelta, timezone

import boxsets
from common import nfo, tmdb

NEW = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
OLD = "2021-05-30T21:48:13.0000000Z"


def movie(films, item_id, name, year, sets, added=NEW, genres=()):
    folder = films / item_id
    # Listed again on every call: the NFO is written the first time only
    if not folder.exists():
        folder.mkdir()
        (folder / "movie.nfo").write_text(f"<movie><title>{name}</title>{sets}</movie>", encoding="utf-8")
    return {
        "Id": item_id,
        "Name": name,
        "ProductionYear": year,
        "Path": str(folder / "video.mkv"),
        "DateCreated": added,
        "Genres": list(genres),
    }


def setup(tmp_path, monkeypatch, movies, boxes=(), tmdb_ids=None):
    films, collections = tmp_path / "Films", tmp_path / "collections"
    films.mkdir()
    collections.mkdir()
    calls, made = [], {}

    def jellyfin(path, method="GET", body=None, **params):
        calls.append((path, method, params, body))
        if path == "/Users":
            return [{"Id": "a", "Policy": {"IsAdministrator": True}}]
        if path == "/Library/VirtualFolders":
            return [{"Name": "Филми", "Locations": [str(films)]}]
        if path == "/Items" and params.get("IncludeItemTypes") == "Movie":
            return {"Items": movies(films)}
        if path == "/Items" and params.get("IncludeItemTypes") == "BoxSet":
            ids = tmdb_ids or {}
            return {
                "Items": [{"Id": n, "Name": n, "ProviderIds": {"Tmdb": str(ids[n])} if n in ids else {}} for n in boxes]
            }
        if path == "/Items":
            return {"Items": [{"Id": i} for i in boxes[params["ParentId"]]]}
        if path == "/Collections":
            (collections / f"{params['Name']} [boxset]").mkdir()
            made[params["Name"]] = params["Ids"].split(",")
            return {"Id": params["Name"]}
        if path.startswith("/Users/a/Items/"):
            name = path.rsplit("/", 1)[1]
            return {"Id": name, "Name": name, "Path": f"{boxsets.JELLYFIN_DIR}/{name} [boxset]"}
        return None

    def collection(tmdb_id):
        if tmdb_id != 9:
            raise tmdb.NotFound(tmdb_id)
        return {"overview": "Мравки.", "images": {"posters": [{"file_path": "/p.jpg"}]}, "backdrop_path": "/b.jpg"}

    def download(file_path, destination, size="original"):
        with open(destination, "w") as f:
            f.write(file_path)

    monkeypatch.setattr(boxsets, "COLLECTIONS_DIR", str(collections))
    monkeypatch.setattr(boxsets, "jellyfin", jellyfin)
    monkeypatch.setattr(boxsets, "LIBRARIES", ["Филми"])
    monkeypatch.setattr(boxsets, "event", lambda *a: None)
    monkeypatch.setattr(boxsets, "own", lambda *a: None)
    monkeypatch.setattr(tmdb, "collection", collection)
    monkeypatch.setattr(tmdb, "download_image", download)
    return collections, calls, made


def test_a_collection_is_made_for_two_new_movies(tmp_path, monkeypatch):
    def movies(films):
        ant = '<set tmdbcolid="9"><name>Ant-Man Collection</name></set>'
        return [
            movie(films, "m2", "Ант-Мен и Осата", 2018, ant),
            movie(films, "m1", "Ант-Мен", 2015, ant, added=OLD),
            movie(films, "h1", "Хан Аспарух - I серия - Фанагория", 1981, "<set><name>Хан</name></set>"),
            movie(films, "h2", "Хан Аспарух - II серия", 1981, "<set><name>Хан</name></set>"),
            movie(films, "alone", "Сам", 2000, '<set tmdbcolid="77"><name>Една</name></set>'),
        ]

    collections, calls, made = setup(tmp_path, monkeypatch, movies)
    assert boxsets.sync() == ["Ант-Мен (поредица)", "Хан Аспарух (поредица)"]
    assert made == {"Ант-Мен (поредица)": ["m2", "m1"], "Хан Аспарух (поредица)": ["h1", "h2"]}
    saved = {c[0]: c[3] for c in calls if c[1] == "POST" and c[0].startswith("/Items/") and c[3]}
    ant = saved["/Items/Ант-Мен (поредица)"]
    assert ant["LockedFields"] == ["Name"] and ant["Overview"] == "Мравки." and ant["ForcedSortName"]
    folder = collections / "Ант-Мен (поредица) [boxset]"
    assert (folder / "folder.jpg").read_text() == "/p.jpg" and (folder / "backdrop.jpg").read_text() == "/b.jpg"
    assert ("/Items/Ант-Мен (поредица)/Refresh", "POST") in [c[:2] for c in calls]
    assert "Overview" not in saved["/Items/Хан Аспарух (поредица)"]


def test_old_ones_are_left_alone_and_new_ones_join_their_collection(tmp_path, monkeypatch):
    def movies(films):
        return [
            # Deleted in Jellyfin long ago: not made again
            movie(films, "o1", "Брат", 1997, "<set><name>Брат</name></set>", added=OLD),
            movie(films, "o2", "Брат 2", 2000, "<set><name>Брат</name></set>", added=OLD),
            # The series has its collection: the new one goes into it, the old one taken out of it stays out
            movie(films, "e1", "Елхи", 2010, "<set><name>Елхи</name></set>", added=OLD),
            movie(films, "e2", "Елхи 2", 2011, "<set><name>Елхи</name></set>", added=OLD),
            movie(films, "e5", "Елхи 5", 2016, "<set><name>Елхи</name></set>"),
            # In two collections: which one can't be told
            movie(films, "x1", "Икс", 2010, "<set><name>X</name></set>", added=OLD),
            movie(films, "x2", "Икс 2", 2011, "<set><name>X</name></set>", added=OLD),
            movie(films, "x3", "Икс 3", 2012, "<set><name>X</name></set>"),
        ]

    errors = []
    monkeypatch.setattr(boxsets, "log", errors.append)
    _, calls, made = setup(tmp_path, monkeypatch, movies, {"Елхи (поредица)": ["e1"], "A": ["x1"], "B": ["x2"]})
    assert boxsets.sync() == ["Елхи (поредица)"] and made == {}
    assert [(c[0], c[2]) for c in calls if c[0].startswith("/Collections/")] == [
        ("/Collections/Елхи (поредица)/Items", {"ids": "e5"})
    ]
    assert len(errors) == 1


def test_a_taken_name_goes_to_the_cartoons_or_is_logged(tmp_path, monkeypatch):
    def movies(films):
        cartoon = ("Анимация",)
        return [
            movie(films, "b1", "Батман", 1993, '<set tmdbcolid="5"><name>B</name></set>', genres=cartoon),
            movie(films, "b2", "Батман 2", 1998, '<set tmdbcolid="5"><name>B</name></set>', genres=cartoon),
            movie(films, "s1", "Шрек", 2001, '<set tmdbcolid="6"><name>S</name></set>'),
            movie(films, "s2", "Шрек 2", 2004, '<set tmdbcolid="6"><name>S</name></set>'),
        ]

    errors = []
    monkeypatch.setattr(boxsets, "log", errors.append)
    _, _, made = setup(tmp_path, monkeypatch, movies, {"Батман (поредица)": [], "Шрек (поредица)": []})
    assert boxsets.sync() == ["Батман (анимационна поредица)"]
    assert list(made) == ["Батман (анимационна поредица)"] and len(errors) == 1


def test_a_movie_may_be_in_several_collections(tmp_path, monkeypatch):
    def movies(films):
        two = "<set><name>Елхи</name></set><set><name>Нова година</name></set>"
        return [
            movie(films, "e1", "Елхи", 2010, "<set><name>Елхи</name></set>", added=OLD),
            movie(films, "e2", "Елхи 2", 2011, "<set><name>Елхи</name></set>", added=OLD),
            movie(films, "e5", "Елхи 5", 2016, two),
            movie(films, "n1", "Ирония на съдбата", 1976, "<set><name>Нова година</name></set>", added=OLD),
        ]

    # Елхи 2 is in a collection made by hand as well
    boxes = {"Елхи (поредица)": ["e1", "e2"], "Любими": ["e2", "n1"]}
    _, calls, made = setup(tmp_path, monkeypatch, movies, boxes)
    assert boxsets.sync() == ["Елхи (поредица)", "Ирония на съдбата (поредица)"]
    assert [(c[0], c[2]) for c in calls if c[0].startswith("/Collections/")] == [
        ("/Collections/Елхи (поредица)/Items", {"ids": "e5"})
    ]
    assert made == {"Ирония на съдбата (поредица)": ["e5", "n1"]}


def sets_in(films, item_id):
    root = nfo.read(str(films / item_id / "movie.nfo"))
    assert root is not None
    return [(e.get("tmdbcolid"), e.findtext("name")) for e in root.findall("set")]


def test_jellyfins_collections_go_into_the_nfos(tmp_path, monkeypatch):
    def movies(films):
        return [
            movie(films, "e1", "Елхи", 2010, "", added=OLD),
            movie(films, "e2", "Елхи 2", 2011, '<set tmdbcolid="5"><name>Yolki Collection</name></set>', added=OLD),
            # Taken out of the collection; the one added lately is sync's yet
            movie(films, "e3", "Елхи 3", 2013, '<set tmdbcolid="5"><name>Елхи (поредица)</name></set>', added=OLD),
            movie(films, "e4", "Елхи 4", 2014, '<set tmdbcolid="5"><name>Елхи (поредица)</name></set>'),
            # TMDB's, for a movie alone: no collection, kept
            movie(films, "s1", "Сам", 2000, '<set tmdbcolid="77"><name>Една</name></set>', added=OLD),
            movie(films, "l1", "Любим", 2001, "", added=OLD),
        ]

    boxes = {
        "Елхи (поредица)": ["e1", "e2"],
        "Любими": ["l1", "e1"],
        "A": ["s1"],
        "B": ["s1"],
        "C": ["s1"],
        "D": ["s1"],
    }
    collections, _, _ = setup(tmp_path, monkeypatch, movies, boxes, {"Елхи (поредица)": 5})
    films = tmp_path / "Films"
    errors = []
    monkeypatch.setattr(boxsets, "log", errors.append)

    assert boxsets.mirror() == 5
    assert sets_in(films, "e1") == [("5", "Елхи (поредица)"), (None, "Любими")]
    assert sets_in(films, "e2") == [("5", "Елхи (поредица)")]
    assert sets_in(films, "e3") == []
    assert sets_in(films, "e4") == [("5", "Елхи (поредица)")]
    others = [(None, n) for n in "ABCD"]
    assert sets_in(films, "s1") == [("77", "Една"), *others]
    assert sets_in(films, "l1") == [(None, "Любими")]
    assert boxsets.mirror() == 0

    # Deleted in Jellyfin: gone from the NFOs too
    del boxes["Любими"]
    assert boxsets.mirror() == 2
    assert sets_in(films, "e1") == [("5", "Елхи (поредица)")] and sets_in(films, "l1") == []

    # Most of them gone at once (an empty database): the NFOs are left as they are
    for name in ("A", "B", "C", "D"):
        del boxes[name]
    assert boxsets.mirror() is None and len(errors) == 1
    assert sets_in(films, "s1") == [("77", "Една"), *others]
    assert (collections / boxsets.SEEN).exists()


def test_the_collections_are_restored_from_the_nfos(tmp_path, monkeypatch):
    def movies(films):
        return [
            movie(films, "e1", "Елхи", 2010, '<set tmdbcolid="5"><name>Елхи (поредица)</name></set>', added=OLD),
            movie(films, "e2", "Елхи 2", 2011, '<set tmdbcolid="5"><name>Елхи (поредица)</name></set>', added=OLD),
            movie(films, "b1", "Брат", 1997, "<set><name>Брат (поредица)</name></set>", added=OLD),
            movie(films, "b2", "Брат 2", 2000, "<set><name>Брат (поредица)</name></set>", added=OLD),
            movie(films, "d1", "Девет", 1986, "<set><name>Девет (поредица)</name></set>", added=OLD),
            movie(films, "s1", "Сам", 2000, '<set tmdbcolid="77"><name>Една</name></set>', added=OLD),
        ]

    # One movie is not told from TMDB's collection for a movie alone; an empty database: the collection is back from its folder, without its movies
    _, calls, made = setup(tmp_path, monkeypatch, movies, {"Елхи (поредица)": ["e1"]})
    plan = [
        ("Елхи (поредица)", "добавяне", ["Елхи 2"]),
        ("Брат (поредица)", "нова", ["Брат", "Брат 2"]),
    ]
    assert boxsets.restore() == plan and made == {}
    assert not [c for c in calls if c[1] == "POST"]
    assert boxsets.restore(apply=True) == plan
    assert made == {"Брат (поредица)": ["b1", "b2"]}
    assert ("/Collections/Елхи (поредица)/Items", {"ids": "e2"}) in [(c[0], c[2]) for c in calls]
