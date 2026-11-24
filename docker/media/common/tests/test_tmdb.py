import json
import urllib.error

import pytest

from common import tmdb


def test_pick_image_prefers_bulgarian_then_textless_then_english():
    images = [
        {"file_path": "/en.jpg", "iso_639_1": "en", "vote_average": 9},
        {"file_path": "/none.jpg", "iso_639_1": None, "vote_average": 5},
        {"file_path": "/de.jpg", "iso_639_1": "de", "vote_average": 10},
    ]
    assert tmdb.pick_image(images) == "/none.jpg"
    images.append({"file_path": "/bg-low.jpg", "iso_639_1": "bg", "vote_average": 1})
    images.append({"file_path": "/bg.jpg", "iso_639_1": "bg", "vote_average": 4})
    assert tmdb.pick_image(images) == "/bg.jpg"
    assert tmdb.pick_image([images[2]]) == "/de.jpg"
    assert tmdb.pick_image([]) is None


def test_bulgarian_title_comes_from_the_translations():
    details = {
        "title": "The Movie",
        "original_language": "en",
        "translations": {"translations": [{"iso_639_1": "de", "data": {"title": "Der Film"}}]},
    }
    assert tmdb.bulgarian_title(details) is None
    details["translations"]["translations"].append({"iso_639_1": "bg", "data": {"title": "Филмът"}})
    assert tmdb.bulgarian_title(details) == "Филмът"
    assert tmdb.bulgarian_title({"original_language": "bg", "original_name": "Под прикритие"}) == "Под прикритие"
    series = {"translations": {"translations": [{"iso_639_1": "bg", "data": {"name": "Приятели"}}]}}
    assert tmdb.bulgarian_title(series) == "Приятели"


def test_get_caches_answers(tmp_path, monkeypatch):
    calls = []

    def fetch(url, headers):
        calls.append(url)
        return json.dumps({"id": 603}).encode()

    monkeypatch.setattr(tmdb, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(tmdb, "API_KEY", "key")
    monkeypatch.setattr(tmdb, "_fetch", fetch)
    assert tmdb.get("movie/603") == {"id": 603}
    assert tmdb.get("movie/603") == {"id": 603}
    assert len(calls) == 1
    assert "language=bg-BG" in calls[0] and "api_key=key" in calls[0]


def test_get_sends_a_read_token_as_a_header(tmp_path, monkeypatch):
    seen = {}

    def fetch(url, headers):
        seen.update(url=url, headers=headers)
        return b"{}"

    monkeypatch.setattr(tmdb, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(tmdb, "API_KEY", "eyJtoken")
    monkeypatch.setattr(tmdb, "_fetch", fetch)
    tmdb.get("movie/1")
    assert seen["headers"]["Authorization"] == "Bearer eyJtoken"
    assert "api_key" not in seen["url"]


def test_find_imdb(monkeypatch):
    answers = {"movie_results": [], "tv_results": [{"id": 790}]}
    monkeypatch.setattr(tmdb, "get", lambda path, **params: answers)
    assert tmdb.find_imdb("tt0094525") == ("tv", 790)
    answers["tv_results"] = []
    assert tmdb.find_imdb("tt0000000") is None


def test_not_found_is_its_own_error(monkeypatch):
    def urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(tmdb.urllib.request, "urlopen", urlopen)
    with pytest.raises(tmdb.NotFound):
        tmdb._fetch("https://api.themoviedb.org/3/movie/0", {})
