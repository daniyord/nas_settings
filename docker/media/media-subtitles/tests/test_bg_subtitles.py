import pytest

import bg_subtitles


@pytest.mark.parametrize(
    ("title", "numbers"),
    [
        ("Boardwalk Empire - 04x04", (4, 4)),
        ("Arcane - Season 2", (2, None)),
        ("Колумбо - сезон 8", (8, None)),
        ("Columbo - 08 - ", (8, None)),
        ("The Avengers", (None, None)),
        ("Ocean's 11", (None, None)),
    ],
)
def test_listing_numbers(title, numbers):
    assert bg_subtitles.numbers(title) == numbers
