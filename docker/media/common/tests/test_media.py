from common import media

BULGARIAN = "1\n00:00:01,000 --> 00:00:02,000\nЗдравей, как си днес?\n"
ENGLISH = "1\n00:00:01,000 --> 00:00:02,000\nHello, how are you today?\n"


def test_subtitle_language_reads_cp1251_and_utf8(tmp_path):
    cp1251, utf8, english = tmp_path / "a.srt", tmp_path / "b.srt", tmp_path / "c.srt"
    cp1251.write_bytes(BULGARIAN.encode("cp1251"))
    utf8.write_text(BULGARIAN, encoding="utf-8")
    english.write_text(ENGLISH)
    assert media.subtitle_language(str(cp1251)) == "bg"
    assert media.subtitle_language(str(utf8)) == "bg"
    assert media.subtitle_language(str(english)) is None


def test_subtitle_language_skips_image_subtitles(tmp_path):
    (tmp_path / "a.idx").write_text("")
    (tmp_path / "a.sub").write_bytes(b"\x00\x01")
    assert media.subtitle_language(str(tmp_path / "a.idx")) is None
    assert media.subtitle_language(str(tmp_path / "a.sub")) is None


def test_with_language_adds_a_found_language_only(tmp_path):
    bulgarian, english = tmp_path / "a.srt", tmp_path / "b.srt"
    bulgarian.write_text(BULGARIAN)
    english.write_text(ENGLISH)
    assert media.with_language(str(bulgarian), ".srt") == ".bg.srt"
    assert media.with_language(str(english), ".srt") == ".srt"
    assert media.with_language(str(english), ".en.forced.srt") == ".en.forced.srt"
