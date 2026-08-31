"""
APIx — FastAPI Read Layer
============================

Serves the Postgres-backed fare data (loaded by app/db/loader.py) to the
dashboard frontend. Read-only: nothing under app/api/ ever writes to the
database — ingestion stays entirely on the run_daily_scrape.py path.

Run locally with:

    uvicorn app.api.main:app --reload

Interactive docs then live at http://127.0.0.1:8000/docs.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routers import fares

app = FastAPI(
    title="APIx Fare Data API",
    description="Read-only API over scraped airfare data, for the Airfare Price Index dashboard.",
    version="0.1.0",
)

# Wide open for local development — the dashboard frontend's origin isn't
# fixed yet. Tighten to an explicit allow-list once that origin is known.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

app.include_router(fares.router, tags=["fares"])


@app.get("/health", tags=["health"])
def health() -> dict:
    return {"status": "ok"}
