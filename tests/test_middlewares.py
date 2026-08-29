"""
Unit tests for middlewares.py: MissingDataTracker, the backoff delay
formula, and SessionCookieMiddleware's cookiejar assignment. No live
Scrapy crawl / network calls are involved.
"""

from __future__ import annotations

import random
import unittest
import unittest.mock
from unittest.mock import AsyncMock

from middlewares import (
    BACKOFF_JITTER_MAX_S,
    BACKOFF_JITTER_MIN_S,
    BACKOFF_MAX_DELAY_S,
    BACKOFF_MAX_RETRIES,
    HARD_BLOCK_STATUS_CODES,
    BACKOFF_RETRYABLE_STATUS_CODES,
    ExponentialBackoffMiddleware,
    MissingDataTracker,
    SessionCookieMiddleware,
    _task_context,
    compute_backoff_delay,
)


class MissingDataTrackerTests(unittest.TestCase):
    def test_starts_empty(self):
        tracker = MissingDataTracker()
        self.assertEqual(len(tracker), 0)
        self.assertEqual(tracker.summary(), {})

    def test_flag_records_an_entry(self):
        tracker = MissingDataTracker()
        tracker.flag(key="DEL-BOM", task_id="t1", source_name="yatra", reason="circuit_open")
        self.assertEqual(len(tracker), 1)
        entry = tracker.flags[0]
        self.assertEqual(entry.key, "DEL-BOM")
        self.assertEqual(entry.task_id, "t1")
        self.assertEqual(entry.source_name, "yatra")
        self.assertEqual(entry.reason, "circuit_open")

    def test_summary_groups_by_reason(self):
        tracker = MissingDataTracker()
        tracker.flag(key="DEL-BOM", task_id="t1", source_name="yatra", reason="circuit_open")
        tracker.flag(key="DEL-BLR", task_id="t2", source_name="yatra", reason="circuit_open")
        tracker.flag(key="BOM-BLR", task_id="t3", source_name="air_india", reason="navigation_failed")
        self.assertEqual(tracker.summary(), {"circuit_open": 2, "navigation_failed": 1})

    def test_flags_property_returns_a_copy(self):
        tracker = MissingDataTracker()
        tracker.flag(key="DEL-BOM", task_id="t1", source_name="yatra", reason="x")
        snapshot = tracker.flags
        snapshot.append("not-a-real-flag")
        self.assertEqual(len(tracker), 1)  # internal list unaffected by mutating the snapshot


class _FakeTaskObj:
    def __init__(self, route_id, task_id):
        self.route_id = route_id
        self.task_id = task_id


class _FakeJobObj:
    def __init__(self, route_id, job_id):
        self.route_id = route_id
        self.job_id = job_id


class _FakeRequest:
    def __init__(self, url, meta=None):
        self.url = url
        self.meta = meta or {}


class TaskContextTests(unittest.TestCase):
    def test_extracts_from_search_task_style_meta(self):
        request = _FakeRequest("https://flight.yatra.com/x", meta={"task": _FakeTaskObj("DEL-BOM", "task-1")})
        self.assertEqual(_task_context(request), ("DEL-BOM", "task-1"))

    def test_extracts_from_legacy_scrapejob_style_meta(self):
        request = _FakeRequest("https://www.airindia.com/x", meta={"job": _FakeJobObj("DEL-BOM", "job-1")})
        self.assertEqual(_task_context(request), ("DEL-BOM", "job-1"))

    def test_falls_back_to_domain_and_url_when_no_task_meta(self):
        request = _FakeRequest("https://www.example.com/search?x=1", meta={})
        self.assertEqual(_task_context(request), ("www.example.com", "https://www.example.com/search?x=1"))


class ComputeBackoffDelayTests(unittest.TestCase):
    def test_delay_grows_exponentially_with_retry_count(self):
        rng = random.Random(0)
        d0 = compute_backoff_delay(0, jitter_min_s=0, jitter_max_s=0, rng=rng)
        d1 = compute_backoff_delay(1, jitter_min_s=0, jitter_max_s=0, rng=rng)
        d2 = compute_backoff_delay(2, jitter_min_s=0, jitter_max_s=0, rng=rng)
        self.assertEqual(d0, 1.0)
        self.assertEqual(d1, 2.0)
        self.assertEqual(d2, 4.0)

    def test_delay_is_capped_at_max_delay(self):
        rng = random.Random(0)
        delay = compute_backoff_delay(20, base_delay_s=1.0, max_delay_s=60.0, jitter_min_s=0, jitter_max_s=0, rng=rng)
        self.assertEqual(delay, 60.0)

    def test_jitter_falls_within_requested_bounds(self):
        rng = random.Random(42)
        for retry_count in range(5):
            delay = compute_backoff_delay(retry_count, rng=rng)
            exponential = min(1.0 * (2 ** retry_count), BACKOFF_MAX_DELAY_S)
            jitter = delay - exponential
            self.assertGreaterEqual(jitter, BACKOFF_JITTER_MIN_S)
            self.assertLessEqual(jitter, BACKOFF_JITTER_MAX_S)

    def test_default_jitter_bounds_match_spec(self):
        self.assertEqual(BACKOFF_JITTER_MIN_S, 1.5)
        self.assertEqual(BACKOFF_JITTER_MAX_S, 4.0)


class BackoffStatusCodeTests(unittest.TestCase):
    def test_403_is_not_in_the_retryable_set(self):
        """403 is an explicit access-denial signal, not a rate-limit one —
        auto-retrying it with backoff would undermine the circuit breaker's
        'stop, don't retry-harder' stance. See middlewares.py's module
        docstring point 3."""
        self.assertNotIn(403, BACKOFF_RETRYABLE_STATUS_CODES)

    def test_429_and_503_are_retryable(self):
        self.assertIn(429, BACKOFF_RETRYABLE_STATUS_CODES)
        self.assertIn(503, BACKOFF_RETRYABLE_STATUS_CODES)

    def test_403_still_counts_toward_the_circuit_breakers_hard_block_set(self):
        """403 isn't dropped entirely — it's handled by the OTHER
        mechanism (AntiBotBackoffMiddleware's CircuitBreaker), not ignored."""
        self.assertIn(403, HARD_BLOCK_STATUS_CODES)


class _SpiderStub:
    def __init__(self, name="test_spider", source_name=None):
        self.name = name
        if source_name is not None:
            self.source_name = source_name


class SessionCookieMiddlewareTests(unittest.TestCase):
    def test_assigns_cookiejar_keyed_by_spiders_source_name(self):
        mw = SessionCookieMiddleware()
        request = _FakeRequest("https://flight.yatra.com/x")
        mw.process_request(request, _SpiderStub(source_name="yatra"))
        self.assertEqual(request.meta["cookiejar"], "yatra")

    def test_falls_back_to_spider_name_when_no_source_name(self):
        mw = SessionCookieMiddleware()
        request = _FakeRequest("https://example.com/x")
        mw.process_request(request, _SpiderStub(name="some_spider"))
        self.assertEqual(request.meta["cookiejar"], "some_spider")

    def test_does_not_overwrite_an_explicitly_assigned_cookiejar(self):
        mw = SessionCookieMiddleware()
        request = _FakeRequest("https://example.com/x", meta={"cookiejar": "custom"})
        mw.process_request(request, _SpiderStub(source_name="yatra"))
        self.assertEqual(request.meta["cookiejar"], "custom")

    def test_two_requests_to_the_same_source_share_one_jar(self):
        mw = SessionCookieMiddleware()
        r1 = _FakeRequest("https://flight.yatra.com/a")
        r2 = _FakeRequest("https://flight.yatra.com/b")
        spider = _SpiderStub(source_name="yatra")
        mw.process_request(r1, spider)
        mw.process_request(r2, spider)
        self.assertEqual(r1.meta["cookiejar"], r2.meta["cookiejar"])


class _FakeResponse:
    def __init__(self, status):
        self.status = status


class _FakeScrapyRequest:
    def __init__(self, url, meta=None):
        self.url = url
        self.meta = dict(meta or {})
        self.dont_filter = False

    def copy(self):
        return _FakeScrapyRequest(self.url, meta=dict(self.meta))


class ExponentialBackoffMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_retryable_status_passes_through_unchanged(self):
        mw = ExponentialBackoffMiddleware()
        request = _FakeScrapyRequest("https://x.com/a")
        response = _FakeResponse(200)
        result = await mw.process_response(request, response, spider=_SpiderStub())
        self.assertIs(result, response)

    async def test_403_is_not_retried_by_this_middleware(self):
        mw = ExponentialBackoffMiddleware()
        request = _FakeScrapyRequest("https://x.com/a")
        response = _FakeResponse(403)
        result = await mw.process_response(request, response, spider=_SpiderStub())
        self.assertIs(result, response)  # passes straight through to the circuit breaker, no retry

    async def test_retryable_status_returns_a_rescheduled_request(self):
        mw = ExponentialBackoffMiddleware()
        request = _FakeScrapyRequest("https://x.com/a")
        response = _FakeResponse(429)
        with unittest.mock.patch("middlewares.asyncio.sleep", new=AsyncMock()):
            result = await mw.process_response(request, response, spider=_SpiderStub())
        self.assertIsInstance(result, _FakeScrapyRequest)
        self.assertEqual(result.meta["backoff_retry_count"], 1)
        self.assertTrue(result.dont_filter)

    async def test_retry_count_increments_across_successive_calls(self):
        mw = ExponentialBackoffMiddleware()
        request = _FakeScrapyRequest("https://x.com/a", meta={"backoff_retry_count": 2})
        response = _FakeResponse(503)
        with unittest.mock.patch("middlewares.asyncio.sleep", new=AsyncMock()):
            result = await mw.process_response(request, response, spider=_SpiderStub())
        self.assertEqual(result.meta["backoff_retry_count"], 3)

    async def test_gives_up_after_max_retries_and_flags_missing_data(self):
        mw = ExponentialBackoffMiddleware()
        request = _FakeScrapyRequest("https://x.com/a", meta={"backoff_retry_count": BACKOFF_MAX_RETRIES})
        response = _FakeResponse(503)
        result = await mw.process_response(request, response, spider=_SpiderStub())
        self.assertIs(result, response)  # gives up — returned as-is for AntiBotBackoffMiddleware to record
        self.assertEqual(len(mw.missing_data), 1)
        self.assertIn("backoff_exhausted:HTTP 503", mw.missing_data.summary())


if __name__ == "__main__":
    unittest.main()
