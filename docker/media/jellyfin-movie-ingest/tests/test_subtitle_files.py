import ingest

BULGARIAN = "1\n00:00:01,000 --> 00:00:02,000\nЗдравей, как си днес?\n"
ENGLISH = "1\n00:00:01,000 --> 00:00:02,000\nHello, how are you today?\n"


def test_subtitle_language_reads_cp1251_and_utf8(tmp_path):
    cp1251, utf8, english = tmp_path / "a.srt", tmp_path / "b.srt", tmp_path / "c.srt"
    cp1251.write_bytes(BULGARIAN.encode("cp1251"))
    utf8.write_text(BULGARIAN, encoding="utf-8")
    english.write_text(ENGLISH)
    assert ingest.subtitle_language(str(cp1251)) == "bg"
    assert ingest.subtitle_language(str(utf8)) == "bg"
    assert ingest.subtitle_language(str(english)) is None


def test_subtitle_language_skips_image_subtitles(tmp_path):
    (tmp_path / "a.idx").write_text("")
    (tmp_path / "a.sub").write_bytes(b"\x00\x01")
    assert ingest.subtitle_language(str(tmp_path / "a.idx")) is None
    assert ingest.subtitle_language(str(tmp_path / "a.sub")) is None


def test_with_language_adds_a_found_language_only(tmp_path):
    bulgarian, english = tmp_path / "a.srt", tmp_path / "b.srt"
    bulgarian.write_text(BULGARIAN)
    english.write_text(ENGLISH)
    assert ingest.with_language(str(bulgarian), ".srt") == ".bg.srt"
    assert ingest.with_language(str(english), ".srt") == ".srt"
    assert ingest.with_language(str(english), ".en.forced.srt") == ".en.forced.srt"


def test_convert_microdvd_uses_the_frame_rate_in_its_header(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "refresh_video", lambda *a: None)
    video = tmp_path / "Movie (2000) [imdbid-tt1].avi"
    video.write_bytes(b"")
    sub = tmp_path / "Movie (2000) [imdbid-tt1].sub"
    sub.write_bytes("{1}{1}25\n{25}{50}Здравей, как си днес?\n{75}{100}Добре съм.\n".encode("cp1251"))
    assert ingest.convert_microdvd(str(sub), str(video))
    srt = (tmp_path / "Movie (2000) [imdbid-tt1].bg.srt").read_text(encoding="utf-8")
    assert "00:00:01,000 --> 00:00:02,000\nЗдравей, как си днес?" in srt
    assert "00:00:03,000 --> 00:00:04,000\nДобре съм." in srt
    # The frame rate line isn't a subtitle; the original is kept aside
    assert "25" not in srt.split("\n\n")[0].splitlines()[-1]
    assert (tmp_path / "Movie (2000) [imdbid-tt1].sub.orig").exists()
    assert not sub.exists()
