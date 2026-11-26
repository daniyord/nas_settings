import people


def test_photos_follow_the_performers_the_nfos_name(tmp_path, monkeypatch):
    library = tmp_path / "Porn" / "Vivid" / "Movie [tpdbid-x]"
    library.mkdir(parents=True)
    (library / "movie.nfo").write_text(
        "<movie><actor><name>A</name><tpdbid>p-1</tpdbid></actor><actor><name>B</name><tpdbid>p-2</tpdbid></actor>"
        "<actor><name>C</name><tmdbid>7</tmdbid></actor></movie>"
    )
    photos = tmp_path / "people"
    photos.mkdir()
    (photos / "tpdb-gone.jpg").write_bytes(b"x")
    (photos / "7.jpg").write_bytes(b"x")
    monkeypatch.setattr(people.tpdb, "performer", lambda i: {"image": "https://cdn/a.jpg" if i == "p-1" else None})
    monkeypatch.setattr(people.tpdb, "download", lambda url, path: open(path, "wb").write(b"x"))

    ids = people.performer_ids([str(tmp_path / "Porn")])
    assert ids == {"p-1", "p-2"}
    assert people.update_photos(ids, str(photos)) == (1, ["p-2"])
    # The TMDB actors' photos are media-ingest's
    assert sorted(p.name for p in photos.iterdir()) == ["7.jpg", "tpdb-p-1.jpg"]


def test_the_catalog_lists_each_performer_with_their_titles(tmp_path, monkeypatch):
    movie = tmp_path / "Porn" / "Vivid" / "Movie (2001) [tpdbid-x]"
    movie.mkdir(parents=True)
    (movie / "Movie.avi").write_bytes(b"")
    (movie / "movie.nfo").write_text(
        "<movie><title>Movie</title><year>2001</year><studio>Vivid</studio>"
        "<actor><name>A</name><tpdbid>p-1</tpdbid></actor><actor><name>C</name><tmdbid>7</tmdbid></actor>"
        "<actor><name>Name Alone</name></actor></movie>"
    )
    bare = tmp_path / "Porn" / "Vivid" / "Bare"
    bare.mkdir()
    (bare / "movie.nfo").write_text("<movie><title>Bare</title><studio>Vivid</studio></movie>")
    scenes = tmp_path / "PornScenes" / "Brazzers"
    scenes.mkdir(parents=True)
    (scenes / "Angel Tits.mp4").write_bytes(b"")
    (scenes / "Angel Tits.nfo").write_text("<movie><title>Angel Tits</title><studio>Brazzers</studio></movie>")
    folders = [
        {"Name": "Порно", "ItemId": "porn", "Locations": [str(tmp_path / "Porn")]},
        {"Name": "Порно - сцени", "ItemId": "scenes", "Locations": [str(tmp_path / "PornScenes")]},
    ]
    items = {"porn": [{"Id": "m1", "Path": str(movie / "Movie.avi")}], "scenes": []}
    monkeypatch.setattr(
        people,
        "jellyfin",
        lambda path, **params: folders if path == "/Library/VirtualFolders" else {"Items": items[params["ParentId"]]},
    )

    performers, empty = people.build_catalog()

    assert sorted(performers) == ["7", "tpdb-p-1"]
    assert performers["tpdb-p-1"]["titles"] == [
        {"title": "Movie", "year": "2001", "studio": "Vivid", "library": "Порно", "item": "m1"}
    ]
    # A scene has no performers to miss
    assert [t["title"] for t in empty] == ["Bare"]
