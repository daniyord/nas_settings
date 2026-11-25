import os

import metadata
import movies
import series
from common import nfo, tmdb
from state import db


def test_from_link(fake):
    assert metadata.from_link("movie", "https://www.themoviedb.org/movie/102899-ant-man?language=bg") == 102899
    assert metadata.from_link("tv", "https://www.themoviedb.org/movie/102899") is None
    assert metadata.from_link("movie", "https://www.imdb.com/title/tt0478970/") == 102899
    assert metadata.from_link("tv", "tt0478970") is None
    assert metadata.from_link("tv", " 1668 ") == 1668
    assert metadata.from_link("movie", "нещо") is None


def found_id(kind, path, names):
    details = metadata.identify(kind, path, names)
    assert details is not None
    return details["id"]


def test_identify_by_id_in_name_then_by_search(fake):
    assert found_id("movie", "/x", ["Ant-Man (2015) [imdbid-tt0478970]"]) == 102899
    assert found_id("tv", "/y", ["Friends [tmdbid-1668] S01"]) == 1668
    assert fake == []
    # The second result is the one that looks like the name and has its year
    assert found_id("movie", "/z", ["Ant-Man.2015.1080p.BluRay.x264"]) == 102899
    assert metadata.identify("movie", "/w", ["Something.Else.2001"]) is None


def test_identify_prefers_the_title_picked_on_the_page(fake):
    db.execute("INSERT INTO chosen VALUES (?, ?, 0)", ("/v.mkv", 102899))
    assert found_id("movie", "/v.mkv", ["Whatever.1990"]) == 102899


def test_movie_nfo(fake, tmp_path):
    root = nfo.merge(nfo.element("movie"), metadata.movie_nfo(tmdb.movie(102899)))
    assert root.findtext("title") == "Ант-Мен"
    assert root.findtext("originaltitle") == "Ant-Man"
    assert root.findtext("sorttitle") == "aaanasamafan"
    # No Bulgarian overview: the English one rather than none
    assert root.findtext("plot") == "Armed with a super-suit..."
    assert root.findtext("mpaa") == "BG-B"
    assert root.findtext("rating") == "7.1"
    assert root.find("set").get("tmdbcolid") == "422834"
    assert {u.get("type"): u.text for u in root.findall("uniqueid")} == {"tmdb": "102899", "imdb": "tt0478970"}
    actors = root.findall("actor")
    # Names in Latin, the role in Bulgarian where TMDB has it, the photo downloaded when there is one
    assert [(a.findtext("name"), a.findtext("role"), a.findtext("tmdbid")) for a in actors] == [
        ("Paul Rudd", "Скот Ланг", "22226"),
        ("Michael Douglas", "Hank Pym", "3392"),
    ]
    assert actors[0].findtext("thumb") == str(tmp_path / "people" / "22226.jpg")
    assert actors[1].find("thumb") is None
    assert [d.text for d in root.findall("director")] == ["Peyton Reed"]
    assert [w.text for w in root.findall("writer")] == ["Edgar Wright"]


def item_for(path):
    return {"Id": "1", "Path": str(path), "Type": "Movie"}


def test_loose_movie_gets_its_folder_with_our_nfo_and_art(fake, tmp_path):
    library = tmp_path / "Films"
    library.mkdir()
    video = library / "Ant-Man.2015.1080p.mkv"
    video.write_text("video")
    # What Jellyfin saved next to it while it still wrote into the libraries
    (library / "Ant-Man.2015.1080p.nfo").write_text("<movie><title>Ant Farm</title></movie>")
    (library / "Ant-Man.2015.1080p-poster.jpg").write_text("old")
    details = metadata.identify("movie", str(video), [video.stem])
    item = metadata.as_item(item_for(video), "movie", details)
    movies.ingest(item, [video.name], str(library), "Филми", details)

    folder = library / "Ant-Man (2015) [imdbid-tt0478970]"
    assert sorted(os.listdir(folder)) == [
        "Ant-Man (2015) [imdbid-tt0478970].mkv",
        "backdrop.jpg",
        "folder.jpg",
        "landscape.jpg",
        "logo.png",
        "movie.nfo",
    ]
    assert sorted(os.listdir(library)) == [folder.name]
    assert (folder / "folder.jpg").read_text() == "/bg-poster.jpg"
    assert (folder / "backdrop.jpg").read_text() == "/clean-back.jpg"
    assert (folder / "landscape.jpg").read_text() == "/en-back.jpg"
    assert (folder / "logo.png").read_text() == "/logo.png"
    root = nfo.read(str(folder / "movie.nfo"))
    assert root is not None and root.findtext("title") == "Ант-Мен" and root.findtext("lockdata") == "true"
    assert db.execute("SELECT 1 FROM untitled").fetchone() is None


def test_release_folder_is_renamed_with_our_nfo_inside(fake, tmp_path):
    library = tmp_path / "Films"
    release = library / "Ant-Man.2015.1080p.BluRay"
    release.mkdir(parents=True)
    (release / "ant-man.mkv").write_text("video")
    (release / "Ant-Man.2015.1080p.BluRay.nfo").write_text("release notes, not XML")
    (release / "folder.jpg").write_text("Jellyfin's")
    video = release / "ant-man.mkv"
    details = metadata.identify("movie", str(video), [release.name, "ant-man"])
    item = metadata.as_item(item_for(video), "movie", details)
    movies.ingest(item, [release.name, video.name], str(library), "Филми", details)

    folder = library / "Ant-Man (2015) [imdbid-tt0478970]"
    assert not release.exists()
    assert "Ant-Man.2015.1080p.BluRay.nfo" not in os.listdir(folder)
    assert (folder / "folder.jpg").read_text() == "/bg-poster.jpg"
    assert nfo.read(str(folder / "movie.nfo")) is not None


def test_series_release_gets_nfos_that_move_with_the_episodes(fake, tmp_path):
    library = tmp_path / "Serials"
    release = library / "Friends.S01.1080p"
    release.mkdir(parents=True)
    episodes = []
    for n in (1, 2, 3):
        video = release / f"Friends.S01E0{n}.mkv"
        video.write_text("video")
        episodes.append({"Path": str(video), "ParentIndexNumber": 1, "IndexNumber": n, "Name": video.stem})
    (release / "tvshow.nfo").write_text("<tvshow><title>Jellyfin's</title></tvshow>")
    details = metadata.identify("tv", str(release), ["Friends.S01.1080p [imdbid-tt0108778]"])
    target = library / "Friends (1994) [imdbid-tt0108778]"
    series.write_metadata(str(release), episodes, str(target), details)
    series.organize_series(str(release), episodes, str(target), "Сериали")

    assert sorted(os.listdir(target)) == ["Season 01", "folder.jpg", "season01-poster.jpg", "tvshow.nfo"]
    show = nfo.read(str(target / "tvshow.nfo"))
    assert show is not None and show.findtext("title") == "Friends" and show.findtext("status") == "Ended"
    season = target / "Season 01"
    # TMDB's titles, Bulgarian where it has one; an episode TMDB lacks keeps Jellyfin's name and gets no NFO
    assert sorted(os.listdir(season)) == [
        "S01E01 - Пилотен-thumb.jpg",
        "S01E01 - Пилотен.mkv",
        "S01E01 - Пилотен.nfo",
        "S01E02 - The Sonogram-thumb.jpg",
        "S01E02 - The Sonogram.mkv",
        "S01E02 - The Sonogram.nfo",
        "S01E03.mkv",
    ]
    e1 = nfo.read(str(season / "S01E01 - Пилотен.nfo"))
    assert e1 is not None and e1.tag == "episodedetails" and e1.findtext("showtitle") == "Friends"
    assert e1.findtext("season") == "1" and e1.findtext("episode") == "1"
    # The episode's guest stars, with their photo
    guest = e1.find("actor")
    assert guest is not None and guest.findtext("name") == "Someone" and guest.findtext("type") == "GuestStar"
    assert guest.findtext("tmdbid") == "2" and (guest.findtext("thumb") or "").endswith("/2.jpg")


def test_actor_name_is_cyrillic_only_for_people_born_where_it_is_written(fake):
    assert metadata.actor_name(86940, "Viktor Sukhorukov", "Виктор Сухоруков") == "Виктор Сухоруков"
    assert metadata.actor_name(1, "Georgi Kaloyanchev", "Георги Калоянчев") == "Георги Калоянчев"
    assert metadata.actor_name(22226, "Paul Rudd", "Пол Ръд") == "Paul Rudd"
    # Unknown place of birth, no Cyrillic name, a person TMDB doesn't have: Latin
    assert metadata.actor_name(2, "Someone", "Някой") == "Someone"
    assert metadata.actor_name(86940, "Viktor Sukhorukov", None) == "Viktor Sukhorukov"
    assert metadata.actor_name(99, "Ghost", "Призрак") == "Ghost"


def test_transliterate():
    assert metadata.transliterate("Kiril Variyski") == "Кирил Варийски"
    assert metadata.transliterate("Ivan Dzhambazov") == "Иван Джамбазов"
    assert metadata.transliterate("Vassil Dimitrov") == "Васил Димитров"
    assert metadata.transliterate("Hristo Shtonov") == "Христо Щонов"
    assert metadata.transliterate("Nikolai Sotirov") == "Николай Сотиров"
    assert metadata.transliterate("Ivan Krystev") == "Иван Кръстев"
    assert metadata.transliterate("Paraskeva Djukelova") == "Параскева Джукелова"


def test_bulgarian_actors_get_cyrillic_from_tmdb_or_wait_for_a_name(fake):
    # One of TMDB's other names that reads as the Latin one; a band's name doesn't
    assert metadata.actor_name(3, "Stefan Danailov", None) == "Стефан Данаилов"
    assert metadata.actor_name(4, "Orlin Pavlov", None, title="Мила от Марс") == "Orlin Pavlov"
    # Born no one knows where: Cyrillic only in a Bulgarian or Russian title
    assert metadata.actor_name(5, "Lambi Kondov", None) == "Lambi Kondov"
    assert metadata.actor_name(5, "Lambi Kondov", "Ламби Кондов", cyrillic_title=True) == "Ламби Кондов"
    assert metadata.actor_name(5, "Kiril Variyski", None, cyrillic_title=True, title="Х") == "Kiril Variyski"
    rows = [tuple(r) for r in db.execute("SELECT tmdb_id, latin, suggestion, title FROM actor_names ORDER BY tmdb_id")]
    assert rows == [(4, "Orlin Pavlov", "Орлин Павлов", "Мила от Марс"), (5, "Kiril Variyski", "Кирил Варийски", "Х")]


def test_a_confirmed_name_wins(fake, tmp_path):
    (tmp_path / "people" / "names.json").write_text('{"4": "Орлин Павлов", "22226": "Пол Ръд"}', encoding="utf-8")
    assert metadata.actor_name(4, "Orlin Pavlov", None) == "Орлин Павлов"
    assert metadata.actor_name(22226, "Paul Rudd", None) == "Пол Ръд"


def test_animation_gets_no_actors(fake):
    cartoon = {**tmdb.movie(102899), "genres": [{"id": 16, "name": "Анимация"}]}
    tags = [e.tag for e in metadata.movie_nfo(cartoon)]
    assert "actor" not in tags and "director" in tags
