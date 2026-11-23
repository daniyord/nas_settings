import pytest

import sync

BULGARIAN = "1\n00:00:01,000 --> 00:00:02,000\nЗдравей, как си днес?\n"
ENGLISH = "1\n00:00:01,000 --> 00:00:02,000\nHello, how are you today?\n"


@pytest.mark.parametrize(
    ("offset", "scale", "safe"),
    [
        (7.87, 25 / 23.976, True),
        (-20, 1, True),
        (20.5, 1, False),
        (3, 1.01, False),
        (0.6, 24 / 25, True),
    ],
)
def test_safe_to_fix(offset, scale, safe):
    assert sync.safe_to_fix(offset, scale) is safe


def test_convert_microdvd_uses_the_frame_rate_in_its_header(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "refresh_video", lambda *a: None)
    video = tmp_path / "Movie (2000) [imdbid-tt1].avi"
    video.write_bytes(b"")
    sub = tmp_path / "Movie (2000) [imdbid-tt1].sub"
    sub.write_bytes("{1}{1}25\n{25}{50}Здравей, как си днес?\n{75}{100}Добре съм.\n".encode("cp1251"))
    assert sync.convert_microdvd(str(sub), str(video))
    srt = (tmp_path / "Movie (2000) [imdbid-tt1].bg.srt").read_text(encoding="utf-8")
    assert "00:00:01,000 --> 00:00:02,000\nЗдравей, как си днес?" in srt
    assert "00:00:03,000 --> 00:00:04,000\nДобре съм." in srt
    # The frame rate line isn't a subtitle; the original is kept aside
    assert "25" not in srt.split("\n\n")[0].splitlines()[-1]
    assert (tmp_path / "Movie (2000) [imdbid-tt1].sub.orig").exists()
    assert not sub.exists()
