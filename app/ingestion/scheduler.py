from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta


@dataclass
class ScrapeJob:
    job_id: str
    route_id: str
    source_name: str
    advance_purchase_window: str
    advance_purchase_days: int
    travel_date: date
    scheduled_time: datetime
    priority: float = 0.0
