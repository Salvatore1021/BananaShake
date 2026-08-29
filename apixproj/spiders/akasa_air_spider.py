"""
APIx — Akasa Air Spider (Playwright + playwright-stealth)
=============================================================

Airline-direct spider for Akasa Air (IATA carrier code QP) — CLEARED in
compliance.SOURCE_REGISTRY (robots.txt is fully open, `User-agent: *` with
zero Disallow rules, re-verified 2026-08-30 superseding an earlier stale
403 finding — see that registry entry's notes).

UNLIKE every other spider in this project, everything below is confirmed
against REAL, live-captured evidence from an actual browsing session, not
assumed or guessed:

  * Search-form selectors: `#From` / `#To` are real `<input>` elements.
    Selecting an origin/destination is NOT done by typing + Enter (typing
    does not filter the shown list) — it's done by clicking the visible,
    already-rendered entry for the target airport's full name in the
    "Our Destinations" panel (e.g. "Indira Gandhi International Airport"
    for DEL), located via Playwright's text locator, which auto-scrolls
    and clicks it.
  * Date picker: a standard `react-datepicker` — day cells are
    `role="gridcell"` with `aria-label="Choose <Weekday>, <Month> <Day>,
    <Year>"` for a selectable date (a disabled/past date instead reads
    "Not available ..."). Month navigation is `.react-datepicker__navigation--next`.
  * Submit: a `text="Search Flights"` button.
  * The fare-search API, captured directly: a POST to
    `https://prod-bl.qp.akasaair.com/api/ibe/availability/search`, JSON
    response shaped as
    `data.results[].trips[].journeysAvailableByMarket[].value[]` for
    journeys (each with `designator` {origin, destination, departure,
    arrival} and `segments[].identifier` {carrierCode, identifier} for the
    real flight number) cross-referenced by `fareAvailabilityKey` into
    `data.faresAvailable[]` for the fare breakdown (`passengerFares[]`
    with `fareAmount` = total, `discountedFare` = base fare component;
    `fareAmount - discountedFare` = taxes/fees, confirmed by summing the
    itemized `serviceCharges` in a real captured response and getting the
    same figure).

Only single-segment (non-stop) journeys are parsed for `flight_number`/
`carrier_code` (taken from segments[0]) — a connecting itinerary's fare is
still captured correctly (the journey-level `designator` covers the whole
trip), but its flight number reflects only the first leg. Full multi-leg
detail is a reasonable follow-up, not required for this basket (DGCA
traffic data for the target city-pairs is dominated by non-stop service).
"""

from __future__ import annotations

import logging
import re
from datetime import date as date_cls
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

# Confirmed real full airport names, captured from Akasa's own "Our
# Destinations" search-form panel — used as the text to click, since
# typing does not filter that list. Covers app/ingestion/scheduler.py's
# ROUTE_PAIRS basket.
AIRPORT_FULL_NAMES = {
    "DEL": "Indira Gandhi International Airport",
    "BOM": "Chhatrapati Shivaji Maharaj International Airport",
    "BLR": "Kempegowda International Airport",
    "CCU": "Netaji Subhash Chandra Bose International Airport",
    "HYD": "Rajiv Gandhi International Airport",
    "MAA": "Chennai International Airport",
}

FARE_API_URL_SUBSTRING = "/api/ibe/availability/search"
NAVIGATION_TIMEOUT_MS = 30_000
FORM_INTERACTION_TIMEOUT_MS = 10_000
_ORDINAL_SUFFIXES = {1: "st", 2: "nd", 3: "rd"}

_stealth = Stealth()


def _ordinal_day(day: int) -> str:
    if 10 <= day % 100 <= 20:
        suffix = "th"
    else:
        suffix = _ORDINAL_SUFFIXES.get(day % 10, "th")
    return f"{day}{suffix}"


def format_datepicker_label_fragment(target_date: date_cls) -> str:
    """The `<Month> <Day><suffix>, <Year>` fragment react-datepicker uses
    in its day-cell aria-label — e.g. "September 5th, 2026" — matched as a
    substring so it works regardless of the "Choose "/"Not available "
    prefix."""
    return f"{target_date.strftime('%B')} {_ordinal_day(target_date.day)}, {target_date.year}"


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
            try:
                for task in self.tasks:
                    async for item in self._run_task(browser, task):
                        yield item
            finally:
                await browser.close()
        if self.missing_data:
            logger.info(
                "akasa_air: run finished with %d missing-data flag(s): %s",
                len(self.missing_data), self.missing_data.summary(),
            )

    async def _run_task(self, browser: Browser, task: SearchTask) -> AsyncIterator[dict]:
        decision = self.gateway.check(self.source_name, HOME_URL)
        if not decision.allowed:
            logger.warning("akasa_air: task %s BLOCKED at dispatch: %s", task.task_id, decision.reason)
            self.missing_data.flag(
                key=task.route_id, task_id=task.task_id, source_name=self.source_name,
                reason=f"compliance_blocked:{decision.reason}",
            )
            return

        origin_name = AIRPORT_FULL_NAMES.get(task.origin)
        destination_name = AIRPORT_FULL_NAMES.get(task.destination)
        if not origin_name or not destination_name:
            logger.warning(
                "akasa_air: task %s — no confirmed airport full-name mapping for %s/%s; "
                "AIRPORT_FULL_NAMES needs extending for this route", task.task_id, task.origin, task.destination,
            )
            self.missing_data.flag(
                key=task.route_id, task_id=task.task_id, source_name=self.source_name,
                reason="airport_name_not_mapped",
            )
            return

        context = await browser.new_context(
            user_agent=get_random_user_agent(),
            viewport={"width": 1440, "height": 1100},
            locale="en-IN",
        )
        await _stealth.apply_stealth_async(context)
        page = await context.new_page()

        captured_payloads: list[dict] = []

        async def _on_response(playwright_response) -> None:
            if FARE_API_URL_SUBSTRING not in playwright_response.url:
                return
            if playwright_response.request.method != "POST":
                return
            try:
                body = await playwright_response.json()
            except Exception:  # noqa: BLE001 — not every matched response is valid JSON
                return
            captured_payloads.append(body)

        page.on("response", _on_response)

        try:
            await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
            await page.wait_for_timeout(3_000)
            try:
                await page.click('button:has-text("Accept cookies")', timeout=3_000)
            except Exception:  # noqa: BLE001 — banner may already be dismissed/absent
                pass

            await self._select_airport(page, "#From", origin_name)
            await self._select_airport(page, "#To", destination_name)
            await self._select_date(page, task.departure_date)

            await page.click('text="Search Flights"', timeout=FORM_INTERACTION_TIMEOUT_MS)
            await page.wait_for_timeout(15_000)  # the fare-search API call fires async after navigation
        except Exception as exc:  # noqa: BLE001 — navigation/interaction failures are logged per task, not fatal to the run
            logger.error("akasa_air: task %s navigation/search failed: %s", task.task_id, exc)
            self.missing_data.flag(
                key=task.route_id, task_id=task.task_id, source_name=self.source_name,
                reason=f"navigation_failed:{type(exc).__name__}",
            )
            await context.close()
            return

        if not captured_payloads:
            logger.warning(
                "akasa_air: task %s — search flow completed but intercepted ZERO matching "
                "fare-search responses (pattern=%s)", task.task_id, FARE_API_URL_SUBSTRING,
            )
            self.missing_data.flag(
                key=task.route_id, task_id=task.task_id, source_name=self.source_name,
                reason="zero_payloads_captured",
            )
            await context.close()
            return

        yielded_any = False
        for payload in captured_payloads:
            for item in parse_fare_search_response(payload, task):
                yielded_any = True
                yield item

        if not yielded_any:
            self.missing_data.flag(
                key=task.route_id, task_id=task.task_id, source_name=self.source_name,
                reason="zero_items_extracted",
            )

        await context.close()

    async def _select_airport(self, page, input_selector: str, full_name: str) -> None:
        await page.click(input_selector, timeout=FORM_INTERACTION_TIMEOUT_MS)
        await page.wait_for_timeout(400)
        await page.get_by_text(full_name, exact=True).click(timeout=FORM_INTERACTION_TIMEOUT_MS)

    async def _select_date(self, page, target_date: date_cls) -> None:
        await page.click('input[name="DepartureDate"]', timeout=FORM_INTERACTION_TIMEOUT_MS)
        await page.wait_for_timeout(400)

        label_fragment = format_datepicker_label_fragment(target_date)
        for _ in range(12):  # up to a year out at one calendar page (month) per click
            cell = page.locator(f'[role="gridcell"][aria-label*="{label_fragment}"]')
            if await cell.count() > 0:
                aria_label = await cell.first.get_attribute("aria-label") or ""
                if aria_label.startswith("Not available"):
                    raise RuntimeError(f"target date {target_date.isoformat()} is not available for booking")
                await cell.first.click(timeout=FORM_INTERACTION_TIMEOUT_MS)
                return
            next_button = page.locator(".react-datepicker__navigation--next")
            if await next_button.count() == 0:
                break
            await next_button.first.click()
            await page.wait_for_timeout(300)

        raise RuntimeError(f"could not locate a selectable date cell for {target_date.isoformat()}")
