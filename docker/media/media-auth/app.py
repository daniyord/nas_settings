"""media-auth: who may open the status pages under media-web (docs/SETUP_docker.md). The sign-in is Jellyfin's: the
name and password are checked there and its access token is kept in a cookie, so a password changed or a device
signed out in Jellyfin ends it too. nginx asks /check before every request: Jellyfin's administrators may do
everything; a user who sees the library ADULT_LIBRARY may look at /adult/ without changing anything; anyone else
nothing."""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

URL = os.environ["JELLYFIN_URL"].rstrip("/")
ADULT_LIBRARY = os.environ.get("ADULT_LIBRARY", "Порно")
PORT = int(os.environ.get("PORT", "8110"))
HERE = os.path.dirname(os.path.abspath(__file__))
COOKIE = "media_session"
MAX_AGE = 30 * 86400
# The pages poll every few seconds: Jellyfin is asked again about a token only this often
CACHE_FOR = 60
cache: dict[str, tuple[float, dict | None]] = {}


def authorization(token=None, device="media-web"):
    fields = {"Client": "media-web", "Device": "Status pages", "DeviceId": device, "Version": "1"}
    if token:
        fields["Token"] = token
    return "MediaBrowser " + ", ".join(f'{k}="{v}"' for k, v in fields.items())


def call(path, token=None, body=None, device="media-web") -> Any:
    request = urllib.request.Request(
        URL + path,
        None if body is None else json.dumps(body).encode(),
        {
            "Authorization": authorization(token, device),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="GET" if body is None else "POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


def user_of(me, token):
    views = call(f"/UserViews?userId={me['Id']}", token)["Items"]
    return {
        "name": me["Name"],
        "admin": bool(me["Policy"]["IsAdministrator"]),
        "adult": any(v.get("Name") == ADULT_LIBRARY for v in views),
    }


def access(token):
    """{"name", "admin", "adult"} of a session's token, or None when Jellyfin doesn't know it (any more)."""
    now = time.time()
    hit = cache.get(token)
    if hit and hit[0] > now:
        return hit[1]
    for stale in [t for t, (until, _) in cache.items() if until <= now]:
        del cache[stale]
    try:
        found = user_of(call("/Users/Me", token), token)
    except urllib.error.HTTPError as e:
        if e.code not in (401, 403):
            raise
        found = None
    cache[token] = (now + CACHE_FOR, found)
    return found


def allowed(user, uri, method):
    if user["admin"]:
        return True
    path = uri.split("?")[0]
    return user["adult"] and (path == "/adult" or path.startswith("/adult/")) and method in ("GET", "HEAD")


def home(user):
    return "/ingest/" if user["admin"] else "/adult/"


def safe_next(target):
    """Only a path on this site, never another one ("//host")."""
    return target if target.startswith("/") and not target.startswith("//") and "\\" not in target else ""


app = FastAPI(title="media-auth")


@app.get("/check")
def check(request: Request):
    token = request.cookies.get(COOKIE)
    user = access(token) if token else None
    if not user:
        return Response(status_code=401)
    uri, method = request.headers.get("X-Original-URI", "/"), request.headers.get("X-Original-Method", "GET")
    return Response(status_code=204 if allowed(user, uri, method) else 403)


@app.get("/me")
def me(request: Request):
    token = request.cookies.get(COOKIE)
    user = access(token) if token else None
    return JSONResponse(user) if user else JSONResponse({"detail": "не си влязъл"}, 401)


@app.get("/home")
def go_home(request: Request):
    token = request.cookies.get(COOKIE)
    user = access(token) if token else None
    return RedirectResponse(home(user) if user else "/auth/login", 302)


@app.get("/login")
def login_page():
    return FileResponse(os.path.join(HERE, "login.html"), headers={"Cache-Control": "no-cache"})


@app.get("/common/page.css")
def page_css():
    return FileResponse(os.path.join(HERE, "page.css"), headers={"Cache-Control": "no-cache"})


def back_to_login(error, target):
    return RedirectResponse("/auth/login?" + urllib.parse.urlencode({"error": error, "next": target}), 303)


@app.post("/login")
async def login(request: Request):
    form = urllib.parse.parse_qs((await request.body()).decode())
    name, password = form.get("username", [""])[0].strip(), form.get("password", [""])[0]
    target = safe_next(form.get("next", [""])[0])
    try:
        # A device of its own per sign-in, so one doesn't sign out another (Jellyfin keeps one token per device)
        signed = call(
            "/Users/AuthenticateByName", body={"Username": name, "Pw": password}, device=f"media-web-{uuid.uuid4()}"
        )
    except urllib.error.HTTPError as e:
        if e.code not in (400, 401):
            raise
        return back_to_login("wrong", target)
    token = signed["AccessToken"]
    user = user_of(signed["User"], token)
    if not user["admin"] and not user["adult"]:
        call("/Sessions/Logout", token, body={})
        return back_to_login("none", target)
    cache[token] = (time.time() + CACHE_FOR, user)
    response = RedirectResponse(target if target and allowed(user, target, "GET") else home(user), 303)
    response.set_cookie(COOKIE, token, max_age=MAX_AGE, httponly=True, samesite="lax", path="/")
    return response


@app.post("/logout")
def logout(request: Request):
    token = request.cookies.get(COOKIE)
    if token:
        cache.pop(token, None)
        try:
            call("/Sessions/Logout", token, body={})
        except urllib.error.URLError:
            pass
    response = RedirectResponse("/auth/login", 303)
    response.delete_cookie(COOKIE, path="/")
    return response


@app.api_route("/forbidden", methods=["GET", "HEAD", "POST", "PUT", "DELETE"])
def forbidden(request: Request):
    """nginx's page for a 403: a JSON answer for the pages' api calls, a short page otherwise."""
    if "/api/" in request.headers.get("X-Original-URI", request.url.path) or request.method != "GET":
        return JSONResponse({"detail": "нямаш права за това"}, 403)
    return HTMLResponse(
        '<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/auth/common/page.css">'
        '<main><h1>Нямаш достъп</h1><p class="sub"><a href="/auth/home">Към твоите страници</a> · '
        '<form method="post" action="/auth/logout" style="display:inline"><button>Изход</button></form></p></main>',
        403,
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
