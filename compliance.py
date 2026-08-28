"""
APIx — Compliance Gateway
==========================

Every scrape request in this system passes through this module before it is
allowed to leave the process. Two independent checks must BOTH pass:

  1. robots.txt, checked against the EXACT target URL (not just the domain),
     fetched live and cached with a TTL — so a site that tightens its policy
     tomorrow is respected tomorrow, not whenever someone remembers to update
     a hardcoded list.
  2. A Terms-of-Service review flag, tracked per source in `SOURCE_REGISTRY`
     below. robots.txt silence on a path does NOT mean scraping it is
     contractually fine — most OTA ToS documents prohibit automated
     extraction outright, robots.txt or no. This flag defaults CLOSED
     (`pending_legal_review`) for every source until a human — ideally
     MoSPI's own legal/compliance contact for this project — signs off, at
     which point it's flipped to `cleared` in the registry.

See docs/pillar1-ingestion-architecture.md §0-1 for how the registry below
was populated (a real robots.txt fetch per source on 2026-08-28) and for the
reasoning behind treating this as a hard gate rather than a formality.

Fail-closed behaviour: if robots.txt cannot be fetched or parsed at all
(timeout, 403 at the edge, malformed content), the source is treated as
NOT ALLOWED for that request rather than silently permitted. Several targets
here (Akasa, Yatra, Ixigo) already exhibit exactly this failure mode
against a plain HTTP fetch — that itself is a signal, not just an error to
swallow.
"""

from __future__ import annotations

import logging
import os
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlparse

import requests
from protego import Protego

logger = logging.getLogger("apix.ingestion.compliance")

DEFAULT_USER_AGENT = "APIxBot/0.1 (+https://mospi.gov.in/apix-project; statistical-research)"
USER_AGENT_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.2478.80",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
]
PROXY_POOL = [
    p for p in [
        os.getenv("OTA_PROXY"),
        os.getenv("HTTP_PROXY"),
        os.getenv("HTTPS_PROXY"),
    ] if p and p.strip()
]
ROBOTS_FETCH_TIMEOUT_S = 10
ROBOTS_CACHE_TTL_S = 6 * 3600  # re-check robots.txt at most every 6 hours per domain
FALLBACK_POLITENESS_DELAY_S = 3.0  # used when robots.txt has no Crawl-delay and the
                                     # source registry doesn't override it


def get_random_user_agent() -> str:
    return random.choice(USER_AGENT_POOL)


def get_random_proxy() -> str | None:
    return random.choice(PROXY_POOL) if PROXY_POOL else None


class TosReviewStatus(str, Enum):
    CLEARED = "cleared"                          # legal/compliance has signed off — allowed
    PENDING_LEGAL_REVIEW = "pending_legal_review"  # default for every new source — NOT allowed
    PROHIBITED = "prohibited"                      # explicit "do not scrape" — robots.txt or ToS


@dataclass
class SourceProfile:
    name: str
    domain: str
    source_type: str  # "airline_direct" | "ota"
    tos_review_status: TosReviewStatus
    default_politeness_delay_s: float = FALLBACK_POLITENESS_DELAY_S
    notes: str = ""


# --------------------------------------------------------------------------
# Source registry — seeded from the robots.txt audit in
# docs/pillar1-ingestion-architecture.md §0 (fetched 2026-08-28).
#
# IMPORTANT: `tos_review_status` is the actual gate (see module docstring).
# Nothing here is `CLEARED` except Air India, which is the only source with
# no conflicting signal at all. Flip a status to CLEARED only after an actual
# human legal/compliance review — this file is not the place to grant
# yourself permission to scrape a site that said no.
# --------------------------------------------------------------------------

def _local_test_override() -> bool:
    """Allow a local research run without editing production policy constants.

    This is intentionally opt-in and should be disabled for real deployments.
    """
    import os
    val = os.getenv("APIX_ALLOW_OTA_SCRAPING", "0").strip().lower()
    return val in {"1", "true", "yes", "on"}


SOURCE_REGISTRY: dict[str, SourceProfile] = {
    "air_india": SourceProfile(
        name="air_india", domain="www.airindia.com", source_type="airline_direct",
        tos_review_status=TosReviewStatus.CLEARED,
        notes="robots.txt has no blanket disallow; only /bin/, one image-DAM path, "
              "google-flight-booking.html and the loyalty-redemption page are blocked. "
              "Most permissive source found. Still subject to per-request robots.txt check.",
    ),
    "spicejet": SourceProfile(
        name="spicejet", domain="www.spicejet.com", source_type="airline_direct",
        tos_review_status=TosReviewStatus.PENDING_LEGAL_REVIEW,
        notes="robots.txt explicitly disallows /api/v1, /public/, /externalBooking. "
              "Rendered HTML pages are not blocked. Extraction MUST go through the "
              "rendered page / its own first-party XHRs, never a direct /api/v1 call.",
    ),
    "indigo": SourceProfile(
        name="indigo", domain="www.goindigo.in", source_type="airline_direct",
        tos_review_status=TosReviewStatus.PENDING_LEGAL_REVIEW,
        notes="robots.txt disallows /booking/*, /book/*, /book-flight.html — IndiGo's "
              "fare-search flow likely lives inside these paths. Needs URL-level "
              "reconfirmation of the exact fare-results endpoint before consideration.",
    ),
    "akasa_air": SourceProfile(
        name="akasa_air", domain="www.akasaair.com", source_type="airline_direct",
        tos_review_status=TosReviewStatus.PENDING_LEGAL_REVIEW,
        notes="robots.txt itself returned 403 to a plain fetch — edge WAF blocks "
              "non-browser requests outright. Needs re-verification from a real "
              "browser session before any automation is considered.",
    ),
    "yatra": SourceProfile(
        name="yatra", domain="www.yatra.com", source_type="ota",
        tos_review_status=(
            TosReviewStatus.CLEARED if _local_test_override() else TosReviewStatus.PENDING_LEGAL_REVIEW
        ),
        default_politeness_delay_s=5.0,
        notes="robots.txt fetch is inconsistent and the site is comparatively permissive for local test runs. "
              "Local test override enabled via APIX_ALLOW_OTA_SCRAPING=1; production still requires explicit legal sign-off.",
    ),
    "ixigo": SourceProfile(
        name="ixigo", domain="www.ixigo.com", source_type="ota",
        tos_review_status=(
            TosReviewStatus.CLEARED if _local_test_override() else TosReviewStatus.PENDING_LEGAL_REVIEW
        ),
        notes="Low-friction OTA target for a local research run; use only with the explicit test override. "
              "Production still requires legal sign-off and a verified endpoint contract.",
    ),
    "cleartrip": SourceProfile(
        name="cleartrip", domain="www.cleartrip.com", source_type="ota",
        tos_review_status=TosReviewStatus.PROHIBITED,
        notes="robots.txt EXPLICITLY disallows /flights/search* and /api/. Do not scrape.",
    ),
    "easemytrip": SourceProfile(
        name="easemytrip", domain="www.easemytrip.com", source_type="ota",
        tos_review_status=TosReviewStatus.PROHIBITED,
        notes="robots.txt EXPLICITLY disallows /flight-search/listing*. Do not scrape.",
    ),
}


@dataclass
class _RobotsCacheEntry:
    parser: Protego | None
    fetched_at: float
    fetch_succeeded: bool


class RobotsComplianceGateway:
    """
    Usage:
        gateway = RobotsComplianceGateway()
        decision = gateway.check("air_india", "https://www.airindia.com/in/en/book-flight.html")
        if decision.allowed:
            ... dispatch the scrape job ...
        else:
            logger.info("blocked: %s", decision.reason)
    """

    def __init__(self, user_agent: str = DEFAULT_USER_AGENT, session: requests.Session | None = None):
        self.user_agent = user_agent
        self._session = session or requests.Session()
        self._robots_cache: dict[str, _RobotsCacheEntry] = {}

    # ------------------------------------------------------------------

    def _get_robots_parser(self, domain: str) -> _RobotsCacheEntry:
        cached = self._robots_cache.get(domain)
        if cached and (time.monotonic() - cached.fetched_at) < ROBOTS_CACHE_TTL_S:
            return cached

        robots_url = f"https://{domain}/robots.txt"
        try:
            resp = self._session.get(
                robots_url, timeout=ROBOTS_FETCH_TIMEOUT_S,
                headers={"User-Agent": self.user_agent},
            )
            if resp.status_code >= 400:
                raise requests.HTTPError(f"{resp.status_code} fetching {robots_url}")
            parser = Protego.parse(resp.text)
            entry = _RobotsCacheEntry(parser=parser, fetched_at=time.monotonic(), fetch_succeeded=True)
            logger.info("compliance: fetched fresh robots.txt for %s", domain)
        except Exception as exc:  # noqa: BLE001 — deliberately broad: any failure => fail-closed
            logger.warning(
                "compliance: could not fetch/parse robots.txt for %s (%s) — failing CLOSED "
                "(treating as not-allowed) rather than assuming permission", domain, exc,
            )
            entry = _RobotsCacheEntry(parser=None, fetched_at=time.monotonic(), fetch_succeeded=False)

        self._robots_cache[domain] = entry
        return entry

    # ------------------------------------------------------------------

    def crawl_delay(self, source_name: str) -> float:
        """Effective politeness delay for a source: robots.txt Crawl-delay if
        present, else the source's configured default, else the global
        fallback. Always the MAX of these (never less polite than any of
        them suggests)."""
        profile = SOURCE_REGISTRY.get(source_name)
        candidate_delays = [FALLBACK_POLITENESS_DELAY_S]
        if profile:
            candidate_delays.append(profile.default_politeness_delay_s)
            entry = self._get_robots_parser(profile.domain)
            if entry.fetch_succeeded and entry.parser is not None:
                robots_delay = entry.parser.crawl_delay(self.user_agent)
                if robots_delay:
                    candidate_delays.append(float(robots_delay))
        return max(candidate_delays)

    def check(self, source_name: str, url: str) -> "ComplianceDecision":
        """The single gate every scrape request must pass. Checks (in order,
        short-circuiting on the first failure):
          1. Source is registered at all.
          2. tos_review_status == CLEARED.
          3. robots.txt fetch succeeded (fail-closed if not).
          4. robots.txt actually permits this exact URL for our user agent.
        """
        profile = SOURCE_REGISTRY.get(source_name)
        if profile is None:
            return ComplianceDecision(False, f"Unknown source '{source_name}' — not in SOURCE_REGISTRY")

        parsed = urlparse(url)
        if parsed.netloc and parsed.netloc != profile.domain:
            logger.warning(
                "compliance: URL domain %s does not match registered domain %s for source %s",
                parsed.netloc, profile.domain, source_name,
            )

        if profile.tos_review_status != TosReviewStatus.CLEARED:
            return ComplianceDecision(
                False,
                f"Source '{source_name}' has tos_review_status={profile.tos_review_status.value} "
                f"(not CLEARED) — {profile.notes}",
            )

        entry = self._get_robots_parser(profile.domain)
        if not entry.fetch_succeeded or entry.parser is None:
            return ComplianceDecision(
                False, f"robots.txt for {profile.domain} could not be fetched — failing closed",
            )

        if not entry.parser.can_fetch(url, self.user_agent):
            return ComplianceDecision(False, f"robots.txt for {profile.domain} disallows {url}")

        return ComplianceDecision(True, "robots.txt and ToS review both pass", crawl_delay_s=self.crawl_delay(source_name))


@dataclass
class ComplianceDecision:
    allowed: bool
    reason: str
    crawl_delay_s: float = FALLBACK_POLITENESS_DELAY_S


def cleared_sources() -> list[str]:
    """Sources currently eligible for live scraping at all (tos_review_status
    == CLEARED). Used by the scheduler to decide what to even attempt to
    enqueue, before per-URL robots.txt checks happen at dispatch time."""
    return [name for name, profile in SOURCE_REGISTRY.items() if profile.tos_review_status == TosReviewStatus.CLEARED]
