"""
APIx — Canonical Raw Fare Item Shape
=======================================

Every scraper worker in this project (currently
apixproj/spiders/akasa_air_spider.py) yields items in this exact flat
shape, regardless of how different the underlying source's JSON payload
looks. Keeping the shape defined in one place means a future second
source can't quietly drift apart on field names, and any downstream
consumer (run_daily_scrape.py's JSON/CSV export, a future database loader)
only has to know one schema.

Deliberately source-agnostic and flat — no foreign keys, no persistence
concerns — because this is what a spider itself can produce with no
database access at all. A future Postgres loader maps this shape onto
whatever normalized schema the database uses, resolving carrier/route
dimension tables at load time rather than scrape time.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone

RAW_FARE_ITEM_FIELDS = (
    "flight_number",
    "airline_name",
    "carrier_code",
    "origin",
    "destination",
    "departure_time",
    "arrival_time",
    "base_fare",
    "taxes_and_fees",
    "total_fare",
    "seats_left",
    "fare_class",
    "scraped_at_timestamp",
    "lead_window",
    "source_name",
    "source_type",
    "route_id",
)


def build_raw_fare_item(
    *,
    flight_number: str | None,
    airline_name: str | None,
    carrier_code: str | None,
    origin: str,
    destination: str,
    departure_time: str | None,
    arrival_time: str | None,
    base_fare: float | None,
    taxes_and_fees: float | None,
    total_fare: float | None,
    seats_left: int | None,
    lead_window: str,
    source_name: str,
    source_type: str,
    fare_class: str | None = None,
    scraped_at: datetime | None = None,
) -> dict:
    """Build one raw fare item dict in the canonical shape.

    `fare_class` is the source's own fare-bucket/product-class code for
    this quote (e.g. Akasa's "EC" — see akasa_air_spider.py's
    _parse_journey), passed through as-is rather than decoded into a
    human label, since no source in this project documents what its codes
    mean beyond what's directly observable in a captured response.
    Optional (defaults to None) because not every source is guaranteed to
    expose it.

    `scraped_at` is injectable (defaults to `datetime.now(timezone.utc)`)
    purely so callers can get a deterministic `scraped_at_timestamp` in
    tests without monkeypatching the clock.
    """
    timestamp = scraped_at if scraped_at is not None else datetime.now(timezone.utc)
    return {
        "flight_number": flight_number,
        "airline_name": airline_name,
        "carrier_code": carrier_code,
        "origin": origin,
        "destination": destination,
        "departure_time": departure_time,
        "arrival_time": arrival_time,
        "base_fare": base_fare,
        "taxes_and_fees": taxes_and_fees,
        "total_fare": total_fare,
        "seats_left": seats_left,
        "fare_class": fare_class,
        "scraped_at_timestamp": timestamp.isoformat(),
        "lead_window": lead_window,
        "source_name": source_name,
        "source_type": source_type,
        "route_id": f"{origin}-{destination}",
    }


def write_raw_fare_items_csv(items: list[dict], path: str) -> None:
    """Write raw fare items to `path` as CSV, one row per item, columns in
    RAW_FARE_ITEM_FIELDS order regardless of key order in the dicts —
    the one canonical layout every run produces, so files from different
    runs/sources line up column-for-column. A field absent from a given
    item (shouldn't happen for anything built via build_raw_fare_item, but
    csv.DictWriter would otherwise raise on a genuinely malformed dict)
    is written as an empty cell rather than erroring the whole export.
    """
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RAW_FARE_ITEM_FIELDS, restval="", extrasaction="ignore")
        writer.writeheader()
        for item in items:
            writer.writerow(item)
