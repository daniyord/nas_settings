import xml.etree.ElementTree as ET

from common import nfo
from common.nfo import element as E

# Shortened from an NFO Jellyfin wrote in Films, with a BOM as it writes them
JELLYFIN_NFO = """﻿<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<movie>
  <plot>АНТ-МЕН е един от членовете, основатели на Отмъстителите.</plot>
  <lockdata>false</lockdata>
  <title>Ант-Мен</title>
  <originaltitle>Ant-Man</originaltitle>
  <sorttitle>aaanasamafan</sorttitle>
  <tmdbid>102899</tmdbid>
  <genre>Екшън</genre>
  <studio />
  <art>
    <poster>/movies1/Films/Ant-Man (2015) [imdbid-tt0478970]/folder.jpg</poster>
  </art>
  <actor>
    <name>Paul Rudd</name>
    <role>Scott Lang</role>
    <type>Actor</type>
  </actor>
</movie>
"""


def ours():
    return [
        E("title", "Човекът мравка"),
        E("plot", "Друго описание"),
        E("year", 2015),
        E("genre", "Приключение"),
        E("studio", "Marvel Studios"),
        E("uniqueid", "102899", type="tmdb", default="true"),
        E("uniqueid", "tt0478970", type="imdb"),
        E(
            "actor",
            None,
            E("name", "Paul Rudd"),
            E("role", "Ant-Man"),
            E("tmdbid", 22226),
            E("thumb", "/people/22226.jpg"),
        ),
        E("actor", None, E("name", "Evangeline Lilly"), E("tmdbid", 19034)),
    ]


def test_path_for():
    assert nfo.path_for("movie", "/m/A (2015)") == "/m/A (2015)/movie.nfo"
    assert nfo.path_for("tvshow", "/s/B") == "/s/B/tvshow.nfo"
    assert nfo.path_for("season", "/s/B/Season 01") == "/s/B/Season 01/season.nfo"
    assert nfo.path_for("episode", "/s/B/Season 01/S01E02.mkv") == "/s/B/Season 01/S01E02.nfo"


def test_new_nfo_has_everything_and_is_locked(tmp_path):
    assert nfo.save("movie", str(tmp_path), ours())
    root = nfo.read(str(tmp_path / "movie.nfo"))
    assert root is not None and root.tag == "movie"
    assert root.findtext("title") == "Човекът мравка"
    assert root.findtext("year") == "2015"
    assert [a.findtext("name") for a in root.findall("actor")] == ["Paul Rudd", "Evangeline Lilly"]
    assert root.findtext("lockdata") == "true"
    assert (tmp_path / "movie.nfo").read_bytes().startswith(b'<?xml version="1.0" encoding="utf-8"')


def test_existing_nfo_keeps_what_is_there_and_gets_what_is_missing(tmp_path):
    (tmp_path / "movie.nfo").write_text(JELLYFIN_NFO, encoding="utf-8")
    assert nfo.save("movie", str(tmp_path), ours())
    root = nfo.read(str(tmp_path / "movie.nfo"))
    assert root is not None
    # Kept: a title, plot and genre list set before, and elements we don't write
    assert root.findtext("title") == "Ант-Мен"
    assert root.findtext("plot", "").startswith("АНТ-МЕН")
    assert [g.text for g in root.findall("genre")] == ["Екшън"]
    assert root.findtext("art/poster", "").endswith("folder.jpg")
    assert root.findtext("tmdbid") == "102899"
    # Added: what was missing or empty
    assert root.findtext("year") == "2015"
    assert [s.text for s in root.findall("studio")] == ["Marvel Studios"]
    assert {u.get("type"): u.text for u in root.findall("uniqueid")} == {"tmdb": "102899", "imdb": "tt0478970"}
    # Actors: the list stays, the known one gets the ids and photo it lacked, its role is kept
    actors = root.findall("actor")
    assert [a.findtext("name") for a in actors] == ["Paul Rudd"]
    assert actors[0].findtext("role") == "Scott Lang"
    assert actors[0].findtext("tmdbid") == "22226"
    assert actors[0].findtext("thumb") == "/people/22226.jpg"
    assert [e.text for e in root.findall("lockdata")] == ["true"]


def test_replace_overwrites_named_fields_only(tmp_path):
    (tmp_path / "movie.nfo").write_text(JELLYFIN_NFO, encoding="utf-8")
    nfo.save(
        "movie",
        str(tmp_path),
        [E("title", "Човекът мравка"), E("sorttitle", "x"), E("plot", "Друго")],
        replace={"title", "sorttitle"},
    )
    root = nfo.read(str(tmp_path / "movie.nfo"))
    assert root is not None
    assert root.findtext("title") == "Човекът мравка"
    assert [e.text for e in root.findall("sorttitle")] == ["x"]
    assert root.findtext("plot", "").startswith("АНТ-МЕН")


def test_unchanged_nfo_is_not_written_again(tmp_path):
    assert nfo.save("movie", str(tmp_path), ours())
    mtime = (tmp_path / "movie.nfo").stat().st_mtime_ns
    assert not nfo.save("movie", str(tmp_path), ours())
    assert (tmp_path / "movie.nfo").stat().st_mtime_ns == mtime


def test_broken_nfo_is_not_written_over(tmp_path):
    (tmp_path / "movie.nfo").write_text("<movie><title>Нещо</movie>")
    try:
        nfo.save("movie", str(tmp_path), ours())
        raise AssertionError("expected a ParseError")
    except ET.ParseError:
        pass
    assert (tmp_path / "movie.nfo").read_text() == "<movie><title>Нещо</movie>"
