"""
Unit tests for apixproj/spiders/ota_yatra_spider.py.

Covers the pure logic: header realism, searchId extraction, JSON payload
parsing, and compliance-gate skip behaviour. No real network/Playwright
calls are made — a fake compliance gateway is injected where needed.
"""

from __future__ import annotations

import unittest
import unittest.mock
from datetime import date

from app.ingestion.scheduler import SearchTask
from apixproj.spiders.ota_yatra_spider import (
    PRICE_ENDPOINT_TEMPLATE,
    SEARCH_RESULTS_URL_TEMPLATE,
    YatraFareSpider,
    _extract_search_id,
    _to_float,
    build_realistic_headers,
    extract_yatra_fare_items,
)


def make_task(**overrides) -> SearchTask:
    defaults = dict(
        origin="DEL", destination="BOM", departure_date=date(2026, 9, 5),
        lead_days=7, advance_purchase_window="T+7", task_id="test-task",
    )
    defaults.update(overrides)
    return SearchTask(**defaults)


class BuildRealisticHeadersTests(unittest.TestCase):
    def test_includes_core_browser_headers(self):
        headers = build_realistic_headers(referer="https://www.yatra.com/")
        for key in ("User-Agent", "Accept", "Accept-Language", "Accept-Encoding", "Referer"):
            self.assertIn(key, headers)
        self.assertEqual(headers["Referer"], "https://www.yatra.com/")

    def test_chromium_family_ua_gets_sec_ch_ua_headers(self):
        chrome_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        with unittest.mock.patch("apixproj.spiders.ota_yatra_spider.get_random_user_agent", return_value=chrome_ua):
            headers = build_realistic_headers(referer="https://www.yatra.com/")
        self.assertEqual(headers["User-Agent"], chrome_ua)
        self.assertIn("Sec-Ch-Ua", headers)
        self.assertIn("Sec-Ch-Ua-Mobile", headers)
        self.assertIn("Sec-Ch-Ua-Platform", headers)

    def test_firefox_ua_gets_no_sec_ch_ua_headers(self):
        firefox_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0"
        with unittest.mock.patch("apixproj.spiders.ota_yatra_spider.get_random_user_agent", return_value=firefox_ua):
            headers = build_realistic_headers(referer="https://www.yatra.com/")
        self.assertNotIn("Sec-Ch-Ua", headers)

    def test_origin_header_adds_xhr_markers(self):
        headers = build_realistic_headers(referer="https://www.yatra.com/", origin_header="https://flight.yatra.com")
        self.assertEqual(headers["Origin"], "https://flight.yatra.com")
        self.assertEqual(headers["X-Requested-With"], "XMLHttpRequest")

    def test_no_origin_header_by_default(self):
        headers = build_realistic_headers(referer="https://www.yatra.com/")
        self.assertNotIn("Origin", headers)
        self.assertNotIn("X-Requested-With", headers)


class ExtractSearchIdTests(unittest.TestCase):
    def test_extracts_search_id_from_inline_json(self):
        html = '<script>window.__STATE__={"searchId":"614b8271-37a1-4edc-9102-38fb5ee2429a","other":1}</script>'
        self.assertEqual(_extract_search_id(html), "614b8271-37a1-4edc-9102-38fb5ee2429a")

    def test_returns_none_when_absent(self):
        self.assertIsNone(_extract_search_id("<html><body>no search id here</body></html>"))


class ToFloatTests(unittest.TestCase):
    def test_numeric_passthrough(self):
        self.assertEqual(_to_float(4400), 4400.0)
        self.assertEqual(_to_float(4400.5), 4400.5)

    def test_parses_currency_formatted_string(self):
        self.assertEqual(_to_float("₹4,400.50"), 4400.50)

    def test_none_and_garbage_return_none(self):
        self.assertIsNone(_to_float(None))
        self.assertIsNone(_to_float("not-a-number"))


class ExtractYatraFareItemsTests(unittest.TestCase):
    def setUp(self):
        self.task = make_task()
        self.payload = {
            "data": {
                "fltSchedule": {
                    "route1": [
                        {
                            "ID": "route1",
                            "OD": [
                                {
                                    "tdu": "06:00",
                                    "FS": [
                                        {
                                            "fid": "f1",
                                            "fnum": "AI101",
                                            "ac": "AI",
                                            "acn": "Air India",
                                            "dd": "06:05",
                                            "ad": "08:20",
                                            "seatsLeft": 4,
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                },
                "fareDetails": {
                    "route1": {
                        "f1": {"O": {"ADT": {"bf": 4400, "tf": 1420, "ftf": 5820}}},
                    }
                },
                "airlineNames": {"AI": "Air India"},
            }
        }

    def test_extracts_one_item_with_expected_fields(self):
        items = extract_yatra_fare_items(self.payload, self.task)
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
        self.assertEqual(item["lead_window"], "T+7")
        self.assertEqual(item["source_name"], "yatra")
        self.assertEqual(item["source_type"], "ota")
        self.assertIn("scraped_at_timestamp", item)

    def test_falls_back_to_base_plus_taxes_when_total_missing(self):
        del self.payload["data"]["fareDetails"]["route1"]["f1"]["O"]["ADT"]["ftf"]
        items = extract_yatra_fare_items(self.payload, self.task)
        self.assertEqual(items[0]["total_fare"], 4400.0 + 1420.0)

    def test_malformed_payload_returns_empty_list(self):
        self.assertEqual(extract_yatra_fare_items({}, self.task), [])
        self.assertEqual(extract_yatra_fare_items({"data": "not-a-dict"}, self.task), [])
        self.assertEqual(extract_yatra_fare_items(None, self.task), [])

    def test_no_schedule_entries_returns_empty_list(self):
        self.assertEqual(extract_yatra_fare_items({"data": {"fltSchedule": {}}}, self.task), [])


class _FakeDecision:
    def __init__(self, allowed: bool, reason: str = "test"):
        self.allowed = allowed
        self.reason = reason


class _FakeGateway:
    def __init__(self, allowed: bool):
        self.allowed = allowed
        self.checked_urls: list[str] = []

    def check(self, source_name, url):
        self.checked_urls.append(url)
        return _FakeDecision(self.allowed)


class YatraFareSpiderDispatchTests(unittest.TestCase):
    def test_defaults_to_full_route_date_matrix_when_no_tasks_given(self):
        spider = YatraFareSpider()
        self.assertGreater(len(spider.tasks), 0)

    def test_accepts_explicit_task_list(self):
        tasks = [make_task()]
        spider = YatraFareSpider(tasks=tasks)
        self.assertEqual(spider.tasks, tasks)

    def test_start_requests_skips_all_tasks_when_compliance_blocks(self):
        spider = YatraFareSpider(tasks=[make_task()])
        spider.gateway = _FakeGateway(allowed=False)
        requests = list(spider.start_requests())
        self.assertEqual(requests, [])

    def test_start_requests_builds_request_for_search_results_url_when_allowed(self):
        task = make_task()
        spider = YatraFareSpider(tasks=[task])
        spider.gateway = _FakeGateway(allowed=True)
        requests = list(spider.start_requests())
        self.assertEqual(len(requests), 1)
        expected_url = SEARCH_RESULTS_URL_TEMPLATE.format(
            origin=task.origin, destination=task.destination, departure_date=task.departure_date.isoformat(),
        )
        self.assertEqual(requests[0].url, expected_url)
        self.assertIs(requests[0].meta["task"], task)

    def test_price_endpoint_template_accepts_search_id(self):
        url = PRICE_ENDPOINT_TEMPLATE.format(search_id="abc-123")
        self.assertIn("searchId=abc-123", url)
        self.assertIn("msid=abc-123", url)


if __name__ == "__main__":
    unittest.main()
