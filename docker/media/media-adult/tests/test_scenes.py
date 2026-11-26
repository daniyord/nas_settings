import os
import xml.etree.ElementTree as ET

import scenes


def test_a_scene_without_our_nfo_gets_it_once_it_has_settled(tmp_path, monkeypatch):
    written = []

    def write(video, title, studio, alone):
        written.append((os.path.basename(video), title, studio))
        stem = os.path.splitext(video)[0]
        ET.ElementTree(ET.fromstring(f"<movie><title>{title}</title><studio>{studio}</studio></movie>")).write(
            stem + ".nfo"
        )
        open(stem + "-poster.jpg", "wb").close()

    monkeypatch.setattr(scenes.metadata, "write_unknown", write)
    monkeypatch.setattr(scenes, "event", lambda *a: None)
    studio = tmp_path / "Brazzers"
    studio.mkdir()
    old = studio / "Hard.And.En.Garde.1080p.mp4"
    old.write_bytes(b"")
    (studio / "Hard.And.En.Garde.1080p.nfo").write_text("<movie><title>x</title><fileinfo /></movie>")
    copying = studio / "New One.mp4"
    copying.write_bytes(b"")

    # Still being copied in
    assert scenes.fill([str(tmp_path)]) == []
    monkeypatch.setattr(scenes, "SETTLE_AFTER", -1)
    assert scenes.fill([str(tmp_path)]) == [str(old), str(copying)]
    assert written[0] == ("Hard.And.En.Garde.1080p.mp4", "Hard And En Garde", "Brazzers")
    # Ours is left as it is
    assert scenes.fill([str(tmp_path)]) == []
