"""
APIx — Raw Fare Item -> Postgres Loader
==========================================

Maps a batch of raw fare items (the flat, source-agnostic shape defined in
apixproj/raw_fare_item.py) onto the normalized schema in app/db/models.py,
resolving the Carrier and Route dimension rows at load time — exactly what
raw_fare_item.py's own docstring anticipates a "future Postgres loader"
doing.

Called by run_daily_scrape.py after every scrape batch. Also safe to call
directly for a one-off load (e.g. backfilling an existing
apix_daily_scrape.json) since every insert here is idempotent:
Carrier/Route rows use ON CONFLICT DO NOTHING keyed on their natural
primary key, and FareObservation rows use ON CONFLICT DO NOTHING keyed on
the uq_fare_observations_identity constraint, so re-loading the same batch
twice never duplicates or errors.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import Carrier, FareObservation, Route
from app.db.session import SessionLocal


def _parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _parse_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _upsert_dimensions(session: Session, items: list[dict]) -> None:
    """Insert any Carrier/Route rows this batch references that don't exist
    yet. ON CONFLICT DO NOTHING makes this safe on every run — existing
    dimension rows are left untouched."""
    carriers = {
        (item["carrier_code"], item.get("airline_name") or item["carrier_code"])
        for item in items
        if item.get("carrier_code")
    }
    if carriers:
        session.execute(
            pg_insert(Carrier)
            .values([{"carrier_code": code, "airline_name": name} for code, name in carriers])
            .on_conflict_do_nothing(index_elements=["carrier_code"])
        )

    routes = {
        (item["route_id"], item["origin"], item["destination"])
        for item in items
        if item.get("route_id") and item.get("origin") and item.get("destination")
    }
    if routes:
        session.execute(
            pg_insert(Route)
            .values([{"route_id": rid, "origin": o, "destination": d} for rid, o, d in routes])
            .on_conflict_do_nothing(index_elements=["route_id"])
        )


def load_raw_fare_items(items: list[dict], session: Session | None = None) -> int:
    """Load a batch of raw fare items into Postgres.

    Returns the number of FareObservation rows actually inserted — rows
    that collide with an already-loaded observation on the identity unique
    constraint are silently skipped (that's the intended re-run-safe
    behaviour, not an error; see FareObservation's docstring).

    Opens and commits its own session when `session` isn't supplied (the
    normal case, from run_daily_scrape.py). Pass an explicit session to
    fold this into a caller's own transaction (e.g. from a test).
    """
    if not items:
        return 0

    owns_session = session is None
    session = session or SessionLocal()
    try:
        _upsert_dimensions(session, items)

        rows = [
            {
                "route_id": item["route_id"],
                "carrier_code": item["carrier_code"],
                "flight_number": item.get("flight_number"),
                "departure_time": _parse_datetime(item.get("departure_time")),
                "arrival_time": _parse_datetime(item.get("arrival_time")),
                "base_fare": _parse_decimal(item.get("base_fare")),
                "taxes_and_fees": _parse_decimal(item.get("taxes_and_fees")),
                "total_fare": _parse_decimal(item.get("total_fare")),
                "seats_left": item.get("seats_left"),
                "fare_class": item.get("fare_class"),
                "lead_window": item["lead_window"],
                "source_name": item["source_name"],
                "source_type": item["source_type"],
                "scraped_at": _parse_datetime(item["scraped_at_timestamp"]),
            }
            for item in items
            if item.get("carrier_code") and item.get("route_id")
        ]

        inserted = 0
        if rows:
            result = session.execute(
                pg_insert(FareObservation)
                .on_conflict_do_nothing(constraint="uq_fare_observations_identity")
                .returning(FareObservation.id),
                rows,
            )
            inserted = len(result.fetchall())

        if owns_session:
            session.commit()
        return inserted
    except Exception:
        if owns_session:
            session.rollback()
        raise
    finally:
        if owns_session:
            session.close()
