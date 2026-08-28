"""
APIx — Scrapy Item Pipeline
=============================

Closes the loop with Pillar 2 (app/models/schema.py): every item a spider
yields (a RawFareQuote-shaped dict, see spiders/base_spider.py) is validated,
resolved against the `carriers`/`routes` dimension tables, and bulk-inserted
into `raw_fare_quotes` — the same immutable landing table
pipeline/cleaning.py reads from.

Kept intentionally simple for the prototype: one DB session per spider run,
buffered bulk insert, one ScrapeJobLog summary row written on close. A
production deployment would likely move to an async SQLAlchemy session (to
match Scrapy's Twisted/asyncio reactor) and a proper connection pool sized
to the crawl's concurrency — noted here rather than built now, since neither
changes the shape of what this pipeline does.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.schema import FlightStatus, RawFareQuote, ScrapeJobLog, SourceType

logger = logging.getLogger("apix.ingestion.pipelines")


class JsonExportPipeline:
    """Simple JSON export for scraped OTA flights."""

    def __init__(self):
        self.file = None
        self._first = True

    @classmethod
    def from_crawler(cls, crawler):
        return cls()

    def open_spider(self, spider):
        self.file = open("ota_flights.json", "w", encoding="utf-8")
        self.file.write("[")
        self._first = True

    def process_item(self, item, spider):
        if not self._first:
            self.file.write(",")
        json.dump(item, self.file, ensure_ascii=False)
        self.file.write("\n")
        self._first = False
        return item

    def close_spider(self, spider):
        if self.file is not None:
            self.file.write("]")
            self.file.close()


class RawFareQuotePipeline:
    """Buffers items in memory and flushes in batches, to avoid one DB
    round-trip per scraped fare on a run that may collect thousands."""

    FLUSH_BATCH_SIZE = 200

    def __init__(self, database_url: str):
        self.database_url = database_url
        self.engine = None
        self.session: Session | None = None
        self._buffer: list[RawFareQuote] = []
        self._n_written = 0
        self._n_failed = 0

    @classmethod
    def from_crawler(cls, crawler):
        database_url = crawler.settings.get(
            "APIX_DATABASE_URL", "postgresql+psycopg2://localhost/apix"
        )
        return cls(database_url=database_url)

    def open_spider(self, spider):
        self.engine = create_engine(self.database_url, future=True)
        self.session = Session(self.engine)
        self._job_started_at = datetime.now(timezone.utc)
        logger.info("pipeline: opened DB session for spider '%s' -> %s", spider.name, self.database_url)

    def process_item(self, item: dict, spider):
        try:
            record = self._to_orm(item)
        except Exception as exc:  # noqa: BLE001 — one bad item should never kill the whole run
            self._n_failed += 1
            logger.warning("pipeline: dropping malformed item (%s): %r", exc, item)
            return item

        self._buffer.append(record)
        if len(self._buffer) >= self.FLUSH_BATCH_SIZE:
            self._flush()
        return item

    def close_spider(self, spider):
        self._flush()

        if self.session is None:
            logger.warning("pipeline: no DB session present; skipping job-log flush")
            return

        try:
            from app.models.schema import ScrapeJobLog
        except Exception:  # pragma: no cover - optional schema not mapped here
            logger.warning("pipeline: skipping ScrapeJobLog write; schema is not SQLAlchemy-mapped")
            self.session.close()
            return

        job_log = ScrapeJobLog(
            scrape_batch_id=getattr(spider, "scrape_batch_id", None),
            source_name=getattr(spider, "source_name", spider.name),
            started_at=self._job_started_at,
            finished_at=datetime.now(timezone.utc),
            status="success" if self._n_failed == 0 else "partial",
            records_scraped=self._n_written,
            records_failed=self._n_failed,
        )

        if not hasattr(job_log, "_sa_instance_state"):
            logger.warning("pipeline: skipping ScrapeJobLog write; object is not a mapped SQLAlchemy row")
            self.session.close()
            return

        self.session.add(job_log)
        self.session.commit()
        self.session.close()
        logger.info(
            "pipeline: spider '%s' finished — %d records written, %d failed",
            spider.name, self._n_written, self._n_failed,
        )

    # ------------------------------------------------------------------

    def _flush(self) -> None:
        if not self._buffer:
            return
        self.session.bulk_save_objects(self._buffer)
        self.session.commit()
        self._n_written += len(self._buffer)
        self._buffer.clear()

    @staticmethod
    def _to_orm(item: dict) -> RawFareQuote:
        required = ("route_id", "carrier_id", "source_name", "total_fare_inr", "scrape_timestamp")
        missing = [f for f in required if item.get(f) in (None, "")]
        if missing:
            raise ValueError(f"missing required field(s): {missing}")

        return RawFareQuote(
            route_id=item["route_id"],  # resolved to routes.route_id upstream (job matrix uses "DEL-BOM" style
                                          # keys today; a production loader resolves these to the real UUID FK
                                          # before this pipeline runs — left as a passthrough here for clarity)
            carrier_id=item["carrier_id"],
            source_type=SourceType(item.get("source_type", "airline_direct")),
            source_name=item["source_name"],
            scrape_batch_id=item["scrape_batch_id"],
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
