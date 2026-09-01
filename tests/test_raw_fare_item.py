"""Unit tests for the canonical raw fare item shape (apixproj/raw_fare_item.py)."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from apixproj.raw_fare_item import RAW_FARE_ITEM_FIELDS, build_raw_fare_item


class BuildRawFareItemTests(unittest.TestCase):
    def test_returns_all_canonical_fields(self):
        item = build_raw_fare_item(
            flight_number="AI101", airline_name="Air India", carrier_code="AI",
            origin="DEL", destination="BOM", departure_time="06:05", arrival_time="08:20",
            base_fare=4400.0, taxes_and_fees=1420.0, total_fare=5820.0, seats_left=4,
            lead_window="T+7", source_name="air_india", source_type="airline_direct",
        )
        self.assertEqual(set(item.keys()), set(RAW_FARE_ITEM_FIELDS))

    def test_route_id_is_derived_from_origin_and_destination(self):
        item = build_raw_fare_item(
            flight_number=None, airline_name=None, carrier_code=None,
            origin="DEL", destination="BOM", departure_time=None, arrival_time=None,
            base_fare=None, taxes_and_fees=None, total_fare=None, seats_left=None,
            lead_window="T+1", source_name="x", source_type="ota",
        )
        self.assertEqual(item["route_id"], "DEL-BOM")

    def test_scraped_at_defaults_to_now_utc_when_omitted(self):
        before = datetime.now(timezone.utc)
        item = build_raw_fare_item(
            flight_number=None, airline_name=None, carrier_code=None,
            origin="DEL", destination="BOM", departure_time=None, arrival_time=None,
            base_fare=None, taxes_and_fees=None, total_fare=None, seats_left=None,
            lead_window="T+1", source_name="x", source_type="ota",
        )
        after = datetime.now(timezone.utc)
        parsed = datetime.fromisoformat(item["scraped_at_timestamp"])
        self.assertTrue(before <= parsed <= after)

    def test_scraped_at_is_injectable_for_deterministic_tests(self):
        fixed = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
        item = build_raw_fare_item(
            flight_number=None, airline_name=None, carrier_code=None,
            origin="DEL", destination="BOM", departure_time=None, arrival_time=None,
            base_fare=None, taxes_and_fees=None, total_fare=None, seats_left=None,
            lead_window="T+1", source_name="x", source_type="ota", scraped_at=fixed,
        )
        self.assertEqual(item["scraped_at_timestamp"], fixed.isoformat())

    def test_field_values_pass_through_unmodified(self):
        item = build_raw_fare_item(
            flight_number="6E202", airline_name="IndiGo", carrier_code="6E",
            origin="DEL", destination="BLR", departure_time="10:00", arrival_time="12:45",
            base_fare=3000.5, taxes_and_fees=900.25, total_fare=3900.75, seats_left=9,
            lead_window="T+15", source_name="yatra", source_type="ota",
        )
        self.assertEqual(item["flight_number"], "6E202")
        self.assertEqual(item["airline_name"], "IndiGo")
        self.assertEqual(item["carrier_code"], "6E")
        self.assertEqual(item["base_fare"], 3000.5)
        self.assertEqual(item["taxes_and_fees"], 900.25)
        self.assertEqual(item["total_fare"], 3900.75)
        self.assertEqual(item["seats_left"], 9)
        self.assertEqual(item["lead_window"], "T+15")
        self.assertEqual(item["source_name"], "yatra")
        self.assertEqual(item["source_type"], "ota")

    def test_fare_class_defaults_to_none_when_omitted(self):
        item = build_raw_fare_item(
            flight_number=None, airline_name=None, carrier_code=None,
            origin="DEL", destination="BOM", departure_time=None, arrival_time=None,
            base_fare=None, taxes_and_fees=None, total_fare=None, seats_left=None,
            lead_window="T+1", source_name="x", source_type="ota",
        )
        self.assertIsNone(item["fare_class"])

    def test_fare_class_passes_through_when_given(self):
        item = build_raw_fare_item(
            flight_number=None, airline_name=None, carrier_code=None,
            origin="DEL", destination="BOM", departure_time=None, arrival_time=None,
            base_fare=None, taxes_and_fees=None, total_fare=None, seats_left=None,
            lead_window="T+1", source_name="x", source_type="ota", fare_class="EC",
        )
        self.assertEqual(item["fare_class"], "EC")


if __name__ == "__main__":
    unittest.main()
