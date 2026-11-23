import bg_subtitles
import ingest
import pytest


def test_bg_sort_puts_cyrillic_in_alphabet_order_before_latin():
    titles = ["Avatar", "Яко", "Шрек", "Аватар", "Ъгъл", "Щастие", "Юла", "Ьо"]
    assert sorted(titles, key=ingest.bg_sort) == ["Аватар", "Шрек", "Щастие", "Ъгъл", "Ьо", "Юла", "Яко", "Avatar"]


def test_bg_sort_ignores_case_and_punctuation():
    assert ingest.bg_sort("Мисия: Невъзможна!") == ingest.bg_sort("мисия невъзможна")


def test_bg_sort_keeps_digits_for_jellyfin_to_pad():
    assert ingest.bg_sort("Роки 2") == ingest.bg_sort("Роки") + " 2"


def test_padded_pads_numbers_as_jellyfin_does():
    assert ingest.padded("Rocky 2") == "Rocky 0000000002"


@pytest.mark.parametrize(("title", "clean"), [
    ("Star Wars: Episode IV", "Star Wars - Episode IV"),
    ("9 1/2 Weeks", "9½ Weeks"),
    ("AC/DC: Let There Be Rock", "AC-DC - Let There Be Rock"),
    ('What "Lies" Beneath?', "What Lies Beneath"),
    ("Mr. Nobody.", "Mr. Nobody"),
    ("  Две   думи ", "Две думи"),
])
def test_clean_title(title, clean):
    assert ingest.clean_title(title) == clean


def test_target_name_uses_original_title_with_imdb_id():
    item = {"OriginalTitle": "The Thing", "Name": "Нещото", "ProductionYear": 2011, "ProviderIds": {"Imdb": "tt0905372"}}
    assert ingest.target_name(item) == "The Thing (2011) [imdbid-tt0905372]"


def test_target_name_falls_back_to_localized_name_for_non_cyrillic_script():
    item = {"OriginalTitle": "千と千尋の神隠し", "Name": "Отнесена от духовете", "ProductionYear": 2001, "ProviderIds": {"Tmdb": "129"}}
    assert ingest.target_name(item) == "Отнесена от духовете (2001) [tmdbid-129]"


@pytest.mark.parametrize(("name", "title"), [
    ("The.Walking.Dead.S01.HDTV.x264", "The.Walking.Dead."),
    ("Arcane S01", "Arcane "),
    ("Boardwalk.Empire.S04E04.720p", "Boardwalk.Empire."),
    ("Под прикритие сезон 2", "Под прикритие "),
    ("Cheers", "Cheers"),
])
def test_release_title(name, title):
    assert ingest.release_title(name) == title


@pytest.mark.parametrize(("episode", "stem", "name"), [
    ({"ParentIndexNumber": 1, "IndexNumber": 3, "Name": "The Base Violence Necessary for Change"}, "S01E03",
     "S01E03 - The Base Violence Necessary for Change"),
    ({"ParentIndexNumber": 8, "IndexNumber": 1, "Name": "Columbo Goes to the Guillotine"}, "x", "S08E01 - Columbo Goes to the Guillotine"),
    ({"ParentIndexNumber": 2, "IndexNumber": 5, "IndexNumberEnd": 6, "Name": "Pilot: Part 1"}, "x", "S02E05-E06 - Pilot - Part 1"),
    ({"ParentIndexNumber": 1, "IndexNumber": 7, "IndexNumberEnd": 7, "Name": "Same"}, "x", "S01E07 - Same"),
    # Until Jellyfin has the title, the name is the file's or a placeholder
    ({"ParentIndexNumber": 1, "IndexNumber": 2, "Name": "Episode 2"}, "x", "S01E02"),
    ({"ParentIndexNumber": 1, "IndexNumber": 2, "Name": "Епизод 2"}, "x", "S01E02"),
    ({"ParentIndexNumber": 1, "IndexNumber": 2, "Name": "TBA"}, "x", "S01E02"),
    ({"ParentIndexNumber": 1, "IndexNumber": 2, "Name": "Show.S01E02.720p"}, "Show.S01E02.720p", "S01E02"),
    ({"ParentIndexNumber": 1, "IndexNumber": 2}, "x", "S01E02"),
])
def test_episode_name(episode, stem, name):
    assert ingest.episode_name(episode, stem) == name


def test_season_dir_reuses_the_series_own_folder(tmp_path):
    (tmp_path / "Season 1").mkdir()
    (tmp_path / "S03").mkdir()
    (tmp_path / "Season 4.txt").write_text("")
    assert ingest.season_dir(str(tmp_path), 1) == "Season 1"
    assert ingest.season_dir(str(tmp_path), 3) == "S03"
    assert ingest.season_dir(str(tmp_path), 2) == "Season 02"
    assert ingest.season_dir(str(tmp_path), 4) == "Season 04"
    assert ingest.season_dir(str(tmp_path / "missing"), 1) == "Season 01"


@pytest.mark.parametrize(("offset", "scale", "safe"), [
    (7.87, 25 / 23.976, True),
    (-20, 1, True),
    (20.5, 1, False),
    (3, 1.01, False),
    (0.6, 24 / 25, True),
])
def test_safe_to_fix(offset, scale, safe):
    assert ingest.safe_to_fix(offset, scale) is safe


@pytest.mark.parametrize(("rest", "name"), [
    (".nfo", "movie.nfo"),
    ("-poster.jpg", "folder.jpg"),
    ("-fanart.JPG", "fanart.jpg"),
    (".srt", None),
])
def test_generic_name(rest, name):
    assert ingest.generic_name(rest) == name


@pytest.mark.parametrize(("title", "numbers"), [
    ("Boardwalk Empire - 04x04", (4, 4)),
    ("Arcane - Season 2", (2, None)),
    ("Колумбо - сезон 8", (8, None)),
    ("Columbo - 08 - ", (8, None)),
    ("The Avengers", (None, None)),
    ("Ocean's 11", (None, None)),
])
def test_listing_numbers(title, numbers):
    assert bg_subtitles.numbers(title) == numbers


def test_norm_compares_titles_without_case_punctuation_or_ampersands():
    assert ingest.norm("Fast & Furious: 7") == ingest.norm("fast and furious 7") == "fastandfurious7"
    assert ingest.norm("Мисия: Невъзможна") == "мисияневъзможна"
