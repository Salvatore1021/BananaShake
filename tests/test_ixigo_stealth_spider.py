"""
Unit tests for apixproj/spiders/ota_ixigo_stealth_spider.py.

Covers the pure logic: JSON payload parsing (against the generic assumed
shape, clearly marked unconfirmed in that module) and the compliance-gate
skip path, which returns before ever touching a Playwright Browser object.
No live network/browser calls are made here.
"""

from __future__ import annotations

import unittest
from datetime import date

from app.ingestion.scheduler import SearchTask
from apixproj.spiders.ota_ixigo_stealth_spider import (
    IxigoStealthSpider,
    _AIRPORT_CITY_NAMES,
    _to_optional_float,
    parse_fare_json,
)


def make_task(**overrides) -> SearchTask:
    defaults = dict(
        origin="DEL", destination="BOM", departure_date=date(2026, 9, 5),
        lead_days=7, advance_purchase_window="T+7", task_id="test-task",
    )
    defaults.update(overrides)
    return SearchTask(**defaults)


class ToOptionalFloatTests(unittest.TestCase):
    def test_numeric_passthrough(self):
        self.assertEqual(_to_optional_float(4400), 4400.0)

    def test_none_returns_none(self):
        self.assertIsNone(_to_optional_float(None))

    def test_garbage_returns_none(self):
        self.assertIsNone(_to_optional_float("not-a-number"))


class ParseFareJsonTests(unittest.TestCase):
    def test_extracts_from_results_key(self):
        task = make_task()
        payload = {
            "results": [
                {
                    "flightNumber": "6E202", "airlineName": "IndiGo", "carrierCode": "6E",
                    "departureTime": "09:10", "arrivalTime": "11:25",
                    "baseFare": 3100.0, "taxesAndFees": 900.0, "totalFare": 4000.0, "seatsLeft": 9,
                }
            ]
        }
        items = parse_fare_json(payload, task)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["flight_number"], "6E202")
        self.assertEqual(item["airline_name"], "IndiGo")
        self.assertEqual(item["carrier_code"], "6E")
        self.assertEqual(item["total_fare"], 4000.0)
        self.assertEqual(item["base_fare"], 3100.0)
        self.assertEqual(item["taxes_and_fees"], 900.0)
        self.assertEqual(item["seats_left"], 9)
        self.assertEqual(item["source_name"], "ixigo")
        self.assertEqual(item["source_type"], "ota")

    def test_extracts_from_flights_key(self):
        task = make_task()
        payload = {"flights": [{"flight_number": "AI101", "total_fare": 5820.0}]}
        items = parse_fare_json(payload, task)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["flight_number"], "AI101")
        self.assertEqual(items[0]["total_fare"], 5820.0)

    def test_extracts_from_data_key(self):
        task = make_task()
        payload = {"data": [{"price": 2999}]}
        items = parse_fare_json(payload, task)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["total_fare"], 2999.0)

    def test_skips_entries_without_a_recognizable_fare_field(self):
        task = make_task()
        payload = {"results": [{"flightNumber": "AI101"}, {"flightNumber": "AI102", "fare": 3000}]}
        items = parse_fare_json(payload, task)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["flight_number"], "AI102")

    def test_non_dict_or_missing_records_returns_empty(self):
        self.assertEqual(parse_fare_json({}, make_task()), [])
        self.assertEqual(parse_fare_json({"results": "not-a-list"}, make_task()), [])
        self.assertEqual(parse_fare_json(None, make_task()), [])


class AirportCityNameLookupTests(unittest.TestCase):
    def test_covers_the_full_route_basket(self):
        for code in ("DEL", "BOM", "BLR", "CCU", "HYD", "MAA"):
            self.assertIn(code, _AIRPORT_CITY_NAMES)
            self.assertTrue(_AIRPORT_CITY_NAMES[code])


class _FakeDecision:
    def __init__(self, allowed: bool, reason: str = "test"):
        self.allowed = allowed
        self.reason = reason


class _FakeGateway:
    def __init__(self, allowed: bool):
        self.allowed = allowed

    def check(self, source_name, url):
        return _FakeDecision(self.allowed)


class IxigoStealthSpiderTests(unittest.TestCase):
    def test_defaults_to_full_route_date_matrix_when_no_tasks_given(self):
        spider = IxigoStealthSpider()
        self.assertGreater(len(spider.tasks), 0)

    def test_accepts_explicit_task_list(self):
        tasks = [make_task()]
        spider = IxigoStealthSpider(tasks=tasks)
        self.assertEqual(spider.tasks, tasks)


class IxigoStealthSpiderRunTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_compliance_block_yields_nothing_and_never_touches_browser(self):
        spider = IxigoStealthSpider(tasks=[make_task()])
        spider.gateway = _FakeGateway(allowed=False)

        items = [item async for item in spider._run_task(browser=None, task=spider.tasks[0])]
        self.assertEqual(items, [])
        self.assertEqual(len(spider.missing_data), 1)
        self.assertIn("compliance_blocked:test", spider.missing_data.summary())


if __name__ == "__main__":
    unittest.main()
