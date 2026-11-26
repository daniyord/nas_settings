import io
import xml.etree.ElementTree as ET

from PIL import Image

import metadata
import poster

FOUND = {
    "id": "7279ea39-cb3c-44a3-bee0-25856a6b78ad",
    "title": "Carmen & Austyn ",
    "date": "2006-03-20",
    "description": "Best Friends With Benefits!",
    "duration": "7290",
    "site": {"name": "Adam & Eve"},
    "tags": [{"name": "Lesbian"}],
    "posters": {"large": "https://cdn/cover.jpg"},
    "background": {"large": "https://cdn/back.jpg"},
    "performers": [
        {
            "id": f"s{n}",
            "name": f"Site {n}",
            "parent": {"id": f"p{n}", "name": f"Performer {n}", "image": f"https://cdn/p{n}.jpg"},
        }
        for n in range(25)
    ],
}


def fake_download(downloads):
    def download(url, destination):
        downloads.append((url, destination))
        with open(destination, "wb") as f:
            f.write(b"jpg")

    return download


def test_a_movie_gets_its_nfo_artwork_and_first_performers(tmp_path, monkeypatch):
    folder = tmp_path / "Adam&Eve" / "Carmen & Austyn (2006) [tpdbid-7279ea39-cb3c-44a3-bee0-25856a6b78ad]"
    folder.mkdir(parents=True)
    people = tmp_path / "people"
    people.mkdir()
    downloads = []
    monkeypatch.setattr(metadata.tpdb, "download", fake_download(downloads))
    monkeypatch.setattr(metadata, "PEOPLE_DIR", str(people))
    monkeypatch.setattr(metadata, "own", lambda path, like: None)

    metadata.write_movie(str(folder), FOUND, "Adam&Eve")

    root = ET.parse(folder / "movie.nfo").getroot()
    assert root.findtext("title") == "Carmen & Austyn"
    assert root.findtext("year") == "2006" and root.findtext("premiered") == "2006-03-20"
    assert root.findtext("runtime") == "122" and root.findtext("mpaa") == "XXX"
    assert [s.text for s in root.findall("studio")] == ["Adam & Eve"]
    assert root.findtext("uniqueid") == "movies/7279ea39-cb3c-44a3-bee0-25856a6b78ad"
    assert root.find("genre") is None and root.find("tag") is None and root.find("set") is None
    assert root.findtext("lockdata") == "true"
    actors = root.findall("actor")
    assert len(actors) == 20
    assert actors[0].findtext("name") == "Performer 0" and actors[0].findtext("tpdbid") == "p0"
    assert actors[0].findtext("thumb") == str(people / "tpdb-p0.jpg")
    assert (folder / "folder.jpg").exists() and (folder / "backdrop.jpg").exists()
    # Photos only of the performers named, each once
    assert len([d for d in downloads if "/people/" in d[1]]) == 20
    metadata.write_movie(str(folder), FOUND, "Adam&Eve")
    assert len([d for d in downloads if "/people/" in d[1]]) == 20


def test_an_unknown_movie_or_scene_gets_its_title_studio_and_a_poster(tmp_path, monkeypatch):
    monkeypatch.setattr(poster, "video_frame", lambda path, at: Image.new("RGB", (656, 368), (90, 40, 40)))
    monkeypatch.setattr(metadata, "own", lambda path, like: None)
    folder = tmp_path / "Penthouse" / "Sneak Peek 9"
    folder.mkdir(parents=True)
    video = folder / "Sneak Peek 9.avi"
    video.write_bytes(b"")
    metadata.write_unknown(str(video), "Sneak Peek 9", "Penthouse")
    root = ET.parse(folder / "movie.nfo").getroot()
    assert root.findtext("title") == "Sneak Peek 9" and root.findtext("studio") == "Penthouse"
    assert Image.open(folder / "folder.jpg").size == (1000, 1500)

    scenes = tmp_path / "Brazzers"
    scenes.mkdir()
    # What Jellyfin wrote for a scene goes, but for when it was added
    (scenes / "Madison Ivy.nfo").write_text(
        "<movie><title>Madison Ivy</title><dateadded>2018-10-06 23:02:10</dateadded><fileinfo /></movie>"
    )
    for name in ("Madison Ivy.mp4", "Hottest Bosses.mp4"):
        (scenes / name).write_bytes(b"")
        metadata.write_unknown(str(scenes / name), name[:-4], "Brazzers", alone=False)
    root = ET.parse(scenes / "Madison Ivy.nfo").getroot()
    assert [e.tag for e in root] == ["title", "studio", "mpaa", "dateadded"]
    assert root.findtext("dateadded") == "2018-10-06 23:02:10"
    assert (scenes / "Hottest Bosses-poster.jpg").exists() and not (scenes / "movie.nfo").exists()


def test_a_poster_sets_a_trailing_number_apart():
    image = Image.open(io.BytesIO(poster.make(Image.new("RGB", (640, 360)), "Sneak Peek 9", "Penthouse")))
    assert image.size == (1000, 1500)
    # The number in gold under the title
    gold = sum(
        1
        for p in image.crop((0, 200, 1000, 500)).get_flattened_data()
        if isinstance(p, tuple) and p[0] > 150 and p[2] < 120
    )
    assert gold > 1000
