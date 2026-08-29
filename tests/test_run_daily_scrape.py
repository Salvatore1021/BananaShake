"""
Unit tests for run_daily_scrape.py's pure logic (the coverage-matrix
renderer). No live network/browser calls are made here.
"""

from __future__ import annotations

import unittest

from app.ingestion.scheduler import AP_WINDOWS_DAYS, ROUTE_PAIRS
from run_daily_scrape import render_coverage_matrix


def make_item(route_id: str, lead_window: str) -> dict:
    return {"route_id": route_id, "lead_window": lead_window}


class RenderCoverageMatrixTests(unittest.TestCase):
    def test_includes_every_route_row_and_window_column(self):
        matrix = render_coverage_matrix([])
        for route in ROUTE_PAIRS:
            self.assertIn(route.route_id, matrix)
        for window in AP_WINDOWS_DAYS:
            self.assertIn(window, matrix)

    def test_counts_quotes_per_route_and_window_cell(self):
        items = [
            make_item("DEL-BOM", "T+7"),
            make_item("DEL-BOM", "T+7"),
            make_item("DEL-BOM", "T+1"),
            make_item("BOM-BLR", "T+45"),
        ]
        matrix = render_coverage_matrix(items)
        lines = matrix.splitlines()
        header = lines[0]
        window_order = list(AP_WINDOWS_DAYS.keys())
        del_bom_row = next(l for l in lines if l.startswith("DEL-BOM"))
        cells = del_bom_row[len("DEL-BOM".ljust(10)):]
        # T+1 count=1 and T+7 count=2 should both appear somewhere in the row
        self.assertIn("1", cells)
        self.assertIn("2", cells)

    def test_empty_items_still_renders_a_full_zero_grid(self):
        matrix = render_coverage_matrix([])
        # every data row should be present even with zero collected items
        self.assertEqual(len(matrix.splitlines()), 2 + len(ROUTE_PAIRS))  # header + separator + one row per route

    def test_unknown_route_or_window_in_items_is_ignored_not_crashing(self):
        items = [make_item("XXX-YYY", "T+999")]
        matrix = render_coverage_matrix(items)  # should not raise
        self.assertIsInstance(matrix, str)


if __name__ == "__main__":
    unittest.main()
