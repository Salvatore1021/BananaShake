"""
APIx — Air India Stealth Fallback Spider (Playwright + playwright-stealth)
=============================================================================

Airline-direct fallback for JavaScript-rendered fare-search portals: drives
a real, stealth-patched Chromium browser through Playwright, performs the
site's own fare search, and intercepts the JSON response the site's own
frontend receives — the same network-interception pattern as
base_spider.py, reimplemented here as a small self-contained driver so it
can run standalone in apixproj without depending on the app.ingestion
package's spider-layer import layout.

Air India is the only source marked CLEARED in compliance.SOURCE_REGISTRY
today (no blanket robots.txt disallow) — see that module's docstring for
why every other airline-direct / OTA source stays gated until an actual
legal review clears it. The Compliance Gateway is still checked per task,
immediately before each navigation, exactly like every other spider in this
project: scheduling a task does not mean it's still safe to fire by the
time the crawl actually reaches it.

playwright-stealth is applied to every browser context BEFORE any page is
created (patches navigator.webdriver, Sec-Ch-Ua, plugins/languages
fingerprints, etc.) so this fallback behaves like a real browser rather
than an automated one. That's an appropriate technique here specifically
BECAUSE Air India's own robots.txt does not prohibit this traffic — using
the same technique against a source that DOES prohibit automated access
would be evading an access-control decision rather than blending into
ordinary browser traffic, which is exactly the line compliance.py's
SOURCE_REGISTRY exists to enforce. Do not point SOURCE_NAME at a source
whose tos_review_status isn't CLEARED; the gateway check below will refuse
to schedule it, but the intent matters as much as the enforcement.

IMPORTANT — what's real vs illustrative (see air_india_spider.py, the
worked example this reuses the assumed JSON shape from):
  * Compliance gating, task consumption, stealth application, network
    interception wiring, and item shaping are the real, load-bearing
    implementation.
  * SEARCH_URL_TEMPLATE and FARE_API_URL_PATTERN are illustrative
    placeholders — Air India's exact fare-search URL structure and
    internal API path are not publicly documented and need confirming from
    a live DevTools session before this can run against the real site,
    exactly as already flagged in air_india_spider.py.
  * `parse_fare_json` likewise assumes a plausible-but-unconfirmed JSON
    shape. Update it to match whatever the real endpoint returns once
    confirmed.
"""

from __future__ import annotations

import logging
import random
import re
from typing import AsyncIterator, Iterable

from playwright.async_api import Browser, async_playwright
from playwright_stealth import Stealth

from apixproj.raw_fare_item import build_raw_fare_item
from app.ingestion.scheduler import RouteDateMatrixScheduler, SearchTask
from compliance import USER_AGENT_POOL, RobotsComplianceGateway

logger = logging.getLogger("apix.airline.air_india_stealth")

SOURCE_NAME = "air_india"
SOURCE_TYPE = "airline_direct"

# TODO(confirm-before-enabling): verified against DevTools Network tab, not guessed.
SEARCH_URL_TEMPLATE = (
    "https://www.airindia.com/in/en/book-flight.html"
    "?tripType=O&from={origin}&to={destination}&date={departure_date}&paxType=ADULT-1"
)

# TODO(confirm-before-enabling): the real internal fare-search XHR/fetch path.
FARE_API_URL_PATTERN = re.compile(r"/api/.*fare-search|/api/.*flight-search", re.IGNORECASE)

NAVIGATION_TIMEOUT_MS = 30_000

_stealth = Stealth()


def parse_fare_json(payload: dict, task: SearchTask) -> list[dict]:
    """Turn one intercepted JSON payload into raw fare item dicts.

    Assumed (unconfirmed — see module docstring) response shape:
        {
          "results": [
            {
              "flightNumber": "AI101",
              "airlineName": "Air India",
              "departureTime": "06:05",
              "arrivalTime": "08:20",
              "totalFare": 5820.0,
              "baseFare": 4400.0,
              "taxesAndFees": 1420.0,
              "seatsLeft": 4,
            }, ...
          ]
        }
    """
    results = payload.get("results")
    if not isinstance(results, list):
        return []

    items: list[dict] = []
    for entry in results:
        try:
            total_fare = float(entry["totalFare"])
        except (KeyError, TypeError, ValueError):
            continue  # not a fare record we can use — skip rather than crash the whole batch

        items.append(
            build_raw_fare_item(
                flight_number=entry.get("flightNumber"),
                airline_name=entry.get("airlineName", "Air India"),
                carrier_code="AI",
                origin=task.origin,
                destination=task.destination,
                departure_time=entry.get("departureTime"),
                arrival_time=entry.get("arrivalTime"),
                base_fare=_to_optional_float(entry.get("baseFare")),
                taxes_and_fees=_to_optional_float(entry.get("taxesAndFees")),
                total_fare=total_fare,
                seats_left=entry.get("seatsLeft"),
                lead_window=task.advance_purchase_window,
                source_name=SOURCE_NAME,
                source_type=SOURCE_TYPE,
            )
        )
    return items


def _to_optional_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def build_search_url(task: SearchTask) -> str:
    return SEARCH_URL_TEMPLATE.format(
        origin=task.origin, destination=task.destination, departure_date=task.departure_date.isoformat(),
    )


class AirIndiaStealthSpider:
    """Standalone Playwright driver — not a scrapy.Spider subclass, since
    this fallback manages its own browser lifecycle directly rather than
    going through Scrapy's downloader (there is no HTTP request/response
    cycle to hand to Scrapy here; the browser itself is the client).

    Usage:
        spider = AirIndiaStealthSpider()
        async for item in spider.run():
            ...
    """

    source_name = SOURCE_NAME

    def __init__(self, tasks: Iterable[SearchTask] | None = None, headless: bool = True):
        self.tasks = list(tasks) if tasks is not None else RouteDateMatrixScheduler().build()
        self.headless = headless
        self.gateway = RobotsComplianceGateway()

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

    async def _run_task(self, browser: Browser, task: SearchTask) -> AsyncIterator[dict]:
        url = build_search_url(task)
        decision = self.gateway.check(self.source_name, url)
        if not decision.allowed:
            logger.warning("air_india_stealth: task %s BLOCKED at dispatch: %s", task.task_id, decision.reason)
            return

        context = await browser.new_context(
            user_agent=random.choice(USER_AGENT_POOL),
            viewport={"width": 1440, "height": 1100},
            locale="en-US",
        )
        await _stealth.apply_stealth_async(context)
        page = await context.new_page()

        captured_payloads: list[dict] = []

        async def _on_response(playwright_response) -> None:
            if not FARE_API_URL_PATTERN.search(playwright_response.url):
                return
            try:
                body = await playwright_response.json()
            except Exception:  # noqa: BLE001 — not every matched response is valid JSON
                logger.debug(
                    "air_india_stealth: task %s — matched URL %s but body wasn't JSON",
                    task.task_id, playwright_response.url,
                )
                return
            captured_payloads.append(body)

        page.on("response", _on_response)

        try:
            await page.goto(url, wait_until="networkidle", timeout=NAVIGATION_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001 — navigation failures are logged per task, not fatal to the run
            logger.error("air_india_stealth: task %s navigation failed: %s", task.task_id, exc)
            await context.close()
            return

        if not captured_payloads:
            logger.warning(
                "air_india_stealth: task %s — navigation completed but intercepted ZERO matching "
                "responses (pattern=%s). Either the site changed its API shape, the page didn't "
                "fully load, or FARE_API_URL_PATTERN needs updating.",
                task.task_id, FARE_API_URL_PATTERN.pattern,
            )
            await context.close()
            return

        for payload in captured_payloads:
            for item in parse_fare_json(payload, task):
                yield item

        await context.close()
