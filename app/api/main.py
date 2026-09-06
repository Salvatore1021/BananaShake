"""
APIx — FastAPI Read Layer + Dashboard
==========================================

Serves the Postgres-backed fare data (loaded by app/db/loader.py) to the
dashboard frontend, AND serves that dashboard itself — a static,
build-step-free HTML/CSS/JS page (app/web/static/) — from the same
process. One `uvicorn` command, one URL: http://127.0.0.1:8000/ opens the
dashboard, http://127.0.0.1:8000/docs opens the interactive API reference.

Read-only: nothing under app/api/ ever writes to the database — ingestion
stays entirely on the run_daily_scrape.py path.

Run locally with:

    uvicorn app.api.main:app --reload

The dashboard's own JS fetches /fares, /fares/daily-summary, /index/daily
and /scrape-runs directly (relative, same-origin) — see
app/web/static/app.js. The API routers are registered BEFORE the static
mount below specifically so those exact paths keep resolving to JSON, not
to the catch-all static file server.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.routers import fares, index

app = FastAPI(
    title="APIx Fare Data API",
    description="Read-only API over scraped airfare data, for the Airfare Price Index dashboard.",
    version="0.1.0",
)

# Wide open for local development — the dashboard is same-origin (served by
# this same app, see the static mount below) so it doesn't actually need
# this, but a locally-run separate frontend during development still might.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.middleware("http")
async def no_cache_dashboard(request: Request, call_next):
    """Starlette's StaticFiles sets Last-Modified/ETag but no explicit
    Cache-Control, which leaves a browser free to serve its own disk/memory
    cache on a plain reload without even revalidating -- editing app.js/
    index.html/styles.css during development then looks like "nothing
    changed" until someone remembers to hard-refresh. This project has no
    build step or cache-busted filenames for its static assets (see this
    module's own docstring), so the dashboard and everything under /static
    are told never to cache instead -- a normal reload always re-fetches
    the real file from disk. The JSON API responses are unaffected."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response

app.include_router(fares.router, tags=["fares"])
app.include_router(index.router, tags=["index"])


@app.get("/health", tags=["health"])
def health() -> dict:
    return {"status": "ok"}


_STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="dashboard-assets")
app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="dashboard")
