"""Read endpoints over the fare-observation data Postgres holds.

Every endpoint here is read-only (the loader, not the API, is what writes
rows — see app/db/loader.py) and every filter is optional, so the same
`/fares` and `/fares/daily-summary` endpoints serve both "give me
everything" (dashboard's initial load) and narrow, chart-specific queries
(one route, one AP window, a date range) without needing separate routes
per use case.
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Date, cast, func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.schemas import CarrierOut, DailyFareSummaryOut, FareObservationOut, RouteOut
from app.db.models import Carrier, FareObservation, Route

router = APIRouter()

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 1000


@router.get("/routes", response_model=list[RouteOut])
def list_routes(db: Session = Depends(get_db)) -> list[Route]:
    """Every route with at least one loaded fare observation — populates a
    dashboard's route filter/dropdown."""
    return list(db.scalars(select(Route).order_by(Route.route_id)))


@router.get("/carriers", response_model=list[CarrierOut])
def list_carriers(db: Session = Depends(get_db)) -> list[Carrier]:
    return list(db.scalars(select(Carrier).order_by(Carrier.carrier_code)))


@router.get("/fares", response_model=list[FareObservationOut])
def list_fares(
    db: Session = Depends(get_db),
    route_id: str | None = Query(None, description="e.g. DEL-BOM"),
    carrier_code: str | None = Query(None, description="e.g. QP"),
    lead_window: str | None = Query(None, description="e.g. T+7"),
    fare_class: str | None = Query(None, description="source's own fare-bucket code, e.g. EC"),
    source_name: str | None = None,
    scraped_from: datetime.datetime | None = Query(None, description="scraped_at lower bound, inclusive"),
    scraped_to: datetime.datetime | None = Query(None, description="scraped_at upper bound, inclusive"),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
) -> list[FareObservation]:
    """Paginated fare-observation rows, newest first, narrowed by whichever
    filters are supplied."""
    stmt = select(FareObservation)
    if route_id is not None:
        stmt = stmt.where(FareObservation.route_id == route_id)
    if carrier_code is not None:
        stmt = stmt.where(FareObservation.carrier_code == carrier_code)
    if lead_window is not None:
        stmt = stmt.where(FareObservation.lead_window == lead_window)
    if fare_class is not None:
        stmt = stmt.where(FareObservation.fare_class == fare_class)
    if source_name is not None:
        stmt = stmt.where(FareObservation.source_name == source_name)
    if scraped_from is not None:
        stmt = stmt.where(FareObservation.scraped_at >= scraped_from)
    if scraped_to is not None:
        stmt = stmt.where(FareObservation.scraped_at <= scraped_to)

    stmt = stmt.order_by(FareObservation.scraped_at.desc()).limit(limit).offset(offset)
    return list(db.scalars(stmt))


@router.get("/fares/daily-summary", response_model=list[DailyFareSummaryOut])
def daily_fare_summary(
    db: Session = Depends(get_db),
    route_id: str | None = None,
    carrier_code: str | None = None,
    lead_window: str | None = None,
    scraped_from: datetime.datetime | None = None,
    scraped_to: datetime.datetime | None = None,
) -> list[DailyFareSummaryOut]:
    """Per-(day, route, AP window) avg/min/max total_fare — the aggregation
    a fare-trend chart, or a future PSD-index calculation, is built on top
    of rather than each recomputing it from raw rows."""
    fare_date = cast(FareObservation.scraped_at, Date).label("fare_date")

    stmt = (
        select(
            fare_date,
            FareObservation.route_id,
            FareObservation.lead_window,
            func.count(FareObservation.id).label("sample_count"),
            func.avg(FareObservation.total_fare).label("avg_total_fare"),
            func.min(FareObservation.total_fare).label("min_total_fare"),
            func.max(FareObservation.total_fare).label("max_total_fare"),
        )
        .where(FareObservation.total_fare.is_not(None))
        .group_by(fare_date, FareObservation.route_id, FareObservation.lead_window)
        .order_by(fare_date.desc(), FareObservation.route_id, FareObservation.lead_window)
    )
    if route_id is not None:
        stmt = stmt.where(FareObservation.route_id == route_id)
    if carrier_code is not None:
        stmt = stmt.where(FareObservation.carrier_code == carrier_code)
    if lead_window is not None:
        stmt = stmt.where(FareObservation.lead_window == lead_window)
    if scraped_from is not None:
        stmt = stmt.where(FareObservation.scraped_at >= scraped_from)
    if scraped_to is not None:
        stmt = stmt.where(FareObservation.scraped_at <= scraped_to)

    return [
        DailyFareSummaryOut(
            fare_date=row.fare_date,
            route_id=row.route_id,
            lead_window=row.lead_window,
            sample_count=row.sample_count,
            avg_total_fare=row.avg_total_fare,
            min_total_fare=row.min_total_fare,
            max_total_fare=row.max_total_fare,
        )
        for row in db.execute(stmt)
    ]
