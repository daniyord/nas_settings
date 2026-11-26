from library import studios


def test_studios_count_the_videos_under_each_studio_over_all_folders(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    for path in (
        one / "Vivid" / "Some Movie (2008) [tpdbid-x]" / "Some Movie.mp4",
        one / "Vivid" / "Some Movie (2008) [tpdbid-x]" / "movie.nfo",
        one / "Vivid" / "Other" / "cd1.avi",
        one / "Vivid" / "Other" / "cd2.avi",
        two / "Vivid" / "Third.mkv",
        two / "Wicked" / "Fourth.mp4",
        one / ".nomedia",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
    assert studios([str(one), str(two), str(tmp_path / "missing")]) == {"Vivid": 4, "Wicked": 1}
