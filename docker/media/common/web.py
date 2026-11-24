"""The status page's routes both services share: the page, its common style and script, and the summary the top
bar of either page reads (through media-web, where both are under one address)."""

import os

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

HERE = os.path.dirname(os.path.abspath(__file__))
# Revalidated on every load, so a redeploy shows up without a hard reload
NO_CACHE = {"Cache-Control": "no-cache"}


def add_page(app: FastAPI, page, summary):
    """page: the service's index.html; summary: () -> {"waiting": how many things wait for the user}."""

    @app.get("/")
    def index():
        return FileResponse(page, headers=NO_CACHE)

    @app.get("/common/page.css")
    def page_css():
        return FileResponse(os.path.join(HERE, "page.css"), headers=NO_CACHE)

    @app.get("/common/page.js")
    def page_js():
        return FileResponse(os.path.join(HERE, "page.js"), headers=NO_CACHE)

    @app.get("/api/summary")
    def api_summary():
        return JSONResponse(summary())
