"""
Unit tests for apixproj/spiders/air_india_stealth_spider.py.

Covers the pure logic: search-URL construction, JSON payload parsing, and
the compliance-gate skip path (which returns before ever touching a
Playwright Browser object, so it's testable without a real browser).
"""

from __future__ import annotations

import unittest
from datetime import date

from app.ingestion.scheduler import SearchTask
from apixproj.spiders.air_india_stealth_spider import (
    SEARCH_URL_TEMPLATE,
    AirIndiaStealthSpider,
    build_search_url,
    parse_fare_json,
)


def make_task(**overrides) -> SearchTask:
    defaults = dict(
        origin="DEL", destination="BOM", departure_date=date(2026, 9, 5),
        lead_days=1, advance_purchase_window="T+1", task_id="test-task",
    )
    defaults.update(overrides)
    return SearchTask(**defaults)


class BuildSearchUrlTests(unittest.TestCase):
    def test_interpolates_task_fields(self):
        task = make_task(origin="DEL", destination="BOM", departure_date=date(2026, 9, 5))
        url = build_search_url(task)
        expected = SEARCH_URL_TEMPLATE.format(origin="DEL", destination="BOM", departure_date="2026-09-05")
        self.assertEqual(url, expected)
        self.assertIn("from=DEL", url)
        self.assertIn("to=BOM", url)
        self.assertIn("date=2026-09-05", url)


class ParseFareJsonTests(unittest.TestCase):
    def test_extracts_expected_fields(self):
        task = make_task()
        payload = {
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
                }
            ]
        }
        items = parse_fare_json(payload, task)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["flight_number"], "AI101")
        self.assertEqual(item["airline_name"], "Air India")
        self.assertEqual(item["carrier_code"], "AI")
        self.assertEqual(item["origin"], "DEL")
        self.assertEqual(item["destination"], "BOM")
        self.assertEqual(item["departure_time"], "06:05")
        self.assertEqual(item["arrival_time"], "08:20")
        self.assertEqual(item["base_fare"], 4400.0)
        self.assertEqual(item["taxes_and_fees"], 1420.0)
        self.assertEqual(item["total_fare"], 5820.0)
        self.assertEqual(item["seats_left"], 4)
        self.assertEqual(item["lead_window"], "T+1")
        self.assertEqual(item["source_name"], "air_india")
        self.assertEqual(item["source_type"], "airline_direct")

    def test_defaults_airline_name_when_absent(self):
        task = make_task()
        payload = {"results": [{"flightNumber": "AI101", "totalFare": 5820.0}]}
        items = parse_fare_json(payload, task)
        self.assertEqual(items[0]["airline_name"], "Air India")

    def test_skips_entries_missing_total_fare(self):
        task = make_task()
        payload = {"results": [{"flightNumber": "AI101"}, {"flightNumber": "AI102", "totalFare": 1000.0}]}
        items = parse_fare_json(payload, task)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["flight_number"], "AI102")

    def test_non_list_results_returns_empty(self):
        self.assertEqual(parse_fare_json({"results": "not-a-list"}, make_task()), [])
        self.assertEqual(parse_fare_json({}, make_task()), [])


class _FakeDecision:
    def __init__(self, allowed: bool, reason: str = "test"):
        self.allowed = allowed
        self.reason = reason


class _FakeGateway:
    def __init__(self, allowed: bool):
        self.allowed = allowed

    def check(self, source_name, url):
        return _FakeDecision(self.allowed)


class AirIndiaStealthSpiderTests(unittest.TestCase):
    def test_defaults_to_full_route_date_matrix_when_no_tasks_given(self):
        spider = AirIndiaStealthSpider()
        self.assertGreater(len(spider.tasks), 0)

    def test_accepts_explicit_task_list(self):
        tasks = [make_task()]
        spider = AirIndiaStealthSpider(tasks=tasks)
        self.assertEqual(spider.tasks, tasks)


class AirIndiaStealthSpiderRunTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_compliance_block_yields_nothing_and_never_touches_browser(self):
        spider = AirIndiaStealthSpider(tasks=[make_task()])
        spider.gateway = _FakeGateway(allowed=False)

        items = [item async for item in spider._run_task(browser=None, task=spider.tasks[0])]
        self.assertEqual(items, [])


if __name__ == "__main__":
    unittest.main()
