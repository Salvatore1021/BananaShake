from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from uuid import uuid4


class SourceType(str, Enum):
    AIRLINE_DIRECT = "airline_direct"
    OTA = "ota"


class FlightStatus(str, Enum):
    AVAILABLE = "available"
    SOLD_OUT = "sold_out"
    CANCELLED = "cancelled"
    NOT_OPERATING = "not_operating"


@dataclass
class RawFareQuote:
    quote_id: str
    route_id: str
    carrier_id: str
    source_type: SourceType
    source_name: str
    scrape_batch_id: str
    flight_number: str | None
    fare_class: str | None
    cabin: str | None
    travel_date: date
    advance_purchase_days: int
    advance_purchase_window: str
    scrape_timestamp: datetime
    total_fare_inr: float
    base_fare_inr: float | None = None
    taxes_fees_inr: float | None = None
    status: FlightStatus = FlightStatus.AVAILABLE
    seats_available_bucket: str | None = None
    raw_payload: dict | None = None

    @classmethod
    def from_item(cls, item: dict):
        return cls(
            quote_id=str(uuid4()),
            route_id=item["route_id"],
            carrier_id=item["carrier_id"],
            source_type=SourceType(item.get("source_type", "airline_direct")),
            source_name=item["source_name"],
            scrape_batch_id=item.get("scrape_batch_id", "demo"),
            flight_number=item.get("flight_number"),
            fare_class=item.get("fare_class"),
            cabin=item.get("cabin", "Economy"),
            travel_date=item["travel_date"],
            advance_purchase_days=int(item["advance_purchase_days"]),
            advance_purchase_window=item["advance_purchase_window"],
            scrape_timestamp=item["scrape_timestamp"],
            total_fare_inr=float(item["total_fare_inr"]),
            base_fare_inr=item.get("base_fare_inr"),
            taxes_fees_inr=item.get("taxes_fees_inr"),
            status=FlightStatus(item.get("status", "available")),
            seats_available_bucket=item.get("seats_available_bucket"),
            raw_payload=item.get("raw_payload"),
        )


@dataclass
class ScrapeJobLog:
    scrape_batch_id: str
    source_name: str
    started_at: datetime
    finished_at: datetime
    status: str
    records_scraped: int
    records_failed: int
