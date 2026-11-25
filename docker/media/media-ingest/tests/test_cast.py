import xml.etree.ElementTree as ET

import cast
import metadata
from common import nfo, tmdb


def test_missing_actors_are_added_after_the_ones_the_nfo_has(fake):
    root = ET.fromstring(
        "<movie><title>Ант-Мен</title>"
        "<actor><name>Paul Rudd</name><role>Скот</role><type>Actor</type><order>0</order><tmdbid>22226</tmdbid></actor>"
        # As Jellyfin wrote them: no TMDB id
        "<actor><name>Michael Douglas</name><role>Хенри</role><type>Actor</type><order>1</order></actor>"
        "<actor><name>Somebody Else</name><type>Actor</type><order>2</order></actor>"
        "</movie>"
    )
    ant_man = tmdb.movie(102899)
    entries = metadata.cast("movie", ant_man) + [({"id": 2, "name": "Someone", "profile_path": "/s.jpg"}, None, "X")]

    assert metadata.merge_cast(root, ant_man, entries) == 2
    actors = root.findall("actor")
    assert [a.findtext("name") for a in actors] == ["Paul Rudd", "Michael Douglas", "Somebody Else", "Someone"]
    # The one Jellyfin wrote gets the id and keeps its role; the new one comes last with its photo
    assert actors[1].findtext("tmdbid") == "3392" and actors[1].findtext("role") == "Хенри"
    assert actors[3].findtext("order") == "3" and (actors[3].findtext("thumb") or "").endswith("/2.jpg")
    # Again: nothing to add
    assert metadata.merge_cast(root, ant_man, entries) == 0


def test_pass_adds_guest_stars_to_episodes_and_counts(fake, tmp_path, monkeypatch):
    series = tmp_path / "Serials" / "Friends (1994) [imdbid-tt0108778]"
    season = series / "Season 01"
    season.mkdir(parents=True)
    (series / "tvshow.nfo").write_text("<tvshow><title>Friends</title><lockdata>true</lockdata></tvshow>")
    for n in (1, 2):
        (season / f"S01E0{n}.mkv").write_text("video")
        (season / f"S01E0{n}.nfo").write_text(
            f"<episodedetails><title>E{n}</title><season>1</season><episode>{n}</episode></episodedetails>"
        )
    reread = []
    monkeypatch.setattr(cast, "reread", lambda item_id, user_id: reread.append(item_id))
    monkeypatch.setattr(
        cast,
        "jellyfin",
        lambda path, *a, **k: {"Items": [{"Id": "e1", "Path": str(season / "S01E01.mkv")}]},
    )
    monkeypatch.setattr(cast, "BACKUP_DIR", str(tmp_path / "backup"))
    row = dict.fromkeys(
        ("titles_done", "episodes_done", "changed", "episodes_changed", "actors", "skipped", "errors"), 0
    )
    items = [("tv", str(series), str(series / "tvshow.nfo"), {"Id": "s"})]

    cast.run_library("Сериали", {"Locations": [str(tmp_path / "Serials")]}, items, row, "admin")

    e1 = nfo.read(str(season / "S01E01.nfo"))
    assert e1 is not None and [a.findtext("name") for a in e1.findall("actor")] == ["Someone"]
    assert row["titles_done"] == 1 and row["episodes_done"] == 2 and row["episodes_changed"] == 1
    assert row["actors"] == 1 and reread == ["e1"]
    assert len(list((tmp_path / "backup").iterdir())) == 1
