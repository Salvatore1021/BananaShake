"""
Unit tests for apixproj/raw_fare_item.write_raw_fare_items_csv. No live
network calls -- writes to a temp file and reads it back.
"""

from __future__ import annotations

import csv
import os
import tempfile
import unittest
from datetime import datetime, timezone

from apixproj.raw_fare_item import RAW_FARE_ITEM_FIELDS, build_raw_fare_item, write_raw_fare_items_csv


def make_item(**overrides) -> dict:
    item = build_raw_fare_item(
        flight_number="QP1119", airline_name="Akasa Air", carrier_code="QP",
        origin="DEL", destination="BOM", departure_time="2026-09-06T08:40:00",
        arrival_time="2026-09-06T11:05:00", base_fare=5640.0, taxes_and_fees=1240.0,
        total_fare=6880.0, seats_left=7, lead_window="T+7", source_name="akasa_air",
        source_type="airline_direct", scraped_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
    )
    item.update(overrides)
    return item


class WriteRawFareItemsCsvTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)

    def tearDown(self):
        os.unlink(self.path)

    def _read_rows(self) -> list[dict]:
        with open(self.path, encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def test_header_matches_canonical_field_order(self):
        write_raw_fare_items_csv([make_item()], self.path)
        with open(self.path, encoding="utf-8") as f:
            header = next(csv.reader(f))
        self.assertEqual(tuple(header), RAW_FARE_ITEM_FIELDS)

    def test_one_row_per_item_with_correct_values(self):
        items = [make_item(flight_number="QP1119"), make_item(flight_number="QP1112")]
        write_raw_fare_items_csv(items, self.path)
        rows = self._read_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["flight_number"], "QP1119")
        self.assertEqual(rows[1]["flight_number"], "QP1112")
        self.assertEqual(rows[0]["total_fare"], "6880.0")
        self.assertEqual(rows[0]["route_id"], "DEL-BOM")

    def test_empty_items_still_writes_a_header_only_file(self):
        write_raw_fare_items_csv([], self.path)
        rows = self._read_rows()
        self.assertEqual(rows, [])
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.readline().strip(), ",".join(RAW_FARE_ITEM_FIELDS))

    def test_none_values_become_empty_cells_not_the_string_none(self):
        write_raw_fare_items_csv([make_item(seats_left=None, base_fare=None)], self.path)
        rows = self._read_rows()
        self.assertEqual(rows[0]["seats_left"], "")
        self.assertEqual(rows[0]["base_fare"], "")

    def test_extra_unexpected_keys_are_ignored_rather_than_erroring(self):
        item = make_item()
        item["some_future_field_not_in_schema"] = "x"
        write_raw_fare_items_csv([item], self.path)  # should not raise
        rows = self._read_rows()
        self.assertNotIn("some_future_field_not_in_schema", rows[0])

    def test_missing_key_in_an_item_becomes_an_empty_cell_not_an_error(self):
        item = make_item()
        del item["seats_left"]
        write_raw_fare_items_csv([item], self.path)  # should not raise
        rows = self._read_rows()
        self.assertEqual(rows[0]["seats_left"], "")


if __name__ == "__main__":
    unittest.main()
