import os

import incoming


def setup(tmp_path, monkeypatch):
    library = tmp_path / "Porn"
    (library / "Adam&Eve").mkdir(parents=True)
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    updates = []
    scenes = tmp_path / "PornScenes"
    scenes.mkdir()
    monkeypatch.setattr(
        incoming,
        "library_locations",
        lambda name=incoming.LIBRARY: [str(scenes if name == incoming.SCENES_LIBRARY else library)],
    )
    monkeypatch.setattr(incoming, "jellyfin", lambda path, method="GET", body=None, **params: updates.append(body))
    monkeypatch.setattr(
        incoming,
        "announce",
        lambda paths, rescan=False: updates.append({"Updates": [{"Path": p, "UpdateType": "Created"} for p in paths]}),
    )
    monkeypatch.setattr(incoming, "event", lambda *a: None)
    monkeypatch.setattr(incoming.metadata, "own", lambda path, like: None)
    written = []
    monkeypatch.setattr(
        incoming.metadata, "write_movie", lambda folder, found, studio: written.append((folder, studio))
    )
    monkeypatch.setattr(
        incoming.tpdb,
        "movie",
        lambda ref: {
            "id": "7279ea39",
            "title": "Carmen & Austyn",
            "date": "2006-03-20",
            "site": {"name": "Adam & Eve"},
        },
    )
    return library, downloads, updates, written


def add(path):
    with incoming.lock:
        incoming.db.execute("DELETE FROM incoming")
        incoming.db.execute("INSERT INTO incoming (path, state, added_at) VALUES (?, 'new', 0)", (str(path),))
        return incoming.db.execute("SELECT id FROM incoming").fetchone()[0]


def test_a_matched_download_gets_its_folder_under_its_studio(tmp_path, monkeypatch):
    library, downloads, updates, written = setup(tmp_path, monkeypatch)
    release = downloads / "Carmen.And.Austyn.2006.XXX.DVDRip"
    release.mkdir()
    (release / "carmen.avi").write_bytes(b"x" * 1000)
    (release / "sample.avi").write_bytes(b"x")
    (release / "info.nfo").write_text("release notes")
    row_id = add(release)

    incoming.place(row_id, ("tpdb", "7279ea39"), None)

    folder = library / "Adam&Eve" / "Carmen & Austyn (2006) [tpdbid-7279ea39]"
    assert os.listdir(folder) == ["Carmen & Austyn.avi"]
    assert written == [(str(folder), "Adam&Eve")]
    assert not release.exists()
    assert updates[-1] == {"Updates": [{"Path": str(folder), "UpdateType": "Created"}]}
    assert incoming.row(row_id)["state"] == "done"

    # The same movie downloaded again stays where it is
    again = downloads / "carmen.mp4"
    again.write_bytes(b"x")
    row_id = add(again)
    incoming.place(row_id, ("tpdb", "7279ea39"), None)
    assert incoming.row(row_id)["state"] == "duplicate" and again.exists()


def test_a_new_studio_gets_its_folder_and_a_movie_by_hand_its_title(tmp_path, monkeypatch):
    library, downloads, _, _ = setup(tmp_path, monkeypatch)
    made = []
    monkeypatch.setattr(
        incoming.metadata, "write_unknown", lambda video, title, studio, at: made.append((title, studio, at))
    )
    video = downloads / "sp9.avi"
    video.write_bytes(b"x")
    row_id = add(video)
    incoming.place(row_id, None, "Penthouse", "Sneak Peek 9", 0.4)
    assert (library / "Penthouse" / "Sneak Peek 9" / "Sneak Peek 9.avi").exists()
    assert made == [("Sneak Peek 9", "Penthouse", 0.4)]


def test_a_failed_write_leaves_nothing_in_the_library(tmp_path, monkeypatch):
    library, downloads, _, _ = setup(tmp_path, monkeypatch)

    def broken(folder, found, studio):
        raise OSError("no space")

    monkeypatch.setattr(incoming.metadata, "write_movie", broken)
    monkeypatch.setattr(incoming, "log", lambda text: None)
    video = downloads / "carmen.avi"
    video.write_bytes(b"x")
    row_id = add(video)
    incoming.place(row_id, ("tpdb", "7279ea39"), None)
    assert video.exists() and os.listdir(library / "Adam&Eve") == []
    assert incoming.row(row_id)["state"] == "failed"


def test_a_scene_download_goes_under_its_studio_with_each_video_a_scene(tmp_path, monkeypatch):
    _, downloads, updates, _ = setup(tmp_path, monkeypatch)
    made = []
    monkeypatch.setattr(
        incoming.metadata,
        "write_unknown",
        lambda video, title, studio, at, alone: made.append((os.path.basename(video), title, studio, alone)),
    )
    monkeypatch.setattr(incoming, "LEFTOVER_VIDEO", 10)
    pack = downloads / "Sceneries" / "Brazzers.Pack"
    pack.mkdir(parents=True)
    (pack / "Angel.Tits.1080p.mp4").write_bytes(b"x" * 100)
    (pack / "Power.Trios.MP4").write_bytes(b"x" * 100)
    (pack / "sample.mp4").write_bytes(b"x")
    row_id = add(pack)

    incoming.place_scene(row_id, "Brazzers", "ignored for a pack")

    studio = tmp_path / "PornScenes" / "Brazzers"
    assert sorted(os.listdir(studio)) == ["Angel Tits.mp4", "Power Trios.mp4"]
    assert made == [
        ("Angel Tits.mp4", "Angel Tits", "Brazzers", False),
        ("Power Trios.mp4", "Power Trios", "Brazzers", False),
    ]
    assert not pack.exists() and incoming.row(row_id)["state"] == "done"
    assert len(updates[-1]["Updates"]) == 2

    # One scene takes the title typed; one already there stays where it is
    single = downloads / "Angel.Tits.mp4"
    single.write_bytes(b"x" * 100)
    row_id = add(single)
    incoming.place_scene(row_id, "Brazzers", "")
    assert incoming.row(row_id)["state"] == "duplicate" and single.exists()
    incoming.place_scene(row_id, "Brazzers", "Angel Tits (Jessa Rhodes)")
    assert (studio / "Angel Tits (Jessa Rhodes).mp4").exists()


def test_a_download_in_a_studio_folder_is_searched_there_and_placed_under_it(tmp_path, monkeypatch):
    library, downloads, _, _ = setup(tmp_path, monkeypatch)
    (library / "Vivid").mkdir()
    monkeypatch.setattr(incoming, "SETTLE_AFTER", -1)
    monkeypatch.setattr(incoming, "downloads", [str(downloads)])
    release = downloads / "Vivid" / "Cry.Wolf.XXX.DVDRip"
    release.mkdir(parents=True)
    (release / "cry.wolf.avi").write_bytes(b"x")
    loose = downloads / "Something.Else.avi"
    loose.write_bytes(b"x")
    with incoming.lock:
        incoming.db.execute("DELETE FROM incoming")

    incoming.scan([])

    with incoming.lock:
        rows = {r["path"]: r["studio"] for r in incoming.db.execute("SELECT path, studio FROM incoming")}
    assert rows == {str(release): "Vivid", str(loose): None}

    searched, placed = [], []
    monkeypatch.setattr(incoming.match, "site_names", lambda studio: ["Vivid", "Vivid Premium"])

    def search(title, year, minutes, words, sites=None):
        searched.append(sites)
        return {"id": "b241"}, None, []

    monkeypatch.setattr(incoming.match, "search_tpdb", search)
    monkeypatch.setattr(incoming, "runtime", lambda video: 90)
    monkeypatch.setattr(incoming, "place", lambda row_id, pin, studio: placed.append((pin, studio)))
    with incoming.lock:
        row_id = incoming.db.execute("SELECT id FROM incoming WHERE studio = 'Vivid'").fetchone()[0]
    incoming.identify(incoming.row(row_id))
    assert searched == [["Vivid", "Vivid Premium"]]
    assert placed == [(("tpdb", "b241"), "Vivid")]
