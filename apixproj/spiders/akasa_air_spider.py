"""
APIx — Akasa Air Spider (direct API, via a Playwright-established session)
==============================================================================

Airline-direct spider for Akasa Air (IATA carrier code QP) — CLEARED in
compliance.SOURCE_REGISTRY (robots.txt is fully open, `User-agent: *` with
zero Disallow rules, re-verified 2026-08-30 superseding an earlier stale
403 finding — see that registry entry's notes).

This is the SECOND design for this spider. The first drove the visible
search form end-to-end (click origin, select from a suggestion panel,
navigate a date picker, click Search) — every one of those selectors was
individually confirmed against real captured evidence, but the multi-step
UI interaction as a whole turned out to be flaky in practice: repeated live
runs (including one from a clean, non-sandboxed network) intermittently
produced zero results, most likely ordinary browser-automation timing
variance (a click landing before a re-render settles, etc.) rather than
anything site-side — nothing about the failures looked like a block
(no 429s, no soft-block pages, robots.txt stayed open throughout).

Rather than keep patching an inherently multi-step-fragile flow, this
version removes the UI interaction almost entirely:

  1. Load the homepage once. Akasa's own frontend JS fires several API
     calls automatically on load (no click required) — including a
     `/api/ibe/resources/master-data` request carrying a bearer token in
     its `authorization` header, generated moments earlier via
     `/api/ibe/token/generateToken`.
  2. Capture that `authorization` header value from the intercepted
     request (not the site's rendered UI at all).
  3. Issue the fare-search POST directly —
     `https://prod-bl.qp.akasaair.com/api/ibe/availability/search`, with
     the request body shape captured from a real search (see
     `build_search_request_body`) — using Playwright's `page.request`
     (shares the browser context's cookies/TLS fingerprint with the page
     that just loaded, so this isn't a bare disconnected HTTP call).

One page load plus one direct API call, with no fragile click/type/select
sequence at all. Confirmed working end-to-end against the real live site
before being committed: 16 real DEL-BOM flight quotes for a T+7 date, real
flight numbers (QP1940, QP1119, QP1112, ...), real times, and a fare
breakdown that sums correctly.

The response shape (confirmed against a real captured payload, not
guessed): `data.results[].trips[].journeysAvailableByMarket[].value[]` for
journeys (each with `designator` {origin, destination, departure, arrival}
and `segments[].identifier` {carrierCode, identifier} for the real flight
number) cross-referenced by `fareAvailabilityKey` into
`data.faresAvailable[]` for the fare breakdown (`passengerFares[]` with
`fareAmount` = total, `discountedFare` = base fare component;
`fareAmount - discountedFare` = taxes/fees, confirmed by summing the
itemized `serviceCharges` in a real captured response and getting the same
figure).

Only single-segment (non-stop) journeys are parsed for `flight_number`/
`carrier_code` (taken from segments[0]) — a connecting itinerary's fare is
still captured correctly (the journey-level `designator` covers the whole
trip), but its flight number reflects only the first leg. Full multi-leg
detail is a reasonable follow-up, not required for this basket (DGCA
traffic data for the target city-pairs is dominated by non-stop service).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import AsyncIterator, Iterable

from playwright.async_api import Browser, async_playwright
from playwright_stealth import Stealth

from apixproj.raw_fare_item import build_raw_fare_item
from app.ingestion.scheduler import RouteDateMatrixScheduler, SearchTask
from compliance import RobotsComplianceGateway, get_random_user_agent
from middlewares import MissingDataTracker

logger = logging.getLogger("apix.airline.akasa_air")

SOURCE_NAME = "akasa_air"
SOURCE_TYPE = "airline_direct"
CARRIER_CODE = "QP"
AIRLINE_NAME = "Akasa Air"
HOME_URL = "https://www.akasaair.com/"

# Confirmed real endpoint — captured from an actual search's network traffic.
FARE_SEARCH_URL = "https://prod-bl.qp.akasaair.com/api/ibe/availability/search"

# A request whose response carries the bearer token needed for the fare-
# search POST, fired automatically by Akasa's own frontend on page load —
# no click required. Any one of the automatic /api/ibe/* calls would do;
# this one was confirmed present on every homepage load tested.
TOKEN_CARRYING_REQUEST_SUBSTRING = "/api/ibe/resources/master-data"

NAVIGATION_TIMEOUT_MS = 30_000
TOKEN_WAIT_TIMEOUT_MS = 15_000
# Small random jitter added on top of the compliance gateway's own
# crawl_delay() between successive fare-search calls in a multi-task run
# (see RouteDateMatrixScheduler-driven batches) -- akasa_air has no
# Crawl-delay in robots.txt, so the gateway falls back to
# FALLBACK_POLITENESS_DELAY_S; jitter just avoids a metronome-regular
# request cadence on top of that floor, never below it.
INTER_TASK_JITTER_S = (0.5, 1.5)


def build_search_request_body(task: SearchTask) -> dict:
    """The fare-search POST body — shape confirmed against a real captured
    request (see module docstring), origin/destination/date substituted
    from the task."""
    return {
        "criteria": [
            {
                "stations": {
                    "originStationCodes": [task.origin],
                    "destinationStationCodes": [task.destination],
                    "searchDestinationMacs": True,
                    "searchOriginMacs": True,
                },
                "dates": {"beginDate": f"{task.departure_date.isoformat()}T00:00:00"},
                "filters": {
                    "compressionType": 1,
                    "maxConnections": 8,
                    "productClasses": ["NB", "LB", "EC", "AV"],
                    "fareTypes": ["NB", "LB", "R", "V"],
                },
            }
        ],
        "passengers": {"types": [{"type": "ADT", "count": 1}], "residentCountry": ""},
        "codes": {"currencyCode": "INR", "promotionCode": ""},
        "offerCode": None,
        "numberOfFaresPerJourney": 10,
        "taxesAndFees": 1,
    }


def _to_optional_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def parse_fare_search_response(payload: dict, task: SearchTask) -> list[dict]:
    """Turn one `/api/ibe/availability/search` JSON payload into raw fare
    item dicts. Shape confirmed against a real captured response — see
    module docstring."""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []

    fares_by_key = {
        entry.get("key"): entry.get("value")
        for entry in (data.get("faresAvailable") or [])
        if isinstance(entry, dict)
    }

    items: list[dict] = []
    for result in data.get("results") or []:
        for trip in (result or {}).get("trips") or []:
            for market in trip.get("journeysAvailableByMarket") or []:
                for journey in market.get("value") or []:
                    items.extend(_parse_journey(journey, fares_by_key, task))
    return items


def _parse_journey(journey: dict, fares_by_key: dict, task: SearchTask) -> list[dict]:
    segments = journey.get("segments") or []
    if not segments:
        return []
    designator = journey.get("designator") or {}
    # Requesting searchOriginMacs/searchDestinationMacs (see
    # build_search_request_body) makes Akasa's API also return itineraries
    # via nearby alternate airports in the same metro area (observed live:
    # a DEL-BOM search returning some DXN-BOM / DEL-NMI journeys). The
    # index basket in app/ingestion/scheduler.py is a fixed set of exact
    # city pairs, so an alternate-airport substitution is off-basket data,
    # not a cleaner/better version of the searched route — drop it here
    # rather than let it dilute a route's index series with a different
    # route's fares.
    if designator.get("origin") != task.origin or designator.get("destination") != task.destination:
        return []
    first_segment_identifier = (segments[0] or {}).get("identifier") or {}
    carrier_code = first_segment_identifier.get("carrierCode") or CARRIER_CODE
    flight_number = f"{carrier_code}{first_segment_identifier.get('identifier', '')}"

    items: list[dict] = []
    for fare_ref in journey.get("fares") or []:
        fare_key = fare_ref.get("fareAvailabilityKey")
        fare_detail = fares_by_key.get(fare_key)
        if not fare_detail:
            continue

        fare_list = fare_detail.get("fares") or []
        if not fare_list:
            continue
        passenger_fares = fare_list[0].get("passengerFares") or []
        if not passenger_fares:
            continue
        pf = passenger_fares[0]

        total_fare = _to_optional_float(pf.get("fareAmount"))
        base_fare = _to_optional_float(pf.get("discountedFare"))
        if total_fare is None:
            continue
        taxes_and_fees = (total_fare - base_fare) if base_fare is not None else None

        details = fare_ref.get("details") or []
        seats_left = details[0].get("availableCount") if details else None

        items.append(
            build_raw_fare_item(
                flight_number=flight_number,
                airline_name=AIRLINE_NAME,
                carrier_code=carrier_code,
                origin=designator.get("origin") or task.origin,
                destination=designator.get("destination") or task.destination,
                departure_time=designator.get("departure"),
                arrival_time=designator.get("arrival"),
                base_fare=base_fare,
                taxes_and_fees=taxes_and_fees,
                total_fare=total_fare,
                seats_left=seats_left,
                lead_window=task.advance_purchase_window,
                source_name=SOURCE_NAME,
                source_type=SOURCE_TYPE,
            )
        )
        # One fare option per journey is enough for an index data point;
        # avoids multiplying every fare-class/bundle combination per flight.
        break
    return items


class AkasaAirSpider:
    """Standalone Playwright driver — not a scrapy.Spider subclass, same
    shape as AirIndiaStealthSpider/IxigoStealthSpider (see those modules).
    One browser context is reused across all tasks (the auth token is
    captured once and reused), so tasks after the first skip the page
    load entirely.

    Usage:
        spider = AkasaAirSpider()
        async for item in spider.run():
            ...
        print(spider.missing_data.summary())
    """

    source_name = SOURCE_NAME

    def __init__(
        self,
        tasks: Iterable[SearchTask] | None = None,
        headless: bool = True,
        missing_data: MissingDataTracker | None = None,
    ):
        self.tasks = list(tasks) if tasks is not None else RouteDateMatrixScheduler().build()
        self.headless = headless
        self.gateway = RobotsComplianceGateway()
        self.missing_data = missing_data or MissingDataTracker()

    async def run(self) -> AsyncIterator[dict]:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = None
            auth_header = None
            crawl_delay_s = self.gateway.crawl_delay(self.source_name)
            try:
                for i, task in enumerate(self.tasks):
                    if i > 0:
                        # Politeness pacing between successive fare-search calls
                        # in a multi-task batch — see INTER_TASK_JITTER_S.
                        await asyncio.sleep(crawl_delay_s + random.uniform(*INTER_TASK_JITTER_S))

                    decision = self.gateway.check(self.source_name, HOME_URL)
                    if not decision.allowed:
                        logger.warning("akasa_air: task %s BLOCKED at dispatch: %s", task.task_id, decision.reason)
                        self.missing_data.flag(
                            key=task.route_id, task_id=task.task_id, source_name=self.source_name,
                            reason=f"compliance_blocked:{decision.reason}",
                        )
                        continue

                    if context is None or auth_header is None:
                        context, auth_header = await self._establish_session(browser)
                        if auth_header is None:
                            logger.error("akasa_air: task %s — could not establish a session/auth token", task.task_id)
                            self.missing_data.flag(
                                key=task.route_id, task_id=task.task_id, source_name=self.source_name,
                                reason="auth_token_not_captured",
                            )
                            continue

                    async for item in self._run_task(context, auth_header, task):
                        yield item
            finally:
                if context is not None:
                    await context.close()
                await browser.close()
        if self.missing_data:
            logger.info(
                "akasa_air: run finished with %d missing-data flag(s): %s",
                len(self.missing_data), self.missing_data.summary(),
            )

    async def _establish_session(self, browser: Browser):
        """Load the homepage once, capture the bearer token Akasa's own JS
        generates automatically (no click needed) from the first matching
        request. Returns (context, auth_header) — auth_header is None if
        it couldn't be captured within the timeout."""
        context = await browser.new_context(
            user_agent=get_random_user_agent(),
            viewport={"width": 1440, "height": 1100},
            locale="en-IN",
        )
        await Stealth().apply_stealth_async(context)
        page = await context.new_page()

        captured: dict[str, str] = {}

        async def _on_request(request) -> None:
            if TOKEN_CARRYING_REQUEST_SUBSTRING in request.url and "authorization" in request.headers:
                captured["authorization"] = request.headers["authorization"]

        page.on("request", _on_request)

        try:
            await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
            for _ in range(TOKEN_WAIT_TIMEOUT_MS // 500):
                if "authorization" in captured:
                    break
                await page.wait_for_timeout(500)
        except Exception as exc:  # noqa: BLE001 — logged by the caller via the missing-data flag
            logger.error("akasa_air: homepage load failed while establishing session: %s", exc)
        finally:
            await page.close()

        return context, captured.get("authorization")

    async def _run_task(self, context, auth_header: str, task: SearchTask) -> AsyncIterator[dict]:
        try:
            response = await context.request.post(
                FARE_SEARCH_URL,
                headers={
                    "authorization": auth_header,
                    "content-type": "application/json",
                    "accept": "application/json, text/plain, */*",
                    "referer": HOME_URL,
                },
                data=json.dumps(build_search_request_body(task)),
                timeout=NAVIGATION_TIMEOUT_MS,
            )
            if response.status != 200:
                raise RuntimeError(f"fare-search API returned HTTP {response.status}")
            payload = await response.json()
        except Exception as exc:  # noqa: BLE001 — request failures are logged per task, not fatal to the run
            logger.error("akasa_air: task %s fare-search request failed: %s", task.task_id, exc)
            self.missing_data.flag(
                key=task.route_id, task_id=task.task_id, source_name=self.source_name,
                reason=f"request_failed:{type(exc).__name__}",
            )
            return

        try:
            items = parse_fare_search_response(payload, task)
        except Exception as exc:  # noqa: BLE001 — a malformed/unexpected payload for one task must never crash the batch
            logger.error("akasa_air: task %s — payload parsing failed: %s", task.task_id, exc)
            self.missing_data.flag(
                key=task.route_id, task_id=task.task_id, source_name=self.source_name,
                reason=f"parse_failed:{type(exc).__name__}",
            )
            return

        if not items:
            logger.warning(
                "akasa_air: task %s — fare-search API responded but zero items were extracted "
                "(no flights that day, or the response shape changed)", task.task_id,
            )
            self.missing_data.flag(
                key=task.route_id, task_id=task.task_id, source_name=self.source_name,
                reason="zero_items_extracted",
            )
            return

        for item in items:
            yield item
