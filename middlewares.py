"""
APIx — Anti-Bot Middleware
============================

Two distinct jobs, kept deliberately separate because they serve different
purposes (see docs/pillar1-ingestion-architecture.md §1.3 for the ethical
line this module is built around):

  1. CIRCUIT BREAKER + BLOCK DETECTION (this module's actual contribution):
     recognise when a domain has started blocking us — including "soft"
     blocks that return HTTP 200 with a CAPTCHA/interstitial page instead of
     an honest 403, which naive status-code-based retry logic would never
     notice — and, on repeated failures, STOP sending it requests entirely
     for a cooldown period rather than hammering a site that has already
     said no. This is a circuit breaker in the classic sense (closed ->
     open -> half-open -> closed), not a retry-harder loop.

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
     whether it would technically work.

Exponential backoff on transient errors (timeouts, 5xx) is intentionally
NOT reimplemented here — Scrapy's own AUTOTHROTTLE (adapts delay to
observed latency/error rate) and RETRY_TIMES/RETRY_HTTP_CODES already do
this well and are enabled in BaseFareSpider.custom_settings. Reinventing
that machinery here would just create a second, worse copy of it. What
Scrapy's stack does NOT do out of the box — recognizing an HTTP-200
CAPTCHA page as a failure, and permanently backing off a domain rather than
retrying it forever — is what this module adds.
"""

from __future__ import annotations

import itertools
import logging
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum

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
    """

    def __init__(self, proxy_pool: ProxyPool | None = None):
        self.breaker = CircuitBreaker()
        self.proxy_pool = proxy_pool or ProxyPool()

    @classmethod
    def from_crawler(cls, crawler):
        proxies = crawler.settings.getlist("APIX_PROXY_POOL", [])
        return cls(proxy_pool=ProxyPool(proxies))

    def process_request(self, request, spider):
        domain = request.meta.get("download_slot") or _domain_of(request.url)

        if not self.breaker.allow_request(domain):
            logger.info("circuit_breaker: %s is OPEN — refusing to send request %s", domain, request.url)
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
                raise IgnoreRequest(f"Soft-block page detected on {domain} (matched: {block_signature})")

        self.breaker.record_success(domain)
        return response

    def process_exception(self, request, exception, spider):
        domain = request.meta.get("download_slot") or _domain_of(request.url)
        self.breaker.record_failure(domain, f"{type(exception).__name__}: {exception}")
        return None  # let Scrapy's own retry/error handling continue


def _domain_of(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(url).netloc
