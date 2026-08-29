"""
APIx — Anti-Bot Middleware
============================

Several distinct jobs, kept deliberately separate because they serve
different purposes (see docs/pillar1-ingestion-architecture.md §1.3 for the
ethical line this module is built around):

  1. CIRCUIT BREAKER + BLOCK DETECTION (this module's original
     contribution): recognise when a domain has started blocking us —
     including "soft" blocks that return HTTP 200 with a CAPTCHA/
     interstitial page instead of an honest 403, which naive status-code-
     based retry logic would never notice — and, on repeated failures, STOP
     sending it requests entirely for a cooldown period rather than
     hammering a site that has already said no. This is a circuit breaker
     in the classic sense (closed -> open -> half-open -> closed), not a
     retry-harder loop. See `AntiBotBackoffMiddleware`.

  2. PROXY ROTATION FOR LOAD DISTRIBUTION (not evasion): a small pool of
     pre-approved egress IPs is rotated round-robin across ALL requests to a
     domain, uniformly, regardless of whether any request has failed. This
     spreads legitimate, permitted request volume so no single IP alone
     trips a domain's own per-IP rate-limit heuristic under NORMAL,
     within-politeness-budget traffic. It is explicitly NOT wired to react
     to a block — when the circuit breaker opens for a domain, rotation
     doesn't kick in harder to route around it; requests simply stop. Using
     proxy rotation to defeat an active block would cross from "polite,
     distributed access" into "evading an access control decision," which
     this project's ethical-scraping requirement rules out regardless of
     whether it would technically work. See `ProxyPool`.

  3. EXPONENTIAL BACKOFF + JITTER ON TRANSIENT RATE-LIMITING
     (`ExponentialBackoffMiddleware`): HTTP 429 and 503 specifically mean
     "you're going too fast, or I'm overloaded right now" — a real client
     coming back a little later, a little slower, is exactly the correct
     and expected response, so these two codes get a genuine retry with
     exponentially growing delay plus random jitter (1.5-4.0s) so retries
     don't all land in lockstep. HTTP 403 is DELIBERATELY EXCLUDED from
     this retry set: 403 means "no," not "not yet" — retrying an explicit
     denial with backoff would just be the circuit breaker's "retry-harder
     loop" relocated to a different module, undermining job #1 above. A 403
     is handled purely by the circuit breaker: it counts toward that
     domain's failure threshold and, once tripped, stops requests to it
     entirely. (This intentionally departs from this module's earlier
     stance that Scrapy's own AUTOTHROTTLE/RETRY_HTTP_CODES made a custom
     backoff mechanism redundant — AutoThrottle adapts to *observed
     latency*, not a deterministic exponential-plus-jitter schedule keyed
     to specific status codes, which is what's needed here.)

  4. SESSION COOKIE CONTINUITY (`SessionCookieMiddleware`): groups every
     request to one source into a single Scrapy cookiejar, so cookies set
     on an early request (e.g. a landing/search page) carry forward to the
     requests that follow it, the same way a real browser tab would. This
     is ordinary client behaviour, not evasion — a spider whose every
     request looks like a cookie-less first visit is the unusual case, not
     the normal one.

  5. MISSING-DATA FLAGGING (`MissingDataTracker`): whenever any of the
     above gives up on a request for good (circuit open, soft-block
     detected, backoff exhausted), the (route, task, reason) is recorded
     here instead of just logged and forgotten, so a run's coverage gaps
     are a queryable end-of-run artifact — "6 of 30 tasks missing, all on
     source X, all rate-limited" — rather than something you'd have to
     reconstruct from scattered WARNING lines afterward. A skipped task
     never crashes the batch; it's recorded and the crawl moves on to the
     next one.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import random
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum

from scrapy import signals
from scrapy.exceptions import IgnoreRequest

logger = logging.getLogger("apix.ingestion.middlewares")

# Status codes treated as an explicit block signal (as distinct from a
# transient server error that Scrapy's RETRY_HTTP_CODES / AutoThrottle
# already handle on their own).
HARD_BLOCK_STATUS_CODES = {403, 429, 451}

# Signatures of a "soft" block — a page returned with HTTP 200 that is
# actually a CAPTCHA / interstitial / WAF challenge page, not real content.
# Checked against the response body (first 20KB only, for speed) whenever a
# response's content looks like HTML. This list is intentionally generic
# (common phrasing across Cloudflare, Akamai, PerimeterX/HUMAN, and generic
# "are you human" interstitials) rather than site-specific, since a soft
# block page is exactly the kind of thing that changes without notice.
SOFT_BLOCK_SIGNATURES = [
    re.compile(p, re.IGNORECASE) for p in [
        r"attention required.{0,20}cloudflare",
        r"checking your browser before accessing",
        r"cf-browser-verification",
        r"captcha",
        r"pardon our interruption",
        r"unusual traffic from your (computer|network)",
        r"please verify you are a human",
        r"access denied",
        r"request unsuccessful.{0,20}incapsula",
        r"perimeterx",
    ]
]

CIRCUIT_FAILURE_THRESHOLD = 4        # failures within the window that trip the breaker
CIRCUIT_FAILURE_WINDOW_S = 300       # 5 minutes
CIRCUIT_COOLDOWN_S = 30 * 60         # stay OPEN for 30 minutes before allowing a half-open trial
CIRCUIT_HALF_OPEN_TRIAL_LIMIT = 1    # only one probe request allowed through while half-open


class CircuitState(str, Enum):
    CLOSED = "closed"        # normal operation
    OPEN = "open"             # blocking all requests to this domain
    HALF_OPEN = "half_open"   # allowing one probe request through to test recovery


@dataclass
class _DomainCircuit:
    state: CircuitState = CircuitState.CLOSED
    failure_timestamps: deque = field(default_factory=deque)
    opened_at: float | None = None
    half_open_trials_used: int = 0

    def record_failure(self, now: float) -> None:
        self.failure_timestamps.append(now)
        while self.failure_timestamps and now - self.failure_timestamps[0] > CIRCUIT_FAILURE_WINDOW_S:
            self.failure_timestamps.popleft()

    def failures_in_window(self, now: float) -> int:
        while self.failure_timestamps and now - self.failure_timestamps[0] > CIRCUIT_FAILURE_WINDOW_S:
            self.failure_timestamps.popleft()
        return len(self.failure_timestamps)


class CircuitBreaker:
    """Per-domain circuit breaker. Not thread-safe by design — Scrapy's
    downloader middleware chain runs in a single reactor thread, so a plain
    dict is sufficient and avoids the overhead/complexity of locking."""

    def __init__(
        self,
        failure_threshold: int = CIRCUIT_FAILURE_THRESHOLD,
        cooldown_s: float = CIRCUIT_COOLDOWN_S,
    ):
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self._circuits: dict[str, _DomainCircuit] = defaultdict(_DomainCircuit)

    def allow_request(self, domain: str) -> bool:
        circuit = self._circuits[domain]
        now = time.monotonic()

        if circuit.state == CircuitState.OPEN:
            if circuit.opened_at is not None and (now - circuit.opened_at) >= self.cooldown_s:
                circuit.state = CircuitState.HALF_OPEN
                circuit.half_open_trials_used = 0
                logger.warning(
                    "circuit_breaker: %s cooldown elapsed (%.0fs) — moving to HALF_OPEN, "
                    "allowing %d probe request(s)", domain, self.cooldown_s, CIRCUIT_HALF_OPEN_TRIAL_LIMIT,
                )
            else:
                return False

        if circuit.state == CircuitState.HALF_OPEN:
            if circuit.half_open_trials_used >= CIRCUIT_HALF_OPEN_TRIAL_LIMIT:
                return False
            circuit.half_open_trials_used += 1

        return True

    def record_success(self, domain: str) -> None:
        circuit = self._circuits[domain]
        if circuit.state in (CircuitState.HALF_OPEN, CircuitState.OPEN):
            logger.info("circuit_breaker: %s recovered — closing circuit", domain)
        circuit.state = CircuitState.CLOSED
        circuit.failure_timestamps.clear()
        circuit.opened_at = None
        circuit.half_open_trials_used = 0

    def record_failure(self, domain: str, reason: str) -> None:
        circuit = self._circuits[domain]
        now = time.monotonic()

        if circuit.state == CircuitState.HALF_OPEN:
            # The probe request also failed — the site is still blocking us.
            # Re-open immediately without waiting for the failure threshold;
            # a single failed probe after a full cooldown is decisive enough.
            circuit.state = CircuitState.OPEN
            circuit.opened_at = now
            logger.error(
                "circuit_breaker: %s HALF_OPEN probe failed (%s) — re-opening circuit for another %.0fs. "
                "This domain has now failed to recover after a full cooldown cycle; if this keeps "
                "happening it should be escalated to a human (site may have permanently changed its "
                "posture toward automated access — see compliance.SOURCE_REGISTRY tos_review_status).",
                domain, reason, self.cooldown_s,
            )
            return

        circuit.record_failure(now)
        n_failures = circuit.failures_in_window(now)
        logger.warning("circuit_breaker: %s failure recorded (%s) — %d/%d in window", domain, reason, n_failures, self.failure_threshold)

        if n_failures >= self.failure_threshold and circuit.state == CircuitState.CLOSED:
            circuit.state = CircuitState.OPEN
            circuit.opened_at = now
            logger.error(
                "circuit_breaker: %s tripped OPEN after %d failures in %.0fs — no further requests "
                "to this domain for %.0fs. This is the correct response to a source actively "
                "blocking us: STOP, not retry-harder or rotate identity to route around it.",
                domain, n_failures, CIRCUIT_FAILURE_WINDOW_S, self.cooldown_s,
            )


def looks_like_soft_block(body_text: str) -> str | None:
    """Returns the matched signature description if `body_text` looks like a
    CAPTCHA/interstitial page rather than real content, else None."""
    sample = body_text[:20_000]
    for pattern in SOFT_BLOCK_SIGNATURES:
        if pattern.search(sample):
            return pattern.pattern
    return None


@dataclass(frozen=True)
class MissingDataFlag:
    key: str          # route_id this gap applies to (or a domain, if no task context was available)
    task_id: str      # SearchTask.task_id / legacy ScrapeJob.job_id, or the raw URL as a last resort
    source_name: str
    reason: str
    flagged_at: float  # time.monotonic() timestamp


class MissingDataTracker:
    """Accumulates every route/task a crawl gave up on, so a run's gaps are
    a queryable, reportable artifact instead of something reconstructed
    from scattered WARNING log lines afterward.

    Deliberately a plain, dependency-free class (not tied to Scrapy) so it
    can be shared by both the downloader middlewares below AND a spider
    that manages its own client outside Scrapy's downloader entirely (e.g.
    a Playwright-driven fallback) — see
    apixproj/spiders/air_india_stealth_spider.py, which calls `.flag()`
    directly from its own per-task exception handling.
    """

    def __init__(self):
        self._flags: list[MissingDataFlag] = []

    def flag(self, *, key: str, task_id: str, source_name: str, reason: str) -> None:
        entry = MissingDataFlag(
            key=key, task_id=task_id, source_name=source_name, reason=reason, flagged_at=time.monotonic(),
        )
        self._flags.append(entry)
        logger.warning(
            "missing_data: flagged %s (task=%s, source=%s): %s", key, task_id, source_name, reason,
        )

    @property
    def flags(self) -> list[MissingDataFlag]:
        return list(self._flags)

    def summary(self) -> dict[str, int]:
        """reason -> count of flags with that reason, for an end-of-run log line."""
        counts: dict[str, int] = defaultdict(int)
        for entry in self._flags:
            counts[entry.reason] += 1
        return dict(counts)

    def __len__(self) -> int:
        return len(self._flags)


def _task_context(request) -> tuple[str, str]:
    """Best-effort (route_id, task_id) for a Scrapy request, read from
    whichever job/task object a spider attached to `request.meta` —
    SearchTask uses `route_id`/`task_id`, the legacy ScrapeJob uses
    `route_id`/`job_id`. Falls back to (domain, url) when neither is
    present, so a flag is still useful even for a spider that doesn't
    attach a task object."""
    for meta_key in ("task", "job"):
        obj = request.meta.get(meta_key)
        if obj is None:
            continue
        route_id = getattr(obj, "route_id", None)
        if route_id:
            task_id = getattr(obj, "task_id", None) or getattr(obj, "job_id", None) or request.url
            return route_id, task_id
    return _domain_of(request.url), request.url


class ProxyPool:
    """Round-robin rotation across a small, pre-approved proxy pool, applied
    uniformly to every request for LOAD DISTRIBUTION — see module docstring.
    An empty pool (the default) means "no proxy, direct connection," which
    is the correct default until a specific, approved proxy provider is
    contracted for this project; this class is provided so wiring one in
    later is a config change, not a code change.
    """

    def __init__(self, proxies: list[str] | None = None):
        self._proxies = proxies or []
        self._cycle = itertools.cycle(self._proxies) if self._proxies else None

    def next_proxy(self) -> str | None:
        if self._cycle is None:
            return None
        return next(self._cycle)


class AntiBotBackoffMiddleware:
    """Scrapy downloader middleware wiring CircuitBreaker + soft-block
    detection + (optional) proxy rotation into the request/response cycle.
    Every request this middleware permanently gives up on (circuit open,
    soft-block detected) is recorded in `self.missing_data` — see
    MissingDataTracker.
    """

    def __init__(self, proxy_pool: ProxyPool | None = None, missing_data: MissingDataTracker | None = None):
        self.breaker = CircuitBreaker()
        self.proxy_pool = proxy_pool or ProxyPool()
        self.missing_data = missing_data or MissingDataTracker()

    @classmethod
    def from_crawler(cls, crawler):
        proxies = crawler.settings.getlist("APIX_PROXY_POOL", [])
        instance = cls(proxy_pool=ProxyPool(proxies))
        crawler.signals.connect(instance._log_missing_data_summary, signal=signals.spider_closed)
        return instance

    def process_request(self, request, spider):
        domain = request.meta.get("download_slot") or _domain_of(request.url)

        if not self.breaker.allow_request(domain):
            logger.info("circuit_breaker: %s is OPEN — refusing to send request %s", domain, request.url)
            route_id, task_id = _task_context(request)
            self.missing_data.flag(key=route_id, task_id=task_id, source_name=domain, reason="circuit_open")
            raise IgnoreRequest(f"Circuit open for domain {domain}")

        proxy = self.proxy_pool.next_proxy()
        if proxy:
            request.meta["proxy"] = proxy
        return None

    def process_response(self, request, response, spider):
        domain = request.meta.get("download_slot") or _domain_of(request.url)

        if response.status in HARD_BLOCK_STATUS_CODES:
            self.breaker.record_failure(domain, f"HTTP {response.status}")
            return response  # let RETRY_HTTP_CODES / RetryMiddleware decide whether to retry

        content_type = response.headers.get("Content-Type", b"").decode("utf-8", "ignore")
        if "html" in content_type.lower():
            try:
                body_text = response.text
            except (AttributeError, UnicodeDecodeError):
                body_text = ""
            block_signature = looks_like_soft_block(body_text)
            if block_signature:
                self.breaker.record_failure(domain, f"soft-block signature: {block_signature}")
                route_id, task_id = _task_context(request)
                self.missing_data.flag(
                    key=route_id, task_id=task_id, source_name=domain, reason=f"soft_block:{block_signature}",
                )
                raise IgnoreRequest(f"Soft-block page detected on {domain} (matched: {block_signature})")

        self.breaker.record_success(domain)
        return response

    def process_exception(self, request, exception, spider):
        domain = request.meta.get("download_slot") or _domain_of(request.url)
        self.breaker.record_failure(domain, f"{type(exception).__name__}: {exception}")
        return None  # let Scrapy's own retry/error handling continue

    def _log_missing_data_summary(self, spider, reason):
        if self.missing_data:
            logger.info(
                "circuit_breaker: spider '%s' finished with %d missing-data flag(s): %s",
                spider.name, len(self.missing_data), self.missing_data.summary(),
            )


# Codes worth an automatic backoff-and-retry — see module docstring point 3
# for why 403 is deliberately NOT in this set.
BACKOFF_RETRYABLE_STATUS_CODES = {429, 503}
BACKOFF_BASE_DELAY_S = 1.0
BACKOFF_MAX_DELAY_S = 60.0
BACKOFF_MAX_RETRIES = 4
BACKOFF_JITTER_MIN_S = 1.5
BACKOFF_JITTER_MAX_S = 4.0


def compute_backoff_delay(
    retry_count: int,
    *,
    base_delay_s: float = BACKOFF_BASE_DELAY_S,
    max_delay_s: float = BACKOFF_MAX_DELAY_S,
    jitter_min_s: float = BACKOFF_JITTER_MIN_S,
    jitter_max_s: float = BACKOFF_JITTER_MAX_S,
    rng: random.Random | None = None,
) -> float:
    """Exponential backoff (doubling per attempt, capped at max_delay_s)
    plus a random jitter in [jitter_min_s, jitter_max_s] — pulled out as a
    standalone function so the delay math is unit-testable without an
    event loop or a real Scrapy request/response."""
    rng = rng or random
    exponential = min(base_delay_s * (2 ** retry_count), max_delay_s)
    jitter = rng.uniform(jitter_min_s, jitter_max_s)
    return exponential + jitter


class ExponentialBackoffMiddleware:
    """Downloader middleware: on HTTP 429/503, retries the SAME request
    with exponential backoff plus random jitter (see compute_backoff_delay)
    rather than either giving up immediately or hammering the source again
    right away. Gives up after BACKOFF_MAX_RETRIES attempts and lets the
    final response continue up the chain, where AntiBotBackoffMiddleware's
    CircuitBreaker takes over.

    Must sit CLOSER TO THE DOWNLOADER than AntiBotBackoffMiddleware in
    DOWNLOADER_MIDDLEWARES (a higher priority number) — process_response
    runs downloader-side first, so this needs to see (and absorb) 429/503
    responses BEFORE the circuit breaker counts them as failures; the
    breaker should only ever see the *final*, retries-exhausted outcome,
    not every intermediate rate-limited attempt.
    """

    def __init__(self, missing_data: MissingDataTracker | None = None):
        self.missing_data = missing_data or MissingDataTracker()

    @classmethod
    def from_crawler(cls, crawler):
        instance = cls()
        crawler.signals.connect(instance._log_missing_data_summary, signal=signals.spider_closed)
        return instance

    async def process_response(self, request, response, spider):
        if response.status not in BACKOFF_RETRYABLE_STATUS_CODES:
            return response

        retry_count = request.meta.get("backoff_retry_count", 0)
        domain = _domain_of(request.url)

        if retry_count >= BACKOFF_MAX_RETRIES:
            route_id, task_id = _task_context(request)
            self.missing_data.flag(
                key=route_id, task_id=task_id, source_name=domain,
                reason=f"backoff_exhausted:HTTP {response.status}",
            )
            logger.error(
                "backoff: %s — gave up after %d retries on HTTP %d for %s",
                domain, retry_count, response.status, request.url,
            )
            return response  # let AntiBotBackoffMiddleware record the final failure

        delay = compute_backoff_delay(retry_count)
        logger.warning(
            "backoff: HTTP %d from %s — retry %d/%d in %.1fs",
            response.status, request.url, retry_count + 1, BACKOFF_MAX_RETRIES, delay,
        )
        await asyncio.sleep(delay)

        new_request = request.copy()
        new_request.meta["backoff_retry_count"] = retry_count + 1
        new_request.dont_filter = True
        return new_request

    def _log_missing_data_summary(self, spider, reason):
        if self.missing_data:
            logger.info(
                "backoff: spider '%s' finished with %d request(s) permanently rate-limited/unavailable: %s",
                spider.name, len(self.missing_data), self.missing_data.summary(),
            )


class SessionCookieMiddleware:
    """Groups every request to one source into a single Scrapy cookiejar —
    Scrapy's own CookiesMiddleware persists cookies per-jar across requests
    that share a `cookiejar` meta value — so a spider's requests to one
    source look like one continuous browsing session (cookies set on a
    landing/search page carry forward to the requests that follow it)
    rather than each request arriving cookie-less like a fresh incognito
    tab every time. This is ordinary client behaviour, not evasion: a real
    browser tab always does this.

    Must run BEFORE Scrapy's built-in CookiesMiddleware (priority 700 in
    DOWNLOADER_MIDDLEWARES_BASE) so the cookiejar assignment is in place
    before cookie handling runs — give this a priority well under 700.
    """

    def __init__(self):
        self._seen_jars: set[str] = set()

    def process_request(self, request, spider):
        if "cookiejar" in request.meta:
            return None  # spider already assigned one explicitly — respect it

        jar_key = getattr(spider, "source_name", None) or spider.name
        if jar_key not in self._seen_jars:
            self._seen_jars.add(jar_key)
            logger.info("session: starting a new cookie session for '%s'", jar_key)

        request.meta["cookiejar"] = jar_key
        return None


def _domain_of(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(url).netloc
