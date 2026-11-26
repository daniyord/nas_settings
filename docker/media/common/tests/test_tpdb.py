import json
import os
import time
import urllib.error

import pytest

from common import tpdb


def test_get_caches_answers_and_sends_the_token(tmp_path, monkeypatch):
    calls = []

    def fetch(url, headers):
        calls.append((url, headers))
        return json.dumps({"data": {"id": "abc"}}).encode()

    monkeypatch.setattr(tpdb, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(tpdb, "TOKEN", "token")
    monkeypatch.setattr(tpdb, "_fetch", fetch)
    assert tpdb.movie("abc") == {"id": "abc"}
    assert tpdb.movie("abc") == {"id": "abc"}
    assert len(calls) == 1
    assert calls[0][0] == "https://api.theporndb.net/movies/abc"
    assert calls[0][1]["Authorization"] == "Bearer token"


def test_a_search_is_cached_for_a_day_only(tmp_path, monkeypatch):
    calls = []

    def fetch(url, headers):
        calls.append(url)
        return json.dumps({"data": [{"id": "abc"}]}).encode()

    monkeypatch.setattr(tpdb, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(tpdb, "_fetch", fetch)
    assert tpdb.search("Carmen & Austyn", "Adam & Eve") == [{"id": "abc"}]
    assert "q=Carmen+%26+Austyn" in calls[0] and "site=Adam+%26+Eve" in calls[0]
    old = time.time() - 2 * 86400
    for name in os.listdir(tmp_path):
        os.utime(tmp_path / name, (old, old))
    tpdb.search("Carmen & Austyn", "Adam & Eve")
    assert len(calls) == 2
    tpdb.movie("abc")
    for name in os.listdir(tmp_path):
        os.utime(tmp_path / name, (old, old))
    tpdb.movie("abc")
    assert len(calls) == 3


def test_people_are_their_main_entries_once_each():
    found = {
        "performers": [
            {
                "id": "s1",
                "name": "Nicky Hunter",
                "image": None,
                "parent": {"id": "p1", "name": "Nicki Hunter", "image": "x.jpg"},
            },
            {"id": "s2", "name": "Jenna Haze", "image": "y.jpg", "parent": None},
            {"id": "s3", "name": "Nicki Hunter", "parent": {"id": "p1", "name": "Nicki Hunter", "image": "x.jpg"}},
        ]
    }
    assert tpdb.people(found) == [("p1", "Nicki Hunter", "x.jpg"), ("s2", "Jenna Haze", "y.jpg")]
    assert tpdb.people({}) == []


def test_cover_and_backdrop():
    assert tpdb.cover({"posters": {"large": "big.jpg"}, "poster": "small.jpg"}) == "big.jpg"
    assert tpdb.cover({"posters": None, "poster": "small.jpg"}) == "small.jpg"
    assert tpdb.cover({}) is None
    assert tpdb.backdrop({"background": {"large": "back.jpg"}}) == "back.jpg"
    assert tpdb.backdrop({}) is None


def test_not_found_is_its_own_error(monkeypatch):
    def urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(tpdb.urllib.request, "urlopen", urlopen)
    with pytest.raises(tpdb.NotFound):
        tpdb._fetch("https://api.theporndb.net/movies/none", {})
