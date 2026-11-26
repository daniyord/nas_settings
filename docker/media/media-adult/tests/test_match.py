import match


def movie(id, title, site, minutes, date="2006-03-20", cover=True):
    return {
        "id": id,
        "title": title,
        "site": {"name": site},
        "duration": str(minutes * 60),
        "date": date,
        "posters": {"large": f"https://cdn/{id}.jpg"} if cover else {},
    }


def test_guess_takes_the_title_before_the_year_or_a_release_token():
    assert match.guess("Carmen.And.Austyn.2006.XXX.DVDRip.x264-GRP") == ("Carmen And Austyn", 2006)
    assert match.guess("Some Title XXX 1080p WEB-DL") == ("Some Title", None)
    assert match.guess("Sneak Peek 9") == ("Sneak Peek 9", None)
    assert match.guess("[site.com] Jane Blond DD7 (2006)") == ("Jane Blond DD7", 2006)


def test_an_id_tag_or_a_link_pins_the_match():
    uuid = "7279ea39-cb3c-44a3-bee0-25856a6b78ad"
    assert match.pinned(f"Carmen (2006) [tpdbid-{uuid}]") == ("tpdb", uuid)
    assert match.pinned(f"https://theporndb.net/movies/{uuid}") == ("tpdb", uuid)
    assert match.pinned("https://theporndb.net/movies/adameve-carmen-austyn") == ("tpdb", "adameve-carmen-austyn")
    assert match.pinned(uuid) == ("tpdb", uuid)
    assert match.pinned("https://www.themoviedb.org/movie/125808-cheap-booze") == ("tmdb", "125808")
    assert match.pinned("Movie [tmdbid-125808]") == ("tmdb", "125808")
    assert match.pinned("XXX Axel Braun Parody") is None


def test_the_studio_is_left_out_of_the_title_when_compared():
    assert "Guide to Blow Jobs" in match.studio_free("The Penthouse Guide to Blow Jobs", "Penthouse")
    assert "Friends With Benefits" in match.studio_free("Friends With Benefits (Penthouse)", "Penthouse")
    assert "the Housekeeper" in match.studio_free("Housekeeper, the", "")


def search_with(monkeypatch, results):
    monkeypatch.setattr(match.tpdb, "search", lambda query, site=None: results)


def test_one_title_from_one_studio_with_the_runtime_is_taken(monkeypatch):
    search_with(
        monkeypatch, [movie("a", "Carmen & Austyn", "Adam & Eve", 121), movie("b", "Carmen Rocks", "Vivid", 90)]
    )
    found, why, candidates = match.search_tpdb("Carmen And Austyn", 2006, 122, [])
    assert found and found["id"] == "a" and why is None
    assert candidates[0]["id"] == "a" and candidates[0]["similarity"] == 1.0


def test_a_re_release_gives_way_to_the_original_with_a_cover(monkeypatch):
    search_with(
        monkeypatch,
        [
            movie("late", "Pirates", "Digital Playground", 129, "2010-01-01"),
            movie("early", "Pirates", "Digital Playground", 128, "2005-09-01"),
            movie("nocover", "Pirates", "Digital Playground", 129, "2004-01-01", cover=False),
        ],
    )
    found, why, _ = match.search_tpdb("Pirates", None, 129, [])
    assert found and found["id"] == "early"


def test_doubts(monkeypatch):
    search_with(monkeypatch, [movie("a", "Pirates", "Digital Playground", 129), movie("b", "Pirates", "Vivid", 127)])
    assert match.search_tpdb("Pirates", None, 128, [])[1] == "същото заглавие от 2 студиа"
    search_with(monkeypatch, [movie("a", "Pirates", "Digital Playground", 129)])
    assert match.search_tpdb("Pirates", None, 90, [])[1] == "продължителността се различава с 39 мин"
    assert match.search_tpdb("Pirates", None, None, [])[1] == "няма продължителност, с която да се провери"
    assert match.search_tpdb("Something Else", None, 129, [])[0] is None


def test_a_keyword_is_searched_alone_and_with_the_title(monkeypatch):
    asked = []
    monkeypatch.setattr(match.tpdb, "search", lambda query, site=None: asked.append(query) or [])
    match.search_tpdb("Cinderella", None, 100, ["XXX Axel Braun Parody"])
    assert {"Cinderella", "XXX Axel Braun Parody", "Cinderella XXX Axel Braun Parody"} <= set(asked)


def test_a_site_belongs_to_a_studio_folder():
    folders = ["Adam&Eve", "Wicked", "Vivid", "Marc Dorcel"]
    assert match.studio_folder("Wicked Pictures", folders, ["Wicked"]) == "Wicked"
    assert match.studio_folder("Adam & Eve", folders) == "Adam&Eve"
    assert match.studio_folder("Brazzers", folders) is None
