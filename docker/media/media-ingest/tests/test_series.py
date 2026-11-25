from datetime import datetime, timedelta, timezone

import series


def stamp(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


def test_new_episodes_named_after_the_file_are_read_again(tmp_path, monkeypatch):
    season = tmp_path / "Season 01"
    season.mkdir()
    for stem, title in (("S01E01", "Дебюти"), ("S01E02", "Размяна"), ("S01E03", "Старо")):
        (season / f"{stem}.nfo").write_text(
            f"<episodedetails><title>{title}</title></episodedetails>", encoding="utf-8"
        )
    episodes = [
        {"Id": "new", "Name": "S01E01", "Path": str(season / "S01E01.mkv"), "DateCreated": stamp(0)},
        {"Id": "named", "Name": "Размяна", "Path": str(season / "S01E02.mkv"), "DateCreated": stamp(1)},
        {"Id": "old", "Name": "S01E03", "Path": str(season / "S01E03.mkv"), "DateCreated": stamp(5)},
    ]
    read = []
    monkeypatch.setattr(series, "jellyfin", lambda path, **params: {"Items": episodes})
    monkeypatch.setattr(series, "reread", lambda item_id, user_id: read.append(item_id))
    monkeypatch.setattr(series, "log", lambda *a: None)
    monkeypatch.setattr(series, "reread_tried", set())

    assert series.reread_new_episodes({"ItemId": "lib"}, {"Id": "a"}) == 1 and read == ["new"]
    # Once: Jellyfin may keep its own name
    assert series.reread_new_episodes({"ItemId": "lib"}, {"Id": "a"}) == 0 and read == ["new"]
