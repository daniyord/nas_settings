import json

import audio
from state import db


def test_decision_is_kept_in_the_folder(tmp_path):
    folder = str(tmp_path)
    (tmp_path / audio.MEDIA_FILE).write_text(json.dumps({"other": 1}))
    audio.write_decision(folder, "bg")
    assert audio.read_decision(folder) == "bg"
    assert json.loads((tmp_path / audio.MEDIA_FILE).read_text()) == {"other": 1, "audio": "bg"}
    audio.write_decision(folder, None)
    assert audio.read_decision(folder) is None
    assert json.loads((tmp_path / audio.MEDIA_FILE).read_text()) == {"other": 1}


def test_file_goes_when_nothing_is_left_in_it(tmp_path):
    audio.write_decision(str(tmp_path), "subs")
    audio.write_decision(str(tmp_path), None)
    assert not (tmp_path / audio.MEDIA_FILE).exists()


def test_restore_fills_a_lost_database(tmp_path):
    kept, broken, empty = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    for d in (kept, broken, empty):
        d.mkdir()
    audio.write_decision(str(kept), "burned")
    (broken / audio.MEDIA_FILE).write_text("not json")
    db.execute("DELETE FROM audio")
    decisions = {}
    audio.restore([str(kept), str(broken), str(empty)], decisions)
    assert decisions == {str(kept): "burned"}
    assert [tuple(r) for r in db.execute("SELECT folder, decision FROM audio")] == [(str(kept), "burned")]
