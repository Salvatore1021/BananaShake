"""
APIx — Base Fare Spider (Scrapy + Playwright)
================================================

Shared machinery for every per-source spider: driving a real browser through
Playwright to perform the site's own fare search, intercepting the JSON
response the site's own frontend receives (see
docs/pillar1-ingestion-architecture.md §3 for why interception beats DOM
parsing), and turning matched payloads into `RawFareQuote`-shaped items —
all gated through the Compliance Gateway a second time, immediately before
the request is actually sent (belt-and-braces: the scheduler already
filtered to cleared sources when building the job matrix, but robots.txt can
change between "job scheduled this morning" and "job dispatched this
afternoon", and the gateway's cache TTL means this check is cheap when
nothing has changed).

Subclasses implement three things:
    source_name              -- key into compliance.SOURCE_REGISTRY
    build_search_url(job)     -- the URL that starts this source's fare search
    fare_api_url_pattern      -- compiled regex matching the source's own
                                  internal JSON fare-search endpoint
    parse_fare_json(payload, job) -- turn one matched JSON payload into zero
                                  or more RawFareQuote-shaped dicts

Everything else (navigation, interception wiring, retry/backoff hookup,
compliance re-check, item shaping) lives here so a new source spider is a
small, mostly-declarative subclass — see spiders/air_india_spider.py.
"""

from __future__ import annotations

import logging
import re
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

import scrapy
from scrapy.http import Response
from scrapy_playwright.page import PageMethod

from app.ingestion.compliance import RobotsComplianceGateway
from app.ingestion.scheduler import ScrapeJob

logger = logging.getLogger("apix.ingestion.spiders.base")


class BaseFareSpider(scrapy.Spider, ABC):
    """
    Construct with a list of ScrapeJob (from scheduler.build_daily_job_matrix,
    already filtered to this spider's source) plus a shared scrape_batch_id
    tying every item from this run together for ScrapeJobLog / audit.

        scrapy crawl air_india -a jobs_file=today_air_india_jobs.json
    (in production, jobs are handed in-process by the orchestrator rather
    than round-tripped through a file; the file-based constructor here is a
    convenience for local runs / the hackathon demo.)
    """

    source_name: str  # set by subclass, must match a key in compliance.SOURCE_REGISTRY
    fare_api_url_pattern: re.Pattern  # set by subclass

    custom_settings = {
        "DOWNLOAD_HANDLERS": {
            "http": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
            "https": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
        },
        "TWISTED_REACTOR": "twisted.internet.asyncioreactor.AsyncioSelectorReactor",
        "PLAYWRIGHT_LAUNCH_OPTIONS": {"headless": True},
        "PLAYWRIGHT_DEFAULT_NAVIGATION_TIMEOUT": 30_000,  # ms
        # Belt-and-braces: Scrapy's OWN robots.txt middleware is left on too,
        # in addition to our explicit ComplianceGateway.check() call below.
        # Two independent implementations of the same check catching the
        # same mistake differently is cheap insurance for something this
        # load-bearing.
        "ROBOTSTXT_OBEY": True,
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_START_DELAY": 3.0,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 1,  # politeness is enforced by scheduling, not parallelism
        "DOWNLOADER_MIDDLEWARES": {
            "app.ingestion.middlewares.AntiBotBackoffMiddleware": 543,
        },
        "ITEM_PIPELINES": {
            "app.ingestion.pipelines.RawFareQuotePipeline": 300,
        },
    }

    def __init__(self, jobs: list[ScrapeJob] | None = None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.jobs = jobs or []
        self.scrape_batch_id = str(uuid.uuid4())
        self.gateway = RobotsComplianceGateway()

    # ------------------------------------------------------------------
    # Abstract interface each source spider implements
    # ------------------------------------------------------------------

    @abstractmethod
    def build_search_url(self, job: ScrapeJob) -> str:
        """The URL that starts this source's fare search for `job`
        (origin/destination/travel_date encoded however this source expects
        — query params for most airline-direct sites)."""
        raise NotImplementedError

    @abstractmethod
    def parse_fare_json(self, payload: dict, job: ScrapeJob) -> list[dict]:
        """Turn one intercepted JSON payload into zero or more
        RawFareQuote-shaped dicts (see app/models/schema.py RawFareQuote for
        the target fields). Return [] if this particular payload isn't the
        fare-search response (network interception catches EVERY response on
        the page — analytics beacons, ads, fonts — this method only ever
        receives ones that already matched `fare_api_url_pattern`, but a
        source may have more than one endpoint matching that pattern for
        different purposes)."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Scrapy entry point
    # ------------------------------------------------------------------

    def start_requests(self):
        for job in self.jobs:
            decision = self.gateway.check(self.source_name, self.build_search_url(job))
            if not decision.allowed:
                logger.warning(
                    "spider[%s]: job %s BLOCKED at dispatch time by compliance gateway: %s",
                    self.source_name, job.job_id, decision.reason,
                )
                self.crawler.stats.inc_value("compliance/blocked_at_dispatch")
                continue

            url = self.build_search_url(job)
            captured_payloads: list[dict] = []

            yield scrapy.Request(
                url=url,
                callback=self.parse_search_results,
                errback=self.handle_error,
                meta={
                    "playwright": True,
                    "playwright_include_page": True,
                    "playwright_page_event_handlers": {
                        # Registered BEFORE navigation completes, so this
                        # catches the XHR/fetch the page's own JS fires
                        # while performing the search — this is the
                        # interception described in the architecture doc,
                        # not a parse of the rendered HTML.
                        "response": self._make_response_interceptor(captured_payloads),
                    },
                    "playwright_page_methods": [
                        # Subclasses can override via job-specific meta if a
                        # source needs form interaction instead of a query-
                        # param URL; the common case (airline direct sites
                        # with URL-addressable search) just needs to wait for
                        # the results to actually render before we consider
                        # the interception complete.
                        PageMethod("wait_for_load_state", "networkidle"),
                    ],
                    "job": job,
                    "captured_payloads": captured_payloads,
                    "download_slot": self.source_name,  # all jobs for one source share one politeness slot
                },
            )

    # ------------------------------------------------------------------
    # Network interception
    # ------------------------------------------------------------------

    def _make_response_interceptor(self, sink: list[dict]):
        """Returns an async handler bound to `sink`, matching Playwright's
        `page.on("response", handler)` signature. Any response whose URL
        matches `fare_api_url_pattern` has its JSON body captured into
        `sink` for the callback to process once navigation settles."""

        async def _on_response(playwright_response) -> None:
            if not self.fare_api_url_pattern.search(playwright_response.url):
                return
            try:
                body = await playwright_response.json()
            except Exception as exc:  # noqa: BLE001 — not every matched response is valid JSON
                logger.debug(
                    "spider[%s]: matched URL %s but body wasn't JSON (%s) — skipping",
                    self.source_name, playwright_response.url, exc,
                )
                return
            sink.append(body)

        return _on_response

    # ------------------------------------------------------------------
    # Result handling
    # ------------------------------------------------------------------

    async def parse_search_results(self, response: Response):
        job: ScrapeJob = response.meta["job"]
        captured_payloads: list[dict] = response.meta["captured_payloads"]
        page = response.meta["playwright_page"]

        try:
            if not captured_payloads:
                logger.warning(
                    "spider[%s]: job %s completed navigation but intercepted ZERO matching "
                    "responses (pattern=%s). Either the site changed its API shape, the page "
                    "didn't fully load, or fare_api_url_pattern needs updating.",
                    self.source_name, job.job_id, self.fare_api_url_pattern.pattern,
                )
                self.crawler.stats.inc_value("extraction/zero_payloads_captured")
                return

            scrape_timestamp = datetime.now(timezone.utc)
            n_items_yielded = 0
            for payload in captured_payloads:
                for raw_item in self.parse_fare_json(payload, job):
                    raw_item.setdefault("source_type", "airline_direct")
                    raw_item.setdefault("source_name", self.source_name)
                    raw_item.setdefault("route_id", job.route_id)
                    raw_item.setdefault("advance_purchase_days", job.advance_purchase_days)
                    raw_item.setdefault("advance_purchase_window", job.advance_purchase_window)
                    raw_item.setdefault("travel_date", job.travel_date.isoformat())
                    raw_item.setdefault("scrape_timestamp", scrape_timestamp.isoformat())
                    raw_item.setdefault("scrape_batch_id", self.scrape_batch_id)
                    n_items_yielded += 1
                    yield raw_item

            logger.info(
                "spider[%s]: job %s -> %d payload(s) intercepted, %d fare item(s) extracted",
                self.source_name, job.job_id, len(captured_payloads), n_items_yielded,
            )
        finally:
            # Always close the Playwright page, even on error, to avoid
            # leaking browser contexts across a long crawl run.
            await page.close()

    async def handle_error(self, failure):
        job = failure.request.meta.get("job")
        logger.error("spider[%s]: job %s failed: %s", self.source_name, getattr(job, "job_id", "?"), failure.value)
        page = failure.request.meta.get("playwright_page")
        if page is not None:
            await page.close()
        self.crawler.stats.inc_value("requests/failed")
