import pytest

import names


def test_bg_sort_puts_cyrillic_in_alphabet_order_before_latin():
    titles = ["Avatar", "Яко", "Шрек", "Аватар", "Ъгъл", "Щастие", "Юла", "Ьо"]
    assert sorted(titles, key=names.bg_sort) == ["Аватар", "Шрек", "Щастие", "Ъгъл", "Ьо", "Юла", "Яко", "Avatar"]


def test_bg_sort_ignores_case_and_punctuation():
    assert names.bg_sort("Мисия: Невъзможна!") == names.bg_sort("мисия невъзможна")


def test_bg_sort_keeps_digits_for_jellyfin_to_pad():
    assert names.bg_sort("Роки 2") == names.bg_sort("Роки") + " 2"


def test_padded_pads_numbers_as_jellyfin_does():
    assert names.padded("Rocky 2") == "Rocky 0000000002"


@pytest.mark.parametrize(
    ("title", "clean"),
    [
        ("Star Wars: Episode IV", "Star Wars - Episode IV"),
        ("9 1/2 Weeks", "9½ Weeks"),
        ("AC/DC: Let There Be Rock", "AC-DC - Let There Be Rock"),
        ('What "Lies" Beneath?', "What Lies Beneath"),
        ("Mr. Nobody.", "Mr. Nobody"),
        ("  Две   думи ", "Две думи"),
    ],
)
def test_clean_title(title, clean):
    assert names.clean_title(title) == clean


def test_target_name_uses_original_title_with_imdb_id():
    item = {
        "OriginalTitle": "The Thing",
        "Name": "Нещото",
        "ProductionYear": 2011,
        "ProviderIds": {"Imdb": "tt0905372"},
    }
    assert names.target_name(item) == "The Thing (2011) [imdbid-tt0905372]"


def test_target_name_falls_back_to_localized_name_for_non_cyrillic_script():
    item = {
        "OriginalTitle": "千と千尋の神隠し",
        "Name": "Отнесена от духовете",
        "ProductionYear": 2001,
        "ProviderIds": {"Tmdb": "129"},
    }
    assert names.target_name(item) == "Отнесена от духовете (2001) [tmdbid-129]"


@pytest.mark.parametrize(
    ("name", "title"),
    [
        ("The.Walking.Dead.S01.HDTV.x264", "The.Walking.Dead."),
        ("Arcane S01", "Arcane "),
        ("Boardwalk.Empire.S04E04.720p", "Boardwalk.Empire."),
        ("Под прикритие сезон 2", "Под прикритие "),
        ("Cheers", "Cheers"),
    ],
)
def test_release_title(name, title):
    assert names.release_title(name) == title


@pytest.mark.parametrize(
    ("episode", "stem", "name"),
    [
        (
            {"ParentIndexNumber": 1, "IndexNumber": 3, "Name": "The Base Violence Necessary for Change"},
            "S01E03",
            "S01E03 - The Base Violence Necessary for Change",
        ),
        (
            {"ParentIndexNumber": 8, "IndexNumber": 1, "Name": "Columbo Goes to the Guillotine"},
            "x",
            "S08E01 - Columbo Goes to the Guillotine",
        ),
        (
            {"ParentIndexNumber": 2, "IndexNumber": 5, "IndexNumberEnd": 6, "Name": "Pilot: Part 1"},
            "x",
            "S02E05-E06 - Pilot - Part 1",
        ),
        ({"ParentIndexNumber": 1, "IndexNumber": 7, "IndexNumberEnd": 7, "Name": "Same"}, "x", "S01E07 - Same"),
        # Until Jellyfin has the title, the name is the file's or a placeholder
        ({"ParentIndexNumber": 1, "IndexNumber": 2, "Name": "Episode 2"}, "x", "S01E02"),
        ({"ParentIndexNumber": 1, "IndexNumber": 2, "Name": "Епизод 2"}, "x", "S01E02"),
        ({"ParentIndexNumber": 1, "IndexNumber": 2, "Name": "TBA"}, "x", "S01E02"),
        ({"ParentIndexNumber": 1, "IndexNumber": 2, "Name": "Show.S01E02.720p"}, "Show.S01E02.720p", "S01E02"),
        ({"ParentIndexNumber": 1, "IndexNumber": 2}, "x", "S01E02"),
    ],
)
def test_episode_name(episode, stem, name):
    assert names.episode_name(episode, stem) == name


def test_season_dir_reuses_the_series_own_folder(tmp_path):
    (tmp_path / "Season 1").mkdir()
    (tmp_path / "S03").mkdir()
    (tmp_path / "Season 4.txt").write_text("")
    assert names.season_dir(str(tmp_path), 1) == "Season 1"
    assert names.season_dir(str(tmp_path), 3) == "S03"
    assert names.season_dir(str(tmp_path), 2) == "Season 02"
    assert names.season_dir(str(tmp_path), 4) == "Season 04"
    assert names.season_dir(str(tmp_path / "missing"), 1) == "Season 01"


@pytest.mark.parametrize(
    ("rest", "name"),
    [
        (".nfo", "movie.nfo"),
        ("-poster.jpg", "folder.jpg"),
        ("-fanart.JPG", "fanart.jpg"),
        (".srt", None),
    ],
)
def test_generic_name(rest, name):
    assert names.generic_name(rest) == name


def test_norm_compares_titles_without_case_punctuation_or_ampersands():
    assert names.norm("Fast & Furious: 7") == names.norm("fast and furious 7") == "fastandfurious7"
    assert names.norm("Мисия: Невъзможна") == "мисияневъзможна"


def test_locked_reads_lockdata_from_nfo(tmp_path):
    nfo = tmp_path / "movie.nfo"
    nfo.write_text("<movie>\n  <lockdata>true</lockdata>\n</movie>")
    assert names.locked(nfo)
    nfo.write_text("<movie>\n  <lockdata>false</lockdata>\n</movie>")
    assert not names.locked(nfo)
    assert not names.locked(tmp_path / "missing.nfo")
