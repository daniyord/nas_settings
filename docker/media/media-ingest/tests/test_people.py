import os

import people
from common import tmdb

MOVIE = """<movie><title>Х</title>
  <actor><name>Paul Rudd</name><tmdbid>22226</tmdbid><thumb>/people/22226.jpg</thumb></actor>
  <actor><name>Nobody Known</name><tmdbid>5</tmdbid></actor>
  <actor><name>No Photo</name><tmdbid>7</tmdbid></actor>
  <actor><name>Jellyfin's, without an id</name></actor>
  <uniqueid type="tmdb">102899</uniqueid>
</movie>"""


def test_actor_ids_come_from_the_actors_only(tmp_path):
    (tmp_path / "A (2015) [imdbid-tt1]").mkdir()
    (tmp_path / "A (2015) [imdbid-tt1]" / "movie.nfo").write_text(MOVIE)
    (tmp_path / "notes.txt").write_text("<actor><tmdbid>9</tmdbid></actor>")
    assert people.actor_ids([str(tmp_path)]) == {22226, 5, 7}


def test_update_photos_fetches_the_missing_and_drops_the_unused(tmp_path, monkeypatch):
    (tmp_path / "22226.jpg").write_text("kept")
    (tmp_path / "1.jpg").write_text("nobody's any more")
    (tmp_path / "README").write_text("not a photo")

    def person(tmdb_id):
        if tmdb_id == 5:
            raise tmdb.NotFound(tmdb_id)
        return {"profile_path": None if tmdb_id == 7 else f"/{tmdb_id}.jpg"}

    def download(file_path, destination, size="original"):
        with open(destination, "w") as f:
            f.write(file_path)

    monkeypatch.setattr(tmdb, "person", person)
    monkeypatch.setattr(tmdb, "download_image", download)
    assert people.update_photos({22226, 5, 7, 3392}, str(tmp_path)) == (2, 1, 1)
    assert sorted(os.listdir(tmp_path)) == ["22226.jpg", "3392.jpg", "README"]
    assert (tmp_path / "22226.jpg").read_text() == "kept"


def test_rename_actors_by_tmdb_id(tmp_path):
    path = tmp_path / "movie.nfo"
    path.write_text(MOVIE, encoding="utf-8")
    assert people.rename_actors(str(path), {7: "Без Снимка", 999: "Никой"})
    text = path.read_text(encoding="utf-8")
    assert "<name>Без Снимка</name>" in text and "<name>Paul Rudd</name>" in text
    assert not people.rename_actors(str(path), {7: "Без Снимка"})


def test_rename_actors_skips_a_release_nfo_in_an_old_code_page(tmp_path):
    path = tmp_path / "release.nfo"
    path.write_bytes(b"  \xb1\xdf\xdf art \xdb")
    assert not people.rename_actors(str(path), {7: "Без Снимка"})


def test_catalog_has_each_actor_with_the_titles_of_the_movies_and_series(tmp_path, monkeypatch):
    films, serials = tmp_path / "Films", tmp_path / "Serials"
    movie = films / "Ant-Man (2015) [imdbid-tt1]"
    movie.mkdir(parents=True)
    (movie / "movie.nfo").write_text(MOVIE.replace("<title>Х</title>", "<title>Ант-Мен</title><year>2015</year>"))
    (movie / "a.nfo").write_text(MOVIE.replace("<title>Х</title>", "<title>Ант-Мен</title><year>2015</year>"))
    show = serials / "Friends (1994) [imdbid-tt2]"
    (show / "Season 01").mkdir(parents=True)
    (show / "tvshow.nfo").write_text(
        "<tvshow><title>Приятели</title><actor><name>Paul Rudd</name><role>Mike</role><tmdbid>22226</tmdbid></actor>"
        "</tvshow>"
    )
    (show / "Season 01" / "S01E01.nfo").write_text(
        "<episodedetails><actor><name>Guest</name><tmdbid>1</tmdbid></actor></episodedetails>"
    )
    (films / "broken.nfo").write_text("not xml")

    def jellyfin(path, method="GET", **params):
        if path == "/Library/VirtualFolders":
            return [
                {"Name": "Филми", "ItemId": "f", "Locations": [str(films)]},
                {"Name": "Сериали", "ItemId": "s", "Locations": [str(serials)]},
            ]
        if params["ParentId"] == "f":
            return {"Items": [{"Id": "m1", "Path": str(movie / "a.mkv")}]}
        return {"Items": [{"Id": "s1", "Path": str(show)}]}

    monkeypatch.setattr(people, "jellyfin", jellyfin)
    monkeypatch.setattr(people, "LIBRARIES", ["Филми"])
    monkeypatch.setattr(people, "SERIES_LIBRARIES", ["Сериали"])
    actors = people.build_catalog()
    assert set(actors) == {22226, 5, 7}
    rudd = actors[22226]
    assert rudd["name"] == "Paul Rudd"
    assert [(t["title"], t["item"], t["role"]) for t in rudd["titles"]] == [
        ("Ант-Мен", "m1", ""),
        ("Приятели", "s1", "Mike"),
    ]
