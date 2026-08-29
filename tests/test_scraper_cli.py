"""
Unit tests for test_scraper.py, the standalone live-scraper verification
CLI. Covers the pure logic (task building, table rendering, assertions,
replay-payload parsing) and an end-to-end run of main() through the
network-free --replay path. No live network/browser calls are made here —
that's deliberate: this file proves the TOOL is correct, not that a live
site currently cooperates.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date, timedelta

from app.ingestion.scheduler import SearchTask
from test_scraper import (
    MIN_VALID_QUOTES,
    build_task,
    load_replay_payload,
    main,
    render_table,
    run_assertions,
)


def make_item(**overrides) -> dict:
    defaults = dict(
        flight_number="QP1119", airline_name="Akasa Air", carrier_code="QP",
        origin="DEL", destination="BOM", departure_time="2026-09-06T08:40:00",
        arrival_time="2026-09-06T11:05:00", base_fare=5640.0, taxes_and_fees=1240.0,
        total_fare=6880.0, seats_left=7, scraped_at_timestamp="2026-08-29T00:00:00+00:00",
        lead_window="T+7", source_name="akasa_air", source_type="airline_direct", route_id="DEL-BOM",
    )
    defaults.update(overrides)
    return defaults


def make_akasa_payload(n_flights: int) -> dict:
    """Payload shaped exactly like a real Akasa fare-search response — see
    apixproj/spiders/akasa_air_spider.py's module docstring for how this
    shape was confirmed."""
    journeys = []
    fares_available = []
    for i in range(n_flights):
        fare_key = f"fk{i}"
        journeys.append({
            "designator": {
                "origin": "DEL", "destination": "BOM",
                "departure": f"2026-09-06T{8 + i:02d}:00:00", "arrival": f"2026-09-06T{10 + i:02d}:20:00",
            },
            "flightType": "NonStop",
            "journeyKey": f"jk{i}",
            "fares": [{"fareAvailabilityKey": fare_key, "details": [{"availableCount": 7}]}],
            "segments": [{"designator": {}, "identifier": {"identifier": str(1100 + i), "carrierCode": "QP"}}],
        })
        fares_available.append({
            "key": fare_key,
            "value": {"fares": [{"passengerFares": [{"fareAmount": 6880.0 + i, "discountedFare": 5640.0 + i}]}]},
        })

    return {
        "data": {
            "results": [{"trips": [{"journeysAvailableByMarket": [{"key": "DEL|BOM", "value": journeys}]}]}],
            "faresAvailable": fares_available,
        }
    }


class BuildTaskTests(unittest.TestCase):
    def test_builds_search_task_with_expected_fields(self):
        task = build_task("del", "bom", 7)
        self.assertIsInstance(task, SearchTask)
        self.assertEqual(task.origin, "DEL")
        self.assertEqual(task.destination, "BOM")
        self.assertEqual(task.lead_days, 7)
        self.assertEqual(task.advance_purchase_window, "T+7")
        self.assertEqual(task.departure_date, date.today() + timedelta(days=7))

    def test_uppercases_airport_codes(self):
        task = build_task("blr", "hyd", 1)
        self.assertEqual(task.origin, "BLR")
        self.assertEqual(task.destination, "HYD")


class RenderTableTests(unittest.TestCase):
    def test_sorts_by_total_fare_ascending(self):
        items = [
            make_item(flight_number="PRICEY1", total_fare=9000.0),
            make_item(flight_number="CHEAP1", total_fare=3000.0),
        ]
        table = render_table(items)
        self.assertLess(table.index("CHEAP1"), table.index("PRICEY1"))

    def test_limits_to_top_n(self):
        items = [make_item(flight_number=f"F{i}", total_fare=float(1000 + i)) for i in range(10)]
        table = render_table(items)
        for i in range(5):
            self.assertIn(f"F{i}", table)
        for i in range(5, 10):
            self.assertNotIn(f"F{i}", table)

    def test_includes_header_columns(self):
        table = render_table([make_item()])
        for header in ("Airline", "Flight #", "Base Fare", "Total Fare"):
            self.assertIn(header, table)

    def test_empty_items_returns_placeholder_text(self):
        table = render_table([])
        self.assertIn("no fare quotes", table)

    def test_items_without_numeric_total_fare_are_excluded(self):
        items = [make_item(total_fare=None), make_item(flight_number="valid", total_fare=1234.0)]
        table = render_table(items)
        self.assertIn("valid", table)
        self.assertIn("1234.00", table)


class RunAssertionsTests(unittest.TestCase):
    def test_passes_with_enough_valid_quotes(self):
        items = [make_item(flight_number=f"F{i}") for i in range(MIN_VALID_QUOTES)]
        run_assertions(items)  # should not raise

    def test_fails_with_too_few_quotes(self):
        items = [make_item(flight_number=f"F{i}") for i in range(MIN_VALID_QUOTES - 1)]
        with self.assertRaises(AssertionError):
            run_assertions(items)

    def test_fails_on_non_positive_total_fare(self):
        items = [make_item(flight_number=f"F{i}") for i in range(MIN_VALID_QUOTES)]
        items[0]["total_fare"] = -10.0
        with self.assertRaises(AssertionError):
            run_assertions(items)

    def test_fails_on_non_numeric_total_fare(self):
        items = [make_item(flight_number=f"F{i}") for i in range(MIN_VALID_QUOTES)]
        items[0]["total_fare"] = "not-a-number"
        with self.assertRaises(AssertionError):
            run_assertions(items)

    def test_fails_on_missing_required_metadata(self):
        for field in ("airline_name", "carrier_code", "departure_time"):
            items = [make_item(flight_number=f"F{i}") for i in range(MIN_VALID_QUOTES)]
            items[0][field] = ""
            with self.assertRaises(AssertionError, msg=f"expected failure for empty {field}"):
                run_assertions(items)

    def test_fails_on_none_required_metadata(self):
        items = [make_item(flight_number=f"F{i}") for i in range(MIN_VALID_QUOTES)]
        items[0]["airline_name"] = None
        with self.assertRaises(AssertionError):
            run_assertions(items)


class LoadReplayPayloadTests(unittest.TestCase):
    def test_parses_a_real_shaped_akasa_payload(self):
        payload = make_akasa_payload(2)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(payload, f)
            path = f.name
        try:
            task = build_task("DEL", "BOM", 7)
            items = load_replay_payload(path, task)
            self.assertEqual(len(items), 2)
            self.assertEqual(items[0]["source_name"], "akasa_air")
            self.assertEqual(items[0]["carrier_code"], "QP")
        finally:
            os.unlink(path)


class MainReplayIntegrationTests(unittest.TestCase):
    def test_main_returns_0_and_writes_output_when_verification_passes(self):
        payload = make_akasa_payload(MIN_VALID_QUOTES)
        fd, payload_path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        original_cwd = os.getcwd()
        tmp_dir = tempfile.mkdtemp()
        try:
            os.chdir(tmp_dir)
            exit_code = main([
                "--origin", "DEL", "--destination", "BOM", "--days", "7",
                "--source", "akasa", "--replay", payload_path,
            ])
            self.assertEqual(exit_code, 0)
            self.assertTrue(os.path.exists("sample_flights.json"))
            with open("sample_flights.json", encoding="utf-8") as f:
                saved = json.load(f)
            self.assertEqual(len(saved), MIN_VALID_QUOTES)
        finally:
            os.chdir(original_cwd)
            os.unlink(payload_path)

    def test_main_returns_1_when_verification_fails(self):
        payload = make_akasa_payload(MIN_VALID_QUOTES - 1)
        fd, payload_path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        original_cwd = os.getcwd()
        tmp_dir = tempfile.mkdtemp()
        try:
            os.chdir(tmp_dir)
            exit_code = main([
                "--origin", "DEL", "--destination", "BOM", "--days", "7",
                "--source", "akasa", "--replay", payload_path,
            ])
            self.assertEqual(exit_code, 1)
        finally:
            os.chdir(original_cwd)
            os.unlink(payload_path)


if __name__ == "__main__":
    unittest.main()
