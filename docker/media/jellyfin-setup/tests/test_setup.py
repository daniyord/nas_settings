import os

import setup

CONFIG = {
    "options": {"EnableRealtimeMonitor": True, "MetadataSavers": []},
    "users": {"home": ["Филми"], "root": "all", "guest": ["Филми"]},
    "library": [
        {
            "name": "Филми",
            "type": "movies",
            "paths": ["/movies1/Films", "/movies2/Films"],
            "options": {"PreferredMetadataLanguage": "bg"},
        },
        {"name": "Сериали", "type": "tvshows", "paths": ["/movies1/Serials"]},
        {"name": "Collections", "type": "boxsets", "paths": ["/config/collections"], "create": False},
    ],
}
FOLDERS = {
    "Филми": {
        "ItemId": "f1",
        "CollectionType": "movies",
        "Locations": ["/movies1/Films", "/old/Films"],
        "LibraryOptions": {
            "EnableRealtimeMonitor": True,
            "MetadataSavers": ["Nfo"],
            "PreferredMetadataLanguage": "bg",
            "Other": 1,
        },
    },
    "Музика": {"ItemId": "m1", "CollectionType": "music", "Locations": ["/music"], "LibraryOptions": {}},
}


def test_libraries(monkeypatch):
    calls = []
    monkeypatch.setattr(setup, "jellyfin", lambda *a: calls.append(a))
    changes = setup.plan_libraries(CONFIG, FOLDERS)
    texts = [t for t, _ in changes]
    assert "Филми: нова папка /movies2/Films" in texts
    assert "Филми: папка /old/Films не е в описанието (остава)" in texts
    assert 'Филми: MetadataSavers ["Nfo"] → []' in texts
    assert "Сериали: нова библиотека (tvshows, /movies1/Serials)" in texts
    assert "Collections: няма я (Jellyfin я прави сам)" in texts
    assert "Музика: библиотеката не е в описанието (остава)" in texts
    for _, action in changes:
        if action:
            action()
    by_path = {c[0].split("?")[0]: c for c in calls}
    # Settings keep what the description doesn't name
    assert by_path["/Library/VirtualFolders/LibraryOptions"][2] == {
        "Id": "f1",
        "LibraryOptions": {
            "EnableRealtimeMonitor": True,
            "MetadataSavers": [],
            "PreferredMetadataLanguage": "bg",
            "Other": 1,
        },
    }
    new = by_path["/Library/VirtualFolders"]
    assert "collectionType=tvshows" in new[0] and "refreshLibrary=false" in new[0]
    assert new[2]["LibraryOptions"]["PathInfos"] == [{"Path": "/movies1/Serials"}]
    assert not any("Музика" in str(c) for c in calls)


def test_nothing_to_do_when_the_server_matches(monkeypatch):
    folders = {
        "Филми": {
            "ItemId": "f1",
            "CollectionType": "movies",
            "Locations": ["/a"],
            "LibraryOptions": {"MetadataSavers": [], "X": 2},
        }
    }
    config = {"options": {"MetadataSavers": []}, "library": [{"name": "Филми", "type": "movies", "paths": ["/a"]}]}
    assert setup.plan_libraries(config, folders) == []


def test_users(monkeypatch):
    calls = []
    monkeypatch.setattr(setup, "jellyfin", lambda *a: calls.append(a))
    users = {
        "home": {"Id": "h", "Policy": {"EnableAllFolders": True, "EnabledFolders": [], "Other": 1}},
        "root": {"Id": "r", "Policy": {"EnableAllFolders": True, "EnabledFolders": []}},
    }
    changes = setup.plan_users(CONFIG, FOLDERS, users)
    assert [t for t, _ in changes] == ["потребител home: вижда Филми", "потребител guest: няма го (създава се на ръка)"]
    changes[0][1]()
    assert calls == [("/Users/h/Policy", "POST", {"EnableAllFolders": False, "EnabledFolders": ["f1"], "Other": 1})]


def test_description_in_the_repo_is_complete():
    config = setup.load(os.path.join(setup.HERE, "libraries.toml"))
    names = {library["name"] for library in config["library"]}
    for access in config["users"].values():
        assert access == "all" or set(access) <= names
    for library in config["library"]:
        options = setup.wanted_options(config, library)
        assert library["type"] and library["paths"]
        assert "PathInfos" not in options


def test_server_options_and_plugins(monkeypatch):
    calls = []
    server = {"MetadataOptions": [{"ItemType": "Movie", "DisabledMetadataFetchers": []}], "Other": 1}

    def jellyfin(path, method="GET", body=None, **params):
        calls.append((path, method, body))
        if path == "/System/Configuration" and method == "GET":
            return server
        if path == "/Plugins":
            return [
                {"Name": "OMDb", "Id": "o", "Version": "1", "Status": "Active"},
                {"Name": "TMDb", "Id": "t", "Version": "1", "Status": "Active"},
            ]
        return None

    monkeypatch.setattr(setup, "jellyfin", jellyfin)
    config = {
        "metadata_options": {"Person": {"DisabledMetadataFetchers": ["TheMovieDb"]}},
        "plugins": {"disabled": ["OMDb"]},
    }
    changes = setup.plan_server(config) + setup.plan_plugins(config)
    assert [t for t, _ in changes][-1] == "плъгин OMDb: изключва се (рестартирай Jellyfin)"
    for _, action in changes:
        if action:
            action()
    posted = [c for c in calls if c[1] == "POST"]
    assert posted[0][0] == "/System/Configuration"
    person = next(o for o in posted[0][2]["MetadataOptions"] if o["ItemType"] == "Person")
    assert person["DisabledMetadataFetchers"] == ["TheMovieDb"] and posted[0][2]["Other"] == 1
    assert posted[1][0] == "/Plugins/o/1/Disable"
    assert setup.plan_server({}) == [] and setup.plan_plugins({"plugins": {"disabled": []}}) == []
