"""Read endpoints over the precomputed Airfare Price Index
(fare_index_daily, written by app/index/psd_index.py) and the daily-scrape
audit trail (scrape_runs, written by run_daily_scrape.py) — what the
dashboard's index chart and pipeline-health panel call.
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.schemas import FareIndexDailyOut, ScrapeRunOut
from app.db.models import FareIndexDaily, ScrapeRun
from app.index.psd_index import ACTIVE_INDEX_METHOD, OVERALL_LABEL

router = APIRouter()

DEFAULT_PAGE_SIZE = 500
MAX_PAGE_SIZE = 2000


@router.get("/index/daily", response_model=list[FareIndexDailyOut])
def daily_index(
    db: Session = Depends(get_db),
    lead_window: str | None = Query(
        None, description=f"e.g. T+7, or '{OVERALL_LABEL}' for the blended row; omit for every row",
    ),
    overall_only: bool = Query(False, description="return only the blended overall row per date"),
    date_from: datetime.date | None = Query(None, description="index_date lower bound, inclusive"),
    date_to: datetime.date | None = Query(None, description="index_date upper bound, inclusive"),
    method: str | None = Query(
        None,
        description=(
            "Which index-construction formula to read (see app.index.psd_index's module "
            f"docstring for all of ALL_METHODS -- carli_arithmetic_v1, jevons_geometric_v2, "
            "tornqvist_bilateral_v1, geks_multilateral_v1). Omit for the currently-active method "
            f"('{ACTIVE_INDEX_METHOD}'); pass another method's name explicitly to read it instead -- "
            "every method's rows are kept side by side, never overwritten, so this always has "
            "something real to return (the dashboard's multi-line chart calls this once per method)."
        ),
    ),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
) -> list[FareIndexDaily]:
    """The daily Airfare Price Index series, oldest first — what a
    dashboard's index line chart plots directly."""
    stmt = select(FareIndexDaily).where(FareIndexDaily.method == (method or ACTIVE_INDEX_METHOD))
    if overall_only:
        stmt = stmt.where(FareIndexDaily.lead_window == OVERALL_LABEL)
    elif lead_window is not None:
        stmt = stmt.where(FareIndexDaily.lead_window == lead_window)
    if date_from is not None:
        stmt = stmt.where(FareIndexDaily.index_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(FareIndexDaily.index_date <= date_to)

    stmt = stmt.order_by(FareIndexDaily.index_date.asc()).limit(limit)
    return list(db.scalars(stmt))


@router.get("/scrape-runs", response_model=list[ScrapeRunOut])
def list_scrape_runs(
    db: Session = Depends(get_db),
    limit: int = Query(30, ge=1, le=365),
) -> list[ScrapeRun]:
    """Most recent daily-scrape batch runs, newest first — the pipeline
    health panel's data source (last run status, coverage, errors)."""
    stmt = select(ScrapeRun).order_by(ScrapeRun.started_at.desc()).limit(limit)
    return list(db.scalars(stmt))
