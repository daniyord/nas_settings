import os

import media_json


def test_dismissed_follows_the_file_not_its_name(tmp_path):
    subtitle = tmp_path / "Movie.bg.srt"
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nЗдравей\n")
    media_json.update(str(tmp_path), audio="subs")
    media_json.remember_dismissed(str(subtitle))
    assert media_json.read(str(tmp_path))["audio"] == "subs"
    # Renamed with its video: still the same file
    renamed = tmp_path / "Movie (2001) [imdbid-tt1].bg.srt"
    os.rename(subtitle, renamed)
    assert media_json.was_dismissed(str(renamed), os.stat(renamed))
    # Changed: checked again, and the stale entry goes when another is kept
    renamed.write_text("1\n00:00:01,500 --> 00:00:02,500\nЗдравей\n")
    assert not media_json.was_dismissed(str(renamed), os.stat(renamed))
    other = tmp_path / "Other.bg.srt"
    other.write_text("x")
    media_json.remember_dismissed(str(other))
    assert list(media_json.read(str(tmp_path))["dismissed"]) == ["Other.bg.srt"]


def test_file_goes_when_empty(tmp_path):
    media_json.update(str(tmp_path), tagged=[["/m/a.mkv", 1]])
    media_json.update(str(tmp_path), tagged=[])
    assert not (tmp_path / media_json.NAME).exists()
