"""
APIx — PSD Airfare Price Index Construction
================================================

Turns the daily per-route average fares in fare_observations into a single
daily index number, the way a statistical office builds a weighted
price-relative index over a fixed basket:

  1. Each route gets its own BASE DATE — the earliest date that route has
     data for — and every later day's average fare on that route is
     expressed as a ratio to its own base-date fare (a "price relative").
     Anchoring per-route rather than to one shared calendar date means a
     route added to the basket later still gets a valid base instead of
     being excluded until history catches up, and it means routes with
     very different absolute fares (a short hop vs. a long one) contribute
     comparably — the index isn't dominated by whichever route happens to
     cost the most in rupees.
  2. Each route's price relative is weighted by its configured weight (see
     app/index/weights.py), renormalized across whichever routes actually
     have data on that day — see normalized_weights()'s own docstring for
     why that matters.
  3. INDEX(date) = 100 * sum(weight_r * price_relative_r) — exactly 100 on
     a route's own base date by construction, moving proportionally to the
     weighted-average fare change since then.

Computed per (date, lead_window) plus one blended "overall" row per date
(equal-weighted across whichever AP windows have a value that date), and
persisted to fare_index_daily (app/db/models.py) rather than recomputed on
every API request. recompute_all() is idempotent — safe to call after
every daily load, and safe to re-run standalone at any time (e.g. after
editing weights.py) since every write is an upsert keyed on
(index_date, lead_window).
"""

from __future__ import annotations

import datetime
import logging
from decimal import Decimal

from sqlalchemy import Date, cast, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import FareIndexDaily, FareObservation
from app.index.weights import normalized_weights

logger = logging.getLogger("apix.index.psd")

BASE_INDEX_VALUE = Decimal("100")
QUANT = Decimal("0.0001")

# Sentinel lead_window value for the one blended "overall" row per date —
# a real string, deliberately not SQL NULL. See FareIndexDaily's docstring
# (app/db/models.py) for why: Postgres's ON CONFLICT never matches two
# NULLs as equal, so a nullable lead_window would make every recompute
# insert a fresh overall row instead of updating the existing one.
OVERALL_LABEL = "OVERALL"


def _daily_avg_fares(
    session: Session, lead_window: str | None = None,
) -> dict[tuple[datetime.date, str, str], Decimal]:
    """(date, route_id, lead_window) -> avg total_fare across every loaded
    observation for that cell — the same aggregation
    GET /fares/daily-summary exposes, queried directly here so a batch
    recompute doesn't need an HTTP round-trip through its own API."""
    fare_date = cast(FareObservation.scraped_at, Date).label("fare_date")
    stmt = (
        select(
            fare_date,
            FareObservation.route_id,
            FareObservation.lead_window,
            func.avg(FareObservation.total_fare).label("avg_total_fare"),
        )
        .where(FareObservation.total_fare.is_not(None))
        .group_by(fare_date, FareObservation.route_id, FareObservation.lead_window)
    )
    if lead_window is not None:
        stmt = stmt.where(FareObservation.lead_window == lead_window)
    return {
        (row.fare_date, row.route_id, row.lead_window): row.avg_total_fare
        for row in session.execute(stmt)
    }


def _base_fares(
    daily: dict[tuple[datetime.date, str, str], Decimal], lead_window: str,
) -> dict[str, tuple[datetime.date, Decimal]]:
    """Each route's own earliest (date, avg_fare) for one AP window — the
    anchor its price relative is computed against on every later date."""
    bases: dict[str, tuple[datetime.date, Decimal]] = {}
    for (fdate, route_id, window), avg_fare in daily.items():
        if window != lead_window:
            continue
        current = bases.get(route_id)
        if current is None or fdate < current[0]:
            bases[route_id] = (fdate, avg_fare)
    return bases


def compute_window_index_series(
    session: Session, lead_window: str, weights: dict[str, float] | None = None,
) -> list[dict]:
    """Daily index rows for one AP window across every date that has data.
    A pure read + compute — nothing here writes to the database (see
    persist_index_rows for that)."""
    daily = _daily_avg_fares(session, lead_window)
    bases = _base_fares(daily, lead_window)
    if not bases:
        return []

    by_date: dict[datetime.date, dict[str, Decimal]] = {}
    for (fdate, route_id, window), avg_fare in daily.items():
        if window != lead_window or route_id not in bases:
            continue
        by_date.setdefault(fdate, {})[route_id] = avg_fare

    rows: list[dict] = []
    for fdate in sorted(by_date):
        day_fares = by_date[fdate]
        norm_weights = normalized_weights(list(day_fares.keys()), weights)
        if not norm_weights:
            continue
        weighted_sum = sum(
            (Decimal(str(norm_weights[route_id])) * (day_fares[route_id] / bases[route_id][1]))
            for route_id in day_fares
            if route_id in norm_weights
        )
        index_value = (weighted_sum * BASE_INDEX_VALUE).quantize(QUANT)
        rows.append(
            {
                "index_date": fdate,
                "lead_window": lead_window,
                "index_value": index_value,
                "base_date": min(base_date for base_date, _ in bases.values()),
                "route_count": len(day_fares),
            }
        )
    return rows


def compute_overall_index_series(window_series_by_window: dict[str, list[dict]]) -> list[dict]:
    """One blended row per date, equal-weighted across whichever AP
    windows have a value on that date — lead_window=OVERALL_LABEL marks it
    as the blended row rather than a per-window one (see FareIndexDaily's
    docstring for why this is a string sentinel, not NULL)."""
    by_date: dict[datetime.date, list[dict]] = {}
    for rows in window_series_by_window.values():
        for row in rows:
            by_date.setdefault(row["index_date"], []).append(row)

    overall: list[dict] = []
    for fdate in sorted(by_date):
        day_rows = by_date[fdate]
        avg_value = (sum(r["index_value"] for r in day_rows) / len(day_rows)).quantize(QUANT)
        overall.append(
            {
                "index_date": fdate,
                "lead_window": OVERALL_LABEL,
                "index_value": avg_value,
                "base_date": min(r["base_date"] for r in day_rows),
                "route_count": sum(r["route_count"] for r in day_rows),
            }
        )
    return overall


def persist_index_rows(session: Session, rows: list[dict]) -> int:
    """Idempotent upsert into fare_index_daily, keyed on
    (index_date, lead_window) — recomputing after new data lands just
    overwrites that date's value rather than duplicating rows."""
    if not rows:
        return 0
    stmt = pg_insert(FareIndexDaily).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_fare_index_daily_date_window",
        set_={
            "index_value": stmt.excluded.index_value,
            "base_date": stmt.excluded.base_date,
            "route_count": stmt.excluded.route_count,
        },
    )
    session.execute(stmt)
    return len(rows)


def recompute_all(
    session: Session,
    weights: dict[str, float] | None = None,
    ap_windows: list[str] | None = None,
    commit: bool = True,
) -> int:
    """Recompute and persist the full index series: one row per
    (date, lead_window) for every AP window with data, plus the blended
    overall row per date. Called after every daily load
    (run_daily_scrape.py) and safe to re-run standalone at any time."""
    from app.ingestion.scheduler import AP_WINDOWS_DAYS

    windows = ap_windows if ap_windows is not None else list(AP_WINDOWS_DAYS.keys())
    series_by_window = {w: compute_window_index_series(session, w, weights) for w in windows}
    overall_rows = compute_overall_index_series(series_by_window)

    total = 0
    for rows in series_by_window.values():
        total += persist_index_rows(session, rows)
    total += persist_index_rows(session, overall_rows)

    if commit:
        session.commit()
    logger.info("psd_index: recomputed and persisted %d index row(s)", total)
    return total
