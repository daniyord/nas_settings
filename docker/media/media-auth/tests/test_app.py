import app

ADMIN = {"name": "root", "admin": True, "adult": True}
ADULT = {"name": "adult", "admin": False, "adult": True}
HOME = {"name": "home", "admin": False, "adult": False}


def test_an_admin_may_do_anything_and_a_porn_viewer_only_look_at_adult():
    assert app.allowed(ADMIN, "/ingest/api/items/1/confirm", "POST")
    assert app.allowed(ADULT, "/adult/performers", "GET")
    assert app.allowed(ADULT, "/adult/api/status?x=1", "GET")
    assert not app.allowed(ADULT, "/adult/api/incoming/3", "DELETE")
    assert not app.allowed(ADULT, "/ingest/", "GET")
    assert not app.allowed(ADULT, "/adultery/", "GET")
    assert not app.allowed(HOME, "/adult/", "GET")


def test_a_sign_in_goes_back_only_to_this_site():
    assert app.safe_next("/adult/performers") == "/adult/performers"
    assert app.safe_next("//evil.example/") == ""
    assert app.safe_next("https://evil.example/") == ""
    assert app.safe_next("/\\evil.example") == ""


def test_jellyfin_is_asked_about_a_token_once_a_minute(monkeypatch):
    calls = []

    def call(path, token=None, body=None, device="media-web"):
        calls.append(path)
        if path == "/Users/Me":
            return {"Id": "u1", "Name": "adult", "Policy": {"IsAdministrator": False}}
        return {"Items": [{"Name": "Порно"}, {"Name": "Еротика"}]}

    monkeypatch.setattr(app, "call", call)
    app.cache.clear()
    assert app.access("t1") == ADULT
    assert app.access("t1") == ADULT
    assert calls == ["/Users/Me", "/UserViews?userId=u1"]
