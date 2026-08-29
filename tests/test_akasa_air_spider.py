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
    AIRPORT_FULL_NAMES,
    AkasaAirSpider,
    _ordinal_day,
    format_datepicker_label_fragment,
    parse_fare_search_response,
)


def make_task(**overrides) -> SearchTask:
    defaults = dict(
        origin="DEL", destination="BOM", departure_date=date(2026, 8, 30),
        lead_days=0, advance_purchase_window="T+0", task_id="test-task",
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


class OrdinalDayTests(unittest.TestCase):
    def test_common_suffixes(self):
        self.assertEqual(_ordinal_day(1), "1st")
        self.assertEqual(_ordinal_day(2), "2nd")
        self.assertEqual(_ordinal_day(3), "3rd")
        self.assertEqual(_ordinal_day(4), "4th")
        self.assertEqual(_ordinal_day(21), "21st")
        self.assertEqual(_ordinal_day(22), "22nd")
        self.assertEqual(_ordinal_day(23), "23rd")

    def test_teens_are_all_th(self):
        for day in (11, 12, 13):
            self.assertEqual(_ordinal_day(day), f"{day}th")


class FormatDatepickerLabelFragmentTests(unittest.TestCase):
    def test_matches_confirmed_real_format(self):
        # Confirmed against a real captured aria-label:
        # "Not available Wednesday, August 5th, 2026"
        self.assertEqual(format_datepicker_label_fragment(date(2026, 8, 5)), "August 5th, 2026")

    def test_various_dates(self):
        self.assertEqual(format_datepicker_label_fragment(date(2026, 9, 1)), "September 1st, 2026")
        self.assertEqual(format_datepicker_label_fragment(date(2026, 9, 21)), "September 21st, 2026")
        self.assertEqual(format_datepicker_label_fragment(date(2026, 12, 31)), "December 31st, 2026")


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


class AirportFullNamesTests(unittest.TestCase):
    def test_covers_the_full_route_basket(self):
        for code in ("DEL", "BOM", "BLR", "CCU", "HYD", "MAA"):
            self.assertIn(code, AIRPORT_FULL_NAMES)
            self.assertTrue(AIRPORT_FULL_NAMES[code])


class _FakeDecision:
    def __init__(self, allowed: bool, reason: str = "test"):
        self.allowed = allowed
        self.reason = reason


class _FakeGateway:
    def __init__(self, allowed: bool):
        self.allowed = allowed

    def check(self, source_name, url):
        return _FakeDecision(self.allowed)


class AkasaAirSpiderTests(unittest.TestCase):
    def test_defaults_to_full_route_date_matrix_when_no_tasks_given(self):
        spider = AkasaAirSpider()
        self.assertGreater(len(spider.tasks), 0)

    def test_accepts_explicit_task_list(self):
        tasks = [make_task()]
        spider = AkasaAirSpider(tasks=tasks)
        self.assertEqual(spider.tasks, tasks)


class AkasaAirSpiderRunTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_compliance_block_yields_nothing_and_never_touches_browser(self):
        spider = AkasaAirSpider(tasks=[make_task()])
        spider.gateway = _FakeGateway(allowed=False)

        items = [item async for item in spider._run_task(browser=None, task=spider.tasks[0])]
        self.assertEqual(items, [])
        self.assertEqual(len(spider.missing_data), 1)
        self.assertIn("compliance_blocked:test", spider.missing_data.summary())

    async def test_unmapped_airport_is_flagged_without_touching_browser(self):
        task = make_task(origin="GOI", destination="BOM")
        spider = AkasaAirSpider(tasks=[task])
        # gateway defaults to a real RobotsComplianceGateway which would
        # attempt a network call; stub it allowed so we reach the airport-
        # mapping check without any network access.
        spider.gateway = _FakeGateway(allowed=True)

        items = [item async for item in spider._run_task(browser=None, task=task)]
        self.assertEqual(items, [])
        self.assertIn("airport_name_not_mapped", spider.missing_data.summary())


if __name__ == "__main__":
    unittest.main()
