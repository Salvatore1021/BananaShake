"""
Unit tests for apixproj/spiders/akasa_air_spider.py.

parse_fare_search_response is tested against a payload shaped exactly like
a REAL captured response from https://prod-bl.qp.akasaair.com/api/ibe/
availability/search (see that module's docstring) -- not a guessed shape.
No live network/browser calls are made here.
"""

from __future__ import annotations

import unittest
from datetime import date

from app.ingestion.scheduler import SearchTask
from apixproj.spiders.akasa_air_spider import (
    AkasaAirSpider,
    build_search_request_body,
    parse_fare_search_response,
)


def make_task(**overrides) -> SearchTask:
    defaults = dict(
        origin="DEL", destination="BOM", departure_date=date(2026, 9, 6),
        lead_days=7, advance_purchase_window="T+7", task_id="test-task",
    )
    defaults.update(overrides)
    return SearchTask(**defaults)


def make_real_shaped_payload() -> dict:
    """Mirrors the real captured response structure: results -> trips ->
    journeysAvailableByMarket -> journey (designator + segments + fares),
    cross-referenced via fareAvailabilityKey into faresAvailable."""
    return {
        "data": {
            "results": [
                {
                    "trips": [
                        {
                            "date": "2026-08-30T00:00:00",
                            "journeysAvailableByMarket": [
                                {
                                    "key": "DEL|BOM",
                                    "value": [
                                        {
                                            "designator": {
                                                "origin": "DEL", "destination": "BOM",
                                                "departure": "2026-08-30T08:40:00",
                                                "arrival": "2026-08-30T11:00:00",
                                            },
                                            "flightType": "NonStop",
                                            "journeyKey": "jk1",
                                            "fares": [
                                                {"fareAvailabilityKey": "fk1", "details": [{"availableCount": 7}]},
                                            ],
                                            "segments": [
                                                {
                                                    "designator": {
                                                        "origin": "DEL", "destination": "BOM",
                                                        "departure": "2026-08-30T08:40:00",
                                                        "arrival": "2026-08-30T11:00:00",
                                                    },
                                                    "identifier": {"identifier": "1119", "carrierCode": "QP"},
                                                }
                                            ],
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                }
            ],
            "faresAvailable": [
                {
                    "key": "fk1",
                    "value": {
                        "fareAvailabilityKey": "fk1",
                        "fares": [
                            {
                                "classOfService": "R0",
                                "passengerFares": [
                                    {
                                        "fareAmount": 9717.0,
                                        "discountedFare": 7937.0,
                                        "publishedFare": 7937.0,
                                        "revenueFare": 7937.0,
                                        "passengerType": "ADT",
                                    }
                                ],
                                "productClass": "EC",
                            }
                        ],
                        "totals": {"discountedTotal": 7937.0, "fareTotal": 9717.0, "publishedTotal": 7937.0},
                    },
                }
            ],
        }
    }


class BuildSearchRequestBodyTests(unittest.TestCase):
    def test_substitutes_origin_destination_and_date_from_task(self):
        task = make_task(origin="DEL", destination="BOM", departure_date=date(2026, 9, 6))
        body = build_search_request_body(task)
        criteria = body["criteria"][0]
        self.assertEqual(criteria["stations"]["originStationCodes"], ["DEL"])
        self.assertEqual(criteria["stations"]["destinationStationCodes"], ["BOM"])
        self.assertEqual(criteria["dates"]["beginDate"], "2026-09-06T00:00:00")

    def test_passenger_and_currency_defaults(self):
        body = build_search_request_body(make_task())
        self.assertEqual(body["passengers"]["types"], [{"type": "ADT", "count": 1}])
        self.assertEqual(body["codes"]["currencyCode"], "INR")

    def test_is_json_serializable(self):
        import json

        json.dumps(build_search_request_body(make_task()))  # should not raise


class ParseFareSearchResponseTests(unittest.TestCase):
    def test_extracts_expected_fields_from_real_shaped_payload(self):
        task = make_task()
        items = parse_fare_search_response(make_real_shaped_payload(), task)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["flight_number"], "QP1119")
        self.assertEqual(item["airline_name"], "Akasa Air")
        self.assertEqual(item["carrier_code"], "QP")
        self.assertEqual(item["origin"], "DEL")
        self.assertEqual(item["destination"], "BOM")
        self.assertEqual(item["departure_time"], "2026-08-30T08:40:00")
        self.assertEqual(item["arrival_time"], "2026-08-30T11:00:00")
        self.assertEqual(item["base_fare"], 7937.0)
        self.assertEqual(item["taxes_and_fees"], 1780.0)
        self.assertEqual(item["total_fare"], 9717.0)
        self.assertEqual(item["seats_left"], 7)
        self.assertEqual(item["source_name"], "akasa_air")
        self.assertEqual(item["source_type"], "airline_direct")

    def test_base_fare_plus_taxes_equals_total(self):
        items = parse_fare_search_response(make_real_shaped_payload(), make_task())
        item = items[0]
        self.assertAlmostEqual(item["base_fare"] + item["taxes_and_fees"], item["total_fare"])

    def test_multiple_journeys_produce_multiple_items(self):
        payload = make_real_shaped_payload()
        market = payload["data"]["results"][0]["trips"][0]["journeysAvailableByMarket"][0]
        second_journey = {
            "designator": {
                "origin": "DEL", "destination": "BOM",
                "departure": "2026-08-30T09:20:00", "arrival": "2026-08-30T11:45:00",
            },
            "flightType": "NonStop",
            "journeyKey": "jk2",
            "fares": [{"fareAvailabilityKey": "fk2", "details": [{"availableCount": 10}]}],
            "segments": [{"designator": {}, "identifier": {"identifier": "1112", "carrierCode": "QP"}}],
        }
        market["value"].append(second_journey)
        payload["data"]["faresAvailable"].append({
            "key": "fk2",
            "value": {
                "fares": [{"passengerFares": [{"fareAmount": 10113.0, "discountedFare": 8719.0}]}],
            },
        })
        items = parse_fare_search_response(payload, make_task())
        self.assertEqual(len(items), 2)
        self.assertEqual({i["flight_number"] for i in items}, {"QP1119", "QP1112"})

    def test_journey_with_no_segments_is_skipped(self):
        payload = make_real_shaped_payload()
        payload["data"]["results"][0]["trips"][0]["journeysAvailableByMarket"][0]["value"][0]["segments"] = []
        items = parse_fare_search_response(payload, make_task())
        self.assertEqual(items, [])

    def test_fare_key_not_found_in_faresAvailable_is_skipped(self):
        payload = make_real_shaped_payload()
        payload["data"]["faresAvailable"] = []
        items = parse_fare_search_response(payload, make_task())
        self.assertEqual(items, [])

    def test_malformed_payload_returns_empty(self):
        self.assertEqual(parse_fare_search_response({}, make_task()), [])
        self.assertEqual(parse_fare_search_response(None, make_task()), [])
        self.assertEqual(parse_fare_search_response({"data": "not-a-dict"}, make_task()), [])


class AkasaAirSpiderTests(unittest.TestCase):
    def test_defaults_to_full_route_date_matrix_when_no_tasks_given(self):
        spider = AkasaAirSpider()
        self.assertGreater(len(spider.tasks), 0)

    def test_accepts_explicit_task_list(self):
        tasks = [make_task()]
        spider = AkasaAirSpider(tasks=tasks)
        self.assertEqual(spider.tasks, tasks)


class _FakeDecision:
    def __init__(self, allowed: bool, reason: str = "test"):
        self.allowed = allowed
        self.reason = reason


class _FakeGateway:
    def __init__(self, allowed: bool):
        self.allowed = allowed

    def check(self, source_name, url):
        return _FakeDecision(self.allowed)


class _FakeResponse:
    def __init__(self, status: int, body: dict):
        self.status = status
        self._body = body

    async def json(self):
        return self._body


class _FakeAPIRequestContext:
    def __init__(self, response: _FakeResponse):
        self.response = response
        self.calls: list[dict] = []

    async def post(self, url, headers=None, data=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "data": data})
        return self.response


class _FakeBrowserContext:
    """Stand-in for a Playwright BrowserContext exposing just the
    `.request` surface _run_task actually uses (`context.request.post`),
    so it can be exercised without any real browser/network."""

    def __init__(self, response: _FakeResponse):
        self.request = _FakeAPIRequestContext(response)

    @property
    def calls(self):
        return self.request.calls


class AkasaAirSpiderRunTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_task_yields_parsed_items_on_success(self):
        spider = AkasaAirSpider(tasks=[make_task()])
        fake_context = _FakeBrowserContext(_FakeResponse(200, make_real_shaped_payload()))

        items = [item async for item in spider._run_task(fake_context, "fake-token", spider.tasks[0])]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["flight_number"], "QP1119")
        self.assertEqual(len(fake_context.calls), 1)
        self.assertEqual(fake_context.calls[0]["headers"]["authorization"], "fake-token")

    async def test_run_task_flags_missing_data_on_non_200(self):
        spider = AkasaAirSpider(tasks=[make_task()])
        fake_context = _FakeBrowserContext(_FakeResponse(500, {}))

        items = [item async for item in spider._run_task(fake_context, "fake-token", spider.tasks[0])]
        self.assertEqual(items, [])
        self.assertIn("request_failed:RuntimeError", spider.missing_data.summary())

    async def test_run_task_flags_missing_data_on_zero_items(self):
        spider = AkasaAirSpider(tasks=[make_task()])
        fake_context = _FakeBrowserContext(_FakeResponse(200, {"data": {"results": []}}))

        items = [item async for item in spider._run_task(fake_context, "fake-token", spider.tasks[0])]
        self.assertEqual(items, [])
        self.assertIn("zero_items_extracted", spider.missing_data.summary())


if __name__ == "__main__":
    unittest.main()
