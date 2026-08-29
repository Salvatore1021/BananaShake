"""
APIx — Ixigo Stealth OTA Spider (Playwright + playwright-stealth)
=====================================================================

OTA fallback used when Yatra (this project's primary OTA target) can't be
reached at all — see the live-verification session this module came out of
(summarized below) for why a browser-driven, form-filling approach is
required here, not the lightweight request-based approach
ota_yatra_spider.py uses.

WHY THIS MODULE EXISTS (concrete findings, not assumptions):

  1. Ixigo's flight-search UI (https://www.ixigo.com/) is a heavily
     client-rendered Next.js app — a plain HTTP GET of the page returns an
     HTML shell plus ~30 JS chunk files and NO usable fare data; there is
     no server-rendered search-results HTML or embedded JSON to regex out
     the way ota_yatra_spider.py does for Yatra. A real browser executing
     that JS is a hard requirement here, not an optimization — the same
     conclusion the network-interception pattern in
     air_india_stealth_spider.py is built around, reapplied here.

  2. The search form's input elements DO have stable, confirmed selectors
     — captured from a real (pre-rate-limit) page load:
         [data-testid="originId"]
         [data-testid="destinationId"]
         [data-testid="departureDate"]
         [data-testid="returnDate"]
         [data-testid="pax"]
     These are real, not guessed. What is NOT yet confirmed is (a) the
     exact submit-button selector and (b) the resulting fare-search XHR's
     URL pattern and JSON shape — recon was cut short by the finding below
     before either could be captured.

  3. During that same recon session, Ixigo's edge returned an explicit,
     unambiguous HTTP 429 "Oops...too many requests!" page after only two
     or three navigations — a real rate-limit signal, not a guess or a
     timeout. Per this project's own CircuitBreaker design (middlewares.py:
     stop, don't retry-harder, don't rotate identity to route around a
     block), that session stopped there rather than continuing to probe
     for the submit button / API shape. FARE_API_URL_PATTERN below is
     therefore a best-effort placeholder based on common OTA API-path
     conventions, not a confirmed value — treat SEARCH_BUTTON_SELECTORS
     and FARE_API_URL_PATTERN as the two remaining
     TODO(confirm-before-enabling) items, to be finished on a FUTURE run
     from a network that isn't already rate-limited, with normal pacing
     (not back-to-back navigations).

Compliance: ixigo is PENDING_LEGAL_REVIEW in compliance.SOURCE_REGISTRY,
gated the same way as Yatra — CLEARED only via the explicit local-test
opt-in (APIX_ALLOW_OTA_SCRAPING=1). The Compliance Gateway is checked
before every navigation, same as every other spider in this project.
"""

from __future__ import annotations

import logging
import re
from typing import AsyncIterator, Iterable

from playwright.async_api import Browser, async_playwright
from playwright_stealth import Stealth

from apixproj.raw_fare_item import build_raw_fare_item
from app.ingestion.scheduler import RouteDateMatrixScheduler, SearchTask
from compliance import RobotsComplianceGateway, get_random_user_agent
from middlewares import MissingDataTracker

logger = logging.getLogger("apix.ota.ixigo_stealth")

SOURCE_NAME = "ixigo"
SOURCE_TYPE = "ota"

HOME_URL = "https://www.ixigo.com/"

# Confirmed real selectors — captured from an actual (successful,
# pre-rate-limit) page load. See module docstring point 2.
ORIGIN_SELECTOR = '[data-testid="originId"]'
DESTINATION_SELECTOR = '[data-testid="destinationId"]'
DEPARTURE_DATE_SELECTOR = '[data-testid="departureDate"]'
PAX_SELECTOR = '[data-testid="pax"]'

# TODO(confirm-before-enabling): NOT yet observed against a live page — the
# recon session that captured the selectors above got rate-limited before
# reaching the results page. Try these in order; update once confirmed.
SEARCH_BUTTON_SELECTORS = (
    'button:has-text("Search")',
    'button:has-text("SEARCH")',
    '[data-testid="search"]',
    '[data-testid="searchBtn"]',
    '[data-testid="searchButton"]',
)

# TODO(confirm-before-enabling): best-effort placeholder, not a confirmed
# endpoint — see module docstring point 3 for why this couldn't be
# captured yet. Broad by design (matches any XHR/fetch whose path looks
# search/flight/fare-related) so a first successful run can log the real
# matched URL(s) for narrowing later, rather than matching nothing at all.
FARE_API_URL_PATTERN = re.compile(r"(search|flight|fare).*(result|price|list)", re.IGNORECASE)

NAVIGATION_TIMEOUT_MS = 30_000
FORM_INTERACTION_TIMEOUT_MS = 10_000

_stealth = Stealth()


def parse_fare_json(payload, task: SearchTask) -> list[dict]:
    """Turn one intercepted JSON payload into raw fare item dicts.

    UNCONFIRMED response shape — Ixigo's real fare-search JSON has not
    been captured yet (see module docstring). This assumes a generic
    `{"flights": [...]}` / `{"results": [...]}` list-of-records shape as a
    starting point; update once a real payload is captured and logged by
    a successful run (see the `logger.info` call in `_run_task` that dumps
    the first captured payload's top-level keys for exactly this purpose).
    """
    if not isinstance(payload, dict):
        return []
    records = payload.get("flights") or payload.get("results") or payload.get("data")
    if not isinstance(records, list):
        return []

    items: list[dict] = []
    for entry in records:
        if not isinstance(entry, dict):
            continue
        total_fare = _to_optional_float(
            entry.get("totalFare") or entry.get("total_fare") or entry.get("price") or entry.get("fare")
        )
        if total_fare is None:
            continue  # not a fare record we can use — skip rather than crash the whole batch

        items.append(
            build_raw_fare_item(
                flight_number=entry.get("flightNumber") or entry.get("flight_number"),
                airline_name=entry.get("airlineName") or entry.get("airline_name") or entry.get("airline"),
                carrier_code=entry.get("carrierCode") or entry.get("airlineCode") or entry.get("carrier_code"),
                origin=task.origin,
                destination=task.destination,
                departure_time=entry.get("departureTime") or entry.get("departure_time"),
                arrival_time=entry.get("arrivalTime") or entry.get("arrival_time"),
                base_fare=_to_optional_float(entry.get("baseFare") or entry.get("base_fare")),
                taxes_and_fees=_to_optional_float(entry.get("taxesAndFees") or entry.get("taxes_and_fees")),
                total_fare=total_fare,
                seats_left=entry.get("seatsLeft") or entry.get("seats_left"),
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


class IxigoStealthSpider:
    """Standalone Playwright driver — form-fill + network-interception,
    not a scrapy.Spider subclass (mirrors AirIndiaStealthSpider's shape;
    see that module for the pattern this one reuses).

    Usage:
        spider = IxigoStealthSpider()
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
        debug_dir: str | None = None,
    ):
        self.tasks = list(tasks) if tasks is not None else RouteDateMatrixScheduler().build()
        self.headless = headless
        self.gateway = RobotsComplianceGateway()
        self.missing_data = missing_data or MissingDataTracker()
        # When set, a screenshot + full HTML dump is saved here on any
        # navigation/search failure — the fastest way to close the loop on
        # "what did the page actually look like" without another round of
        # guessing from log text alone.
        self.debug_dir = debug_dir

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
                "ixigo_stealth: run finished with %d missing-data flag(s): %s",
                len(self.missing_data), self.missing_data.summary(),
            )

    async def _run_task(self, browser: Browser, task: SearchTask) -> AsyncIterator[dict]:
        decision = self.gateway.check(self.source_name, HOME_URL)
        if not decision.allowed:
            logger.warning("ixigo_stealth: task %s BLOCKED at dispatch: %s", task.task_id, decision.reason)
            self.missing_data.flag(
                key=task.route_id, task_id=task.task_id, source_name=self.source_name,
                reason=f"compliance_blocked:{decision.reason}",
            )
            return

        context = await browser.new_context(
            user_agent=get_random_user_agent(),
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
                    "ixigo_stealth: task %s — matched URL %s but body wasn't JSON",
                    task.task_id, playwright_response.url,
                )
                return
            logger.info(
                "ixigo_stealth: task %s — captured JSON from %s, top-level keys=%s "
                "(use this to correct FARE_API_URL_PATTERN / parse_fare_json once confirmed)",
                task.task_id, playwright_response.url,
                list(body.keys()) if isinstance(body, dict) else type(body).__name__,
            )
            captured_payloads.append(body)

        page.on("response", _on_response)

        try:
            # networkidle, not domcontentloaded: three consecutive live runs
            # produced PIXEL-IDENTICAL debug screenshots despite exercising
            # different code paths in _fill_search_form (option-click vs.
            # Enter fallback, with/without a trailing Tab) — the strongest
            # evidence available that none of those interactions were
            # taking effect at all, not that the wrong element was being
            # targeted. This is a heavy client-hydrated Next.js app
            # (~30 JS chunks on the homepage alone); domcontentloaded fires
            # before React finishes attaching its event handlers, so clicks
            # sent immediately after it can land on DOM nodes that are
            # visually present but not yet wired up. This fix is REASONED
            # FROM EVIDENCE but has not itself been live-verified — the
            # next run should confirm whether it actually changes the
            # captured screenshot, not just assume it does.
            await page.goto(HOME_URL, wait_until="networkidle", timeout=NAVIGATION_TIMEOUT_MS)
            await self._fill_search_form(page, task)
            await page.wait_for_timeout(6_000)  # let the results page's XHRs land
        except Exception as exc:  # noqa: BLE001 — navigation/interaction failures are logged per task, not fatal to the run
            logger.error("ixigo_stealth: task %s navigation/search failed: %s", task.task_id, exc)
            await self._save_debug_artifacts(page, task)
            self.missing_data.flag(
                key=task.route_id, task_id=task.task_id, source_name=self.source_name,
                reason=f"navigation_failed:{type(exc).__name__}",
            )
            await context.close()
            return

        if not captured_payloads:
            logger.warning(
                "ixigo_stealth: task %s — search flow completed but intercepted ZERO matching "
                "responses (pattern=%s). FARE_API_URL_PATTERN is an unconfirmed placeholder — "
                "see module docstring — and likely needs updating against what actually fired.",
                task.task_id, FARE_API_URL_PATTERN.pattern,
            )
            self.missing_data.flag(
                key=task.route_id, task_id=task.task_id, source_name=self.source_name,
                reason="zero_payloads_captured",
            )
            await context.close()
            return

        for payload in captured_payloads:
            for item in parse_fare_json(payload, task):
                yield item

        await context.close()

    async def _fill_search_form(self, page, task: SearchTask) -> None:
        """Fill origin/destination via the confirmed selectors and submit.
        City-name typing (rather than the 3-letter code) is used for the
        origin/destination fields since that's how the visible search box
        behaves for a human.

        A first live run through this code (see commit history) showed a
        bare Enter-keypress leaving the destination field permanently
        "not visible" — strong evidence Enter alone wasn't registering an
        actual selection, just closing (or not closing) the suggestion
        list without picking anything. `_select_autocomplete_suggestion`
        now tries clicking a standard ARIA `role="option"` item first (the
        common, accessible pattern most autocomplete widgets use) and only
        falls back to bare Enter if no such option appears — still a
        best-effort heuristic, not a confirmed selector (see module
        docstring), but a more principled one than before.
        """
        origin_query = _AIRPORT_CITY_NAMES.get(task.origin, task.origin)
        destination_query = _AIRPORT_CITY_NAMES.get(task.destination, task.destination)

        await page.click(ORIGIN_SELECTOR, timeout=FORM_INTERACTION_TIMEOUT_MS)
        await self._select_autocomplete_suggestion(page, origin_query)
        await page.wait_for_selector(DESTINATION_SELECTOR, state="visible", timeout=FORM_INTERACTION_TIMEOUT_MS)

        await page.click(DESTINATION_SELECTOR, timeout=FORM_INTERACTION_TIMEOUT_MS)
        await self._select_autocomplete_suggestion(page, destination_query)
        await page.wait_for_timeout(500)

        for selector in SEARCH_BUTTON_SELECTORS:
            button = await page.query_selector(selector)
            if button is not None:
                await button.click()
                return

        logger.warning(
            "ixigo_stealth: task %s — none of SEARCH_BUTTON_SELECTORS matched; "
            "that list is unconfirmed and needs updating against a live page.",
            task.task_id,
        )

    @staticmethod
    async def _select_autocomplete_suggestion(page, query: str) -> None:
        """Type `query` into the currently-focused input and select the
        first autocomplete suggestion — see `_fill_search_form` for why
        this replaced a bare Enter keypress.

        A second and third live run (see commit history / debug
        screenshots) showed the origin dropdown staying open and
        IDENTICAL across attempts despite this method supposedly clicking
        an option and pressing Tab — strong evidence the bare
        `[role="option"]` locator wasn't matching anything inside the
        actually-open suggestion list at all (this is a content-heavy page
        with several other dropdowns/carousels that could easily contain
        an earlier, invisible `role="option"` element in DOM order), so
        every attempt silently fell through to the Enter-only fallback,
        which doesn't reliably close the panel. Scoping the locator to
        `[role="listbox"] [role="option"]` targets an option specifically
        inside a listbox, which should resolve to the actually-open one.
        Tab (not Escape — many autocomplete widgets treat Escape as
        "cancel the selection", which risks undoing it) is the standard
        "confirm this field and move on" keystroke.

        STILL UNCONFIRMED as of this pass: the `role="listbox"` scoping
        fix above has not itself been verified against a live page yet —
        it's the best-reasoned next step from the evidence gathered, not a
        proven fix. See the session's conversation for the full diagnostic
        trail before assuming this resolves it.
        """
        await page.keyboard.type(query, delay=80)
        try:
            option = page.locator('[role="listbox"] [role="option"]').first
            await option.wait_for(state="visible", timeout=4_000)
            await option.click()
        except Exception:  # noqa: BLE001 — fall back to Enter if no ARIA option surfaced
            await page.wait_for_timeout(800)
            await page.keyboard.press("Enter")

        await page.keyboard.press("Tab")
        try:
            await page.locator('[role="option"]').first.wait_for(state="hidden", timeout=3_000)
        except Exception:  # noqa: BLE001 — best-effort: proceed even if we can't confirm the panel closed
            pass

    async def _save_debug_artifacts(self, page, task: SearchTask) -> None:
        """Best-effort screenshot + full HTML dump on failure, when
        `self.debug_dir` is set — never allowed to mask the real error."""
        if not self.debug_dir:
            return
        import os

        os.makedirs(self.debug_dir, exist_ok=True)
        base = os.path.join(self.debug_dir, task.task_id.replace(":", "_").replace("/", "_"))
        try:
            await page.screenshot(path=f"{base}.png", full_page=True)
            html = await page.content()
            with open(f"{base}.html", "w", encoding="utf-8") as f:
                f.write(html)
            logger.info(
                "ixigo_stealth: task %s — saved debug screenshot/HTML to %s.png / %s.html",
                task.task_id, base, base,
            )
        except Exception as exc:  # noqa: BLE001 — a failed debug capture must not hide the original failure
            logger.debug("ixigo_stealth: task %s — could not save debug artifacts: %s", task.task_id, exc)


# Minimal IATA-code -> city-name lookup for the route basket this project
# targets (app/ingestion/scheduler.py's ROUTE_PAIRS) — Ixigo's origin/
# destination fields autocomplete on city name, not the 3-letter code.
_AIRPORT_CITY_NAMES = {
    "DEL": "Delhi",
    "BOM": "Mumbai",
    "BLR": "Bangalore",
    "CCU": "Kolkata",
    "HYD": "Hyderabad",
    "MAA": "Chennai",
}
