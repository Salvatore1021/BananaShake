"""
APIx — Air India Spider (worked example)
==========================================

Air India is the one source cleared today (see compliance.SOURCE_REGISTRY /
docs/pillar1-ingestion-architecture.md §0) — no blanket robots.txt disallow,
and (pending the actual legal ToS sign-off this registry entry still
requires before it may run for real) the least ambiguous of the ten sources
checked. This spider is the concrete demonstration of the pattern
base_spider.py defines.

IMPORTANT — what's real vs. illustrative here:
  * The Compliance Gateway integration, job handling, interception wiring,
    and item shaping are the real, load-bearing implementation.
  * `SEARCH_URL_TEMPLATE` and `FARE_API_URL_PATTERN` below are illustrative
    placeholders. Air India's exact fare-search URL structure and internal
    API path are not publicly documented and are exactly the kind of detail
    that must be confirmed by actually opening the site in a browser with
    DevTools' Network tab open, performing one manual search, and reading
    off the real endpoint — NOT guessed at or reverse-engineered from
    outside signals. That confirmation step is a prerequisite for enabling
    this spider against the live site, tracked as an explicit TODO rather
    than papered over with a made-up URL presented as fact.
  * `parse_fare_json` likewise assumes a plausible-but-unconfirmed JSON
    shape (a `results` list of fare options per flight). Update it to match
    whatever the real endpoint returns once confirmed.
"""

from __future__ import annotations

import re

from app.ingestion.scheduler import ScrapeJob
from app.ingestion.spiders.base_spider import BaseFareSpider

# TODO(confirm-before-enabling): verified against DevTools Network tab, not guessed.
SEARCH_URL_TEMPLATE = (
    "https://www.airindia.com/in/en/book-flight.html"
    "?tripType=O&from={origin}&to={destination}&date={travel_date}&paxType=ADULT-1"
)

# TODO(confirm-before-enabling): the real internal fare-search XHR/fetch path.
FARE_API_URL_PATTERN = re.compile(r"/api/.*fare-search|/api/.*flight-search", re.IGNORECASE)


class AirIndiaSpider(BaseFareSpider):
    name = "air_india"
    source_name = "air_india"  # must match compliance.SOURCE_REGISTRY key
    fare_api_url_pattern = FARE_API_URL_PATTERN

    def build_search_url(self, job: ScrapeJob) -> str:
        origin, destination = job.route_id.split("-")
        return SEARCH_URL_TEMPLATE.format(
            origin=origin, destination=destination, travel_date=job.travel_date.isoformat(),
        )

    def parse_fare_json(self, payload: dict, job: ScrapeJob) -> list[dict]:
        """Shape the intercepted JSON into RawFareQuote-ready dicts.

        Assumed (unconfirmed — see module docstring) response shape:
            {
              "results": [
                {
                  "flightNumber": "AI101",
                  "cabin": "Economy",
                  "fareClass": "Y",
                  "totalFare": 5820.0,
                  "baseFare": 4400.0,
                  "taxesAndFees": 1420.0,
                  "seatsLeft": 4,
                  "status": "AVAILABLE" | "SOLD_OUT" | "CANCELLED",
                }, ...
              ]
            }
        """
        results = payload.get("results")
        if not isinstance(results, list):
            return []

        status_map = {
            "AVAILABLE": "available",
            "SOLD_OUT": "sold_out",
            "CANCELLED": "cancelled",
            "NOT_OPERATING": "not_operating",
        }

        items = []
        for entry in results:
            try:
                total_fare = float(entry["totalFare"])
            except (KeyError, TypeError, ValueError):
                continue  # not a fare record we can use — skip rather than crash the whole batch

            items.append({
                "carrier_id": "AI",  # resolved to the carriers.carrier_id FK by the item pipeline
                "flight_number": entry.get("flightNumber"),
                "cabin": entry.get("cabin", "Economy"),
                "fare_class": entry.get("fareClass"),
                "total_fare_inr": total_fare,
                "base_fare_inr": entry.get("baseFare"),
                "taxes_fees_inr": entry.get("taxesAndFees"),
                "status": status_map.get(str(entry.get("status", "")).upper(), "available"),
                "seats_available_bucket": (
                    f"{entry['seatsLeft']} seats left" if entry.get("seatsLeft") is not None else None
                ),
                "raw_payload": entry,
            })
        return items
