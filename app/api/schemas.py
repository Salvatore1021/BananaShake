"""Pydantic response models for the FastAPI read layer.

One model per shape the API actually returns — mirrors app/db/models.py
but stays a separate layer deliberately: these are the wire contract for
the dashboard frontend (and, later, the index-construction module), not
the ORM's own attribute set, so the two are free to diverge as either
side's needs change.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class CarrierOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    carrier_code: str
    airline_name: str


class RouteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    route_id: str
    origin: str
    destination: str


class FareObservationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    route_id: str
    carrier_code: str
    flight_number: str | None
    departure_time: datetime.datetime | None
    arrival_time: datetime.datetime | None
    base_fare: Decimal | None
    taxes_and_fees: Decimal | None
    total_fare: Decimal | None
    seats_left: int | None
    lead_window: str
    source_name: str
    source_type: str
    scraped_at: datetime.datetime


class DailyFareSummaryOut(BaseModel):
    """One route's average/min/max total_fare for one calendar day —
    the primitive aggregation a fare-index chart or the future PSD
    index-construction module builds on."""

    fare_date: datetime.date
    route_id: str
    lead_window: str
    sample_count: int
    avg_total_fare: Decimal
    min_total_fare: Decimal
    max_total_fare: Decimal
