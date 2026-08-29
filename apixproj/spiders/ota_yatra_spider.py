"""
APIx — Yatra OTA Fare Spider
===============================

Lightweight (no full browser) OTA scraper for Yatra's own fare-search JSON
endpoint. Consumes SearchTasks from the centralized route x date-matrix
scheduler (app/ingestion/scheduler.py) and extracts fields directly from
the JSON network payload the endpoint returns — never from rendered
HTML/CSS selectors, which break the moment a source restyles a page.

Two requests per task:
  1. GET the human-facing search-results URL for (origin, destination,
     departure_date). Yatra's own frontend JS then issues its own XHR to
     the price endpoint below, carrying a fresh session-scoped searchId
     that this response embeds inline.
  2. GET the price endpoint with that searchId; its JSON body is parsed
     directly by `extract_yatra_fare_items` (the shape below matches the
     real payload structure already reverse-engineered in
     apixproj/spiders/ota_demo_spider.py's `_extract_api_flights`).

Gated by the Compliance Gateway before either request goes out (fail-closed
— see compliance.py). Yatra is `PENDING_LEGAL_REVIEW` in SOURCE_REGISTRY
today, so this spider schedules ZERO requests unless
APIX_ALLOW_OTA_SCRAPING=1 is set for a local research run — the same
opt-in the scheduler and every other OTA source already use. EaseMyTrip and
Cleartrip are `PROHIBITED` in that same registry (robots.txt explicitly
disallows their fare-search paths) and are deliberately not targeted by any
spider in this project.

IMPORTANT — what's real vs illustrative here (see the project convention
already established in air_india_spider.py):
  * Compliance gating, task consumption, header construction, and the JSON
    field-mapping in `extract_yatra_fare_items` are the real, load-bearing
    implementation.
  * `_extract_search_id` assumes the searchId is present verbatim somewhere
    in the search-results page's initial HTML / inline-script payload (a
    common OTA bootstrapping pattern) and extracts it with a plain regex —
    deliberately NOT a browser/JS execution step, to keep this leg
    lightweight per the ingestion spec. This has NOT been confirmed against
    a live Yatra response and needs verifying with a real captured session
    (DevTools Network tab) before this is enabled against the live site. If
    Yatra's bootstrap turns out to require JS execution to mint a searchId,
    this OTA leg cannot stay browser-free and should fall back to the
    Playwright approach used for airline-direct sources instead (see
    air_india_stealth_spider.py).
"""

from __future__ import annotations

import json
import logging
import random
import re
from typing import Iterable

import scrapy
from scrapy.http import Response

from apixproj.raw_fare_item import build_raw_fare_item
from app.ingestion.scheduler import RouteDateMatrixScheduler, SearchTask
from compliance import USER_AGENT_POOL, RobotsComplianceGateway

logger = logging.getLogger("apix.ota.yatra")

SOURCE_NAME = "yatra"
SOURCE_TYPE = "ota"

SEARCH_RESULTS_URL_TEMPLATE = (
    "https://www.yatra.com/air-flights/search/{origin}-{destination}/{departure_date}/1/0/0/E/0"
)

# TODO(confirm-before-enabling): the exact price-endpoint query shape beyond
# the searchId/msid pair needs reconfirming against a live capture — see
# apixproj/spiders/ota_demo_spider.py's OTA_TARGET_URL for one previously
# captured (and by-now-expired) example of the fuller param set.
PRICE_ENDPOINT_TEMPLATE = (
    "https://flight.yatra.com/air-service/dom2/price"
    "?searchId={search_id}&msid={search_id}&mode=Background&bpc=true&isSR=false"
)

# TODO(confirm-before-enabling): not confirmed against a live page capture.
SEARCH_ID_PATTERN = re.compile(r'"searchId"\s*:\s*"([a-zA-Z0-9-]+)"')


def build_realistic_headers(*, referer: str, origin_header: str | None = None) -> dict:
    """Realistic desktop-Chrome header set for a plain HTTP request.

    Sec-Ch-Ua is only sent for a Chromium-family User-Agent, matching real
    browser behaviour (Firefox never sends Client Hints at all) — a request
    claiming to be one browser family in User-Agent while sending another
    family's Client Hints is exactly the kind of internally-inconsistent
    header set that's trivially fingerprinted, so the two are derived
    together rather than set independently.
    """
    user_agent = random.choice(USER_AGENT_POOL)
    is_chromium_family = "Chrome" in user_agent or "Edg" in user_agent

    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": referer,
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
    }
    if is_chromium_family:
        is_edge = "Edg" in user_agent
        brand = "Microsoft Edge" if is_edge else "Google Chrome"
        headers["Sec-Ch-Ua"] = f'"Chromium";v="125", "{brand}";v="125", "Not.A/Brand";v="24"'
        headers["Sec-Ch-Ua-Mobile"] = "?0"
        headers["Sec-Ch-Ua-Platform"] = '"Windows"'
    if origin_header:
        headers["Origin"] = origin_header
        headers["X-Requested-With"] = "XMLHttpRequest"
    return headers


def _extract_search_id(html_text: str) -> str | None:
    match = SEARCH_ID_PATTERN.search(html_text)
    return match.group(1) if match else None


def _to_float(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace(",", "").replace("₹", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def extract_yatra_fare_items(payload: dict, task: SearchTask) -> list[dict]:
    """Turn one Yatra price-endpoint JSON payload into raw fare item dicts.

    Shape lifted from the real payload structure already captured in
    ota_demo_spider.py's `_extract_api_flights` (fltSchedule / fareDetails /
    airlineNames) — parses the payload dict directly, no HTML/CSS involved.
    """
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return []

    schedule = data.get("fltSchedule") or {}
    fare_details = data.get("fareDetails") or {}
    airline_names = data.get("airlineNames") or {}

    items: list[dict] = []
    for route_key, route_items in schedule.items():
        for route_item in route_items or []:
            route_id = route_item.get("ID") or route_key
            for od in route_item.get("OD") or []:
                for segment in od.get("FS") or []:
                    flight_code = segment.get("fid") or route_id
                    route_fares = fare_details.get(route_key, {})
                    route_main = route_fares.get(flight_code, {}) if isinstance(route_fares, dict) else {}
                    fare_obj = route_main.get("O", {}).get("ADT", {}) if isinstance(route_main, dict) else {}

                    carrier_code = segment.get("ac") or segment.get("vac") or "UNKNOWN"
                    base_fare = _to_float(fare_obj.get("bf"))
                    taxes = _to_float(fare_obj.get("tf"))
                    total_fare = _to_float(fare_obj.get("ftf"))
                    if total_fare is None and base_fare is not None and taxes is not None:
                        total_fare = base_fare + taxes
                    seats_left_raw = segment.get("seatsLeft") or route_item.get("seatsLeft")

                    items.append(
                        build_raw_fare_item(
                            flight_number=segment.get("fnum") or flight_code,
                            airline_name=airline_names.get(carrier_code, segment.get("acn") or carrier_code),
                            carrier_code=carrier_code,
                            origin=task.origin,
                            destination=task.destination,
                            departure_time=segment.get("dd") or od.get("tdu"),
                            arrival_time=segment.get("ad"),
                            base_fare=base_fare,
                            taxes_and_fees=taxes,
                            total_fare=total_fare,
                            seats_left=int(seats_left_raw) if seats_left_raw not in (None, "") else None,
                            lead_window=task.advance_purchase_window,
                            source_name=SOURCE_NAME,
                            source_type=SOURCE_TYPE,
                        )
                    )
    return items


class YatraFareSpider(scrapy.Spider):
    """
    Construct with a list of SearchTask (from RouteDateMatrixScheduler,
    app/ingestion/scheduler.py); defaults to building today's full route x
    AP-window matrix if none is given.

        scrapy crawl yatra_fare
    """

    name = "yatra_fare"
    source_name = SOURCE_NAME
    allowed_domains = ["www.yatra.com", "flight.yatra.com"]
    custom_settings = {
        "ROBOTSTXT_OBEY": True,
        "AUTOTHROTTLE_ENABLED": True,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
    }

    def __init__(self, tasks: Iterable[SearchTask] | None = None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tasks = list(tasks) if tasks is not None else RouteDateMatrixScheduler().build()
        self.gateway = RobotsComplianceGateway()

    def start_requests(self):
        for task in self.tasks:
            url = SEARCH_RESULTS_URL_TEMPLATE.format(
                origin=task.origin, destination=task.destination,
                departure_date=task.departure_date.isoformat(),
            )
            decision = self.gateway.check(self.source_name, url)
            if not decision.allowed:
                logger.warning("yatra: task %s BLOCKED at dispatch: %s", task.task_id, decision.reason)
                if getattr(self, "crawler", None) is not None:
                    self.crawler.stats.inc_value("compliance/blocked_at_dispatch")
                continue

            yield scrapy.Request(
                url,
                headers=build_realistic_headers(referer="https://www.yatra.com/"),
                callback=self.parse_search_results,
                errback=self.handle_error,
                meta={"task": task},
                dont_filter=True,
            )

    def parse_search_results(self, response: Response):
        task: SearchTask = response.meta["task"]
        search_id = _extract_search_id(response.text)
        if not search_id:
            logger.warning(
                "yatra: task %s — could not extract searchId from the search-results page; "
                "SEARCH_ID_PATTERN likely needs updating against a live capture", task.task_id,
            )
            self.crawler.stats.inc_value("extraction/search_id_not_found")
            return

        price_url = PRICE_ENDPOINT_TEMPLATE.format(search_id=search_id)
        decision = self.gateway.check(self.source_name, price_url)
        if not decision.allowed:
            logger.warning("yatra: task %s price endpoint BLOCKED at dispatch: %s", task.task_id, decision.reason)
            self.crawler.stats.inc_value("compliance/blocked_at_dispatch")
            return

        yield scrapy.Request(
            price_url,
            headers=build_realistic_headers(
                referer=response.url, origin_header="https://flight.yatra.com",
            ),
            callback=self.parse_price_payload,
            errback=self.handle_error,
            meta={"task": task},
            dont_filter=True,
        )

    def parse_price_payload(self, response: Response):
        task: SearchTask = response.meta["task"]
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError:
            logger.warning("yatra: task %s — price endpoint returned a non-JSON body", task.task_id)
            self.crawler.stats.inc_value("extraction/non_json_payload")
            return

        items = extract_yatra_fare_items(payload, task)
        if not items:
            logger.warning("yatra: task %s — zero fares extracted from payload", task.task_id)
            self.crawler.stats.inc_value("extraction/zero_items")
            return

        for item in items:
            yield item

    def handle_error(self, failure):
        task = failure.request.meta.get("task")
        logger.error("yatra: task %s failed: %s", getattr(task, "task_id", "?"), failure.value)
        if getattr(self, "crawler", None) is not None:
            self.crawler.stats.inc_value("requests/failed")
