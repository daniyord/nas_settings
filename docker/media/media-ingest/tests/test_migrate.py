import os
import tarfile

import migrate
from common import nfo, tmdb
from state import db

# Shortened from one Jellyfin wrote, with an actor in TMDB's Bulgarian spelling and a photo in Jellyfin's config
OLD = """﻿<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<movie>
  <plot>АНТ-МЕН е един от членовете, основатели на Отмъстителите.</plot>
  <lockdata>false</lockdata>
  <title>Човекът мравка</title>
  <originaltitle>Ant-Man</originaltitle>
  <sorttitle>bcaaaq</sorttitle>
  <imdbid>tt0478970</imdbid>
  <genre>Екшън</genre>
  <actor>
    <name>Пол Ръд</name>
    <role>Scott Lang</role>
    <type>Actor</type>
    <thumb>/config/data/metadata/People/P/Paul Rudd/folder.jpg</thumb>
  </actor>
  <actor>
    <name>Someone Else</name>
    <type>Producer</type>
  </actor>
</movie>
"""


def test_old_movie_keeps_its_data_and_gets_ours(fake, tmp_path):
    folder = tmp_path / "Ant-Man (2015) [imdbid-tt0478970]"
    folder.mkdir()
    (folder / "Ant-Man (2015) [imdbid-tt0478970].mkv").write_text("video")
    (folder / "movie.nfo").write_text(OLD, encoding="utf-8")
    (folder / "folder.jpg").write_text("chosen by hand")
    item = {"Name": "Човекът мравка", "Overview": "Друго описание, сменено в Jellyfin"}
    result, detail, _ = migrate.migrate_title("movie", str(folder), str(folder / "movie.nfo"), item)

    root = nfo.read(str(folder / "movie.nfo"))
    assert root is not None
    # Kept: the title, sort name, plot and genres it had
    assert root.findtext("title") == "Човекът мравка"
    assert [e.text for e in root.findall("sorttitle")] == ["bcaaaq"]
    assert root.findtext("plot", "").startswith("АНТ-МЕН")
    assert [g.text for g in root.findall("genre")] == ["Екшън"]
    # Added: ids, the collection, what was missing; locked
    assert {u.get("type"): u.text for u in root.findall("uniqueid")} == {"tmdb": "102899", "imdb": "tt0478970"}
    assert root.find("set") is not None and root.findtext("lockdata") == "true"
    # The actor in Latin, with its TMDB id and our photo instead of Jellyfin's
    actors = root.findall("actor")
    assert [a.findtext("name") for a in actors] == ["Paul Rudd", "Someone Else"]
    assert actors[0].findtext("tmdbid") == "22226"
    assert actors[0].findtext("thumb") == os.path.join(str(tmp_path / "people"), "22226.jpg")
    assert actors[0].findtext("role") == "Scott Lang"
    # The photo is there before Jellyfin reads the NFO
    assert (tmp_path / "people" / "22226.jpg").read_text() == "/rudd.jpg"
    # Artwork: only what was missing
    assert (folder / "folder.jpg").read_text() == "chosen by hand"
    assert (folder / "backdrop.jpg").exists()
    # Reported: the plot Jellyfin shows otherwise, and a title other than TMDB's
    assert result == "different"
    assert "описание в Jellyfin" in detail and "в TMDB „Ант-Мен“" in detail


def test_movie_like_tmdb_is_done_and_not_written_twice(fake, tmp_path):
    folder = tmp_path / "Ant-Man (2015) [imdbid-tt0478970]"
    folder.mkdir()
    (folder / "Ant-Man (2015) [imdbid-tt0478970].nfo").write_text(OLD.replace("Човекът мравка", "Ант-Мен"))
    nfo_file = str(folder / "Ant-Man (2015) [imdbid-tt0478970].nfo")
    assert migrate.migrate_title("movie", str(folder), nfo_file, None)[0] == "done"
    assert not (folder / "movie.nfo").exists()
    mtime = os.stat(nfo_file).st_mtime_ns
    assert migrate.migrate_title("movie", str(folder), nfo_file, None) == ("done", "", False)
    assert os.stat(nfo_file).st_mtime_ns == mtime


def test_unknown_title_is_only_locked(fake, tmp_path):
    folder = tmp_path / "Домашно видео (1999) [tmdbid-1]"
    folder.mkdir()
    (folder / "movie.nfo").write_text("<movie><title>Домашно видео</title></movie>")
    result, _, _ = migrate.migrate_title("movie", str(folder), str(folder / "movie.nfo"), None)
    root = nfo.read(str(folder / "movie.nfo"))
    assert result == "unidentified" and root is not None
    assert root.findtext("title") == "Домашно видео" and root.findtext("lockdata") == "true"


def test_series_gets_season_posters_and_locked_episodes(fake, tmp_path):
    folder = tmp_path / "Friends (1994) [imdbid-tt0108778]"
    season = folder / "Season 01"
    season.mkdir(parents=True)
    (folder / "tvshow.nfo").write_text("<tvshow><title>Приятели</title></tvshow>")
    (season / "S01E01.mkv").write_text("video")
    (season / "S01E01.nfo").write_text("<episodedetails><title>Пилотен</title></episodedetails>")
    (season / "notes.nfo").write_text("not an episode's")
    result, detail, _ = migrate.migrate_title("tv", str(folder), str(folder / "tvshow.nfo"), None)
    assert result == "done" and "season01-poster.jpg" in detail
    assert (folder / "season01-poster.jpg").read_text() == "/s1.jpg"
    episode = nfo.read(str(season / "S01E01.nfo"))
    assert episode is not None and episode.findtext("lockdata") == "true" and episode.findtext("title") == "Пилотен"
    assert (season / "notes.nfo").read_text() == "not an episode's"


def test_library_is_backed_up_before_the_first_change(fake, tmp_path, monkeypatch):
    location = tmp_path / "Films"
    folder = location / "Ant-Man (2015) [imdbid-tt0478970]"
    folder.mkdir(parents=True)
    (folder / "a.mkv").write_text("video")
    (folder / "movie.nfo").write_text(OLD, encoding="utf-8")
    (location / "Release.Not.Organized").mkdir()
    monkeypatch.setattr(migrate, "BACKUP_DIR", str(tmp_path / "backup"))
    calls = []
    monkeypatch.setattr(migrate, "jellyfin", lambda *a, **k: calls.append((a, k)) or {"Items": []})
    db.execute("DELETE FROM migrated")
    migrate.run_library("Филми", {"ItemId": "x", "Locations": [str(location)]})

    (backup,) = os.listdir(tmp_path / "backup")
    with tarfile.open(tmp_path / "backup" / backup) as tar:
        names = tar.getnames()
        member = tar.extractfile(names[0])
        assert member is not None and member.read().decode("utf-8") == OLD
    assert names == [str(folder / "movie.nfo").lstrip("/")]
    rows = db.execute("SELECT path, result FROM migrated").fetchall()
    assert [tuple(r) for r in rows] == [(str(folder), "different")]
    assert calls[-1][0] == ("/Items/x/Refresh", "POST") and calls[-1][1]["MetadataRefreshMode"] == "Default"
    summary = migrate.summary()
    assert summary["problems"][0]["result"] == "different"


def test_russian_actor_goes_back_to_cyrillic_everywhere(fake, monkeypatch):
    import xml.etree.ElementTree as ET

    d = {
        "id": 22358,
        "credits": {"cast": [{"id": 86940, "name": "Виктор Сухоруков"}, {"id": 22226, "name": "Пол Ръд"}]},
    }
    monkeypatch.setattr(
        tmdb,
        "credits",
        lambda kind, i: {"cast": [{"id": 86940, "name": "Viktor Sukhorukov"}, {"id": 22226, "name": "Paul Rudd"}]},
    )
    root = ET.fromstring(
        "<movie><actor><name>Viktor Sukhorukov</name></actor><actor><name>Пол Ръд</name></actor></movie>"
    )
    migrate.native_names(root, "movie", d)
    assert [a.findtext("name") for a in root.findall("actor")] == ["Виктор Сухоруков", "Paul Rudd"]


def test_animation_keeps_no_actors_but_its_director(fake, tmp_path, monkeypatch):
    cartoon = {**tmdb.movie(102899), "genres": [{"id": 16, "name": "Анимация"}]}
    monkeypatch.setattr(tmdb, "movie", lambda i: cartoon)
    folder = tmp_path / "Ant-Man (2015) [imdbid-tt0478970]"
    folder.mkdir()
    (folder / "movie.nfo").write_text(OLD.replace("<type>Producer</type>", "<type>Director</type>"), encoding="utf-8")
    migrate.migrate_title("movie", str(folder), str(folder / "movie.nfo"), None)
    root = nfo.read(str(folder / "movie.nfo"))
    assert root is not None
    assert [(a.findtext("name"), a.findtext("type")) for a in root.findall("actor")] == [("Someone Else", "Director")]
    assert not (tmp_path / "people" / "22226.jpg").exists()
