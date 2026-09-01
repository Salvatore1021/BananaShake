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

from fastapi import FastAPI
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

app.include_router(fares.router, tags=["fares"])
app.include_router(index.router, tags=["index"])


@app.get("/health", tags=["health"])
def health() -> dict:
    return {"status": "ok"}


_STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="dashboard-assets")
app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="dashboard")
