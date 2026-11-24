import json
import os

import userdata

MOVIE = {
    "Id": "m1",
    "Type": "Movie",
    "Name": "Ант-Мен",
    "Path": "/movies1/Films/Ant-Man (2015) [imdbid-tt0478970]/a.mkv",
    "ProviderIds": {"Imdb": "tt0478970", "Tmdb": "102899"},
    "UserData": {
        "Played": True,
        "PlayCount": 2,
        "PlaybackPositionTicks": 0,
        "IsFavorite": True,
        "LastPlayedDate": "2026-11-20T20:00:00Z",
    },
}
EPISODE = {
    "Id": "e1",
    "Type": "Episode",
    "Name": "Пилотен",
    "Path": "/movies1/Serials/F/Season 01/S01E01.mkv",
    "SeriesName": "Friends",
    "ParentIndexNumber": 1,
    "IndexNumber": 1,
    "ProviderIds": {"Tmdb": "85987"},
    "UserData": {"Played": False, "PlaybackPositionTicks": 6_000_000_000, "IsFavorite": False},
}
SERIES = {
    "Id": "s1",
    "Type": "Series",
    "Name": "Friends",
    "Path": "/movies1/Serials/F",
    "ProviderIds": {},
    "UserData": {"Played": True, "IsFavorite": True},
}


def fake_jellyfin(calls, items_for_user, library):
    def jellyfin(path, method="GET", body=None, **params):
        calls.append((path, method, body, params))
        if path == "/Users":
            return [{"Name": "home", "Id": "h"}, {"Name": "test", "Id": "t"}]
        if path == "/Items" and "userId" in params:
            return {
                "Items": [i for i in items_for_user if params["Filters"] != "IsFavorite" or i["UserData"]["IsFavorite"]]
            }
        if path == "/Items":
            return {"Items": library}
        return None

    return jellyfin


def test_export_keeps_the_last_ones(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(userdata, "jellyfin", fake_jellyfin(calls, [MOVIE, EPISODE, SERIES], []))
    for day in range(1, 32):
        (tmp_path / f"2026-10-{day:02d}.json").write_text("{}")
    path, counts = userdata.export(str(tmp_path), "2026-11-24")
    assert counts == {"home": 3, "test": 3}
    names = sorted(os.listdir(tmp_path))
    assert len(names) == userdata.KEEP and names[-1] == "2026-11-24.json" and names[0] == "2026-10-03.json"
    home = {e["name"]: e for e in json.loads(open(path).read())["users"]["home"]}
    assert home["Ант-Мен"]["played"] and home["Ант-Мен"]["play_count"] == 2 and home["Ант-Мен"]["favorite"]
    assert home["Пилотен"]["position"] == 6_000_000_000 and home["Пилотен"]["series"] == "Friends"
    # A series' played state is its episodes'
    assert "played" not in home["Friends"] and home["Friends"]["favorite"]


def test_restore_finds_by_path_then_by_id(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(userdata, "jellyfin", fake_jellyfin(calls, [MOVIE, EPISODE, SERIES], []))
    path, _ = userdata.export(str(tmp_path), "2026-11-24")
    # After a rebuilt Jellyfin: new ids, the movie at a new path, the series gone
    library = [
        {
            "Id": "M",
            "Type": "Movie",
            "Path": "/movies2/Films/Ant-Man (2015) [imdbid-tt0478970]/a.mkv",
            "ProviderIds": {"Imdb": "tt0478970"},
        },
        {"Id": "E", "Type": "Episode", "Path": EPISODE["Path"], "ProviderIds": {}},
    ]
    calls.clear()
    monkeypatch.setattr(userdata, "jellyfin", fake_jellyfin(calls, [], library))
    userdata.restore(path, only="home", to="test")
    assert not [c for c in calls if c[1] == "POST"]
    userdata.restore(path, only="home", to="test", apply=True)
    posts = {c[0]: (c[2], c[3]) for c in calls if c[1] == "POST"}
    assert set(posts) == {"/UserItems/M/UserData", "/UserItems/E/UserData"}
    body, params = posts["/UserItems/M/UserData"]
    assert params == {"userId": "t"}
    assert body == {
        "IsFavorite": True,
        "Played": True,
        "PlayCount": 2,
        "PlaybackPositionTicks": 0,
        "LastPlayedDate": "2026-11-20T20:00:00Z",
    }
    assert posts["/UserItems/E/UserData"][0]["PlaybackPositionTicks"] == 6_000_000_000
