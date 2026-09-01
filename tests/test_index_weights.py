"""Unit tests for app/index/weights.py. Deliberately network/DB-free."""

from __future__ import annotations

import unittest

from app.index.weights import DEFAULT_ROUTE_WEIGHTS, normalized_weights, route_weight
from app.ingestion.scheduler import ROUTE_PAIRS


class DefaultRouteWeightsTests(unittest.TestCase):
    def test_every_basket_route_has_a_weight(self):
        for route in ROUTE_PAIRS:
            self.assertIn(route.route_id, DEFAULT_ROUTE_WEIGHTS)

    def test_default_is_equal_weight(self):
        values = set(DEFAULT_ROUTE_WEIGHTS.values())
        self.assertEqual(values, {1.0})


class RouteWeightTests(unittest.TestCase):
    def test_known_route_returns_configured_weight(self):
        self.assertEqual(route_weight("DEL-BOM"), 1.0)

    def test_off_basket_route_returns_zero(self):
        self.assertEqual(route_weight("XXX-YYY"), 0.0)

    def test_custom_table_is_respected(self):
        self.assertEqual(route_weight("DEL-BOM", {"DEL-BOM": 3.5}), 3.5)


class NormalizedWeightsTests(unittest.TestCase):
    def test_sums_to_one(self):
        weights = normalized_weights(["DEL-BOM", "DEL-BLR", "BOM-BLR"])
        self.assertAlmostEqual(sum(weights.values()), 1.0)

    def test_equal_weights_split_evenly(self):
        weights = normalized_weights(["DEL-BOM", "DEL-BLR"], {"DEL-BOM": 1.0, "DEL-BLR": 1.0})
        self.assertAlmostEqual(weights["DEL-BOM"], 0.5)
        self.assertAlmostEqual(weights["DEL-BLR"], 0.5)

    def test_missing_route_redistributes_rather_than_shrinking_total(self):
        """The whole point of renormalizing per-day: a route with no data
        today must not silently deflate the index by leaving its weight
        stranded in some fixed denominator."""
        full_basket = normalized_weights(
            ["A", "B", "C"], {"A": 1.0, "B": 1.0, "C": 1.0},
        )
        one_missing = normalized_weights(
            ["A", "B"], {"A": 1.0, "B": 1.0, "C": 1.0},
        )
        self.assertAlmostEqual(sum(one_missing.values()), 1.0)
        self.assertGreater(one_missing["A"], full_basket["A"])

    def test_uneven_custom_weights(self):
        weights = normalized_weights(["A", "B"], {"A": 3.0, "B": 1.0})
        self.assertAlmostEqual(weights["A"], 0.75)
        self.assertAlmostEqual(weights["B"], 0.25)

    def test_empty_route_list_returns_empty(self):
        self.assertEqual(normalized_weights([]), {})

    def test_all_zero_weights_returns_empty_rather_than_dividing_by_zero(self):
        self.assertEqual(normalized_weights(["A", "B"], {"A": 0.0, "B": 0.0}), {})

    def test_route_not_in_table_contributes_zero_weight(self):
        weights = normalized_weights(["A", "B"], {"A": 1.0})
        self.assertAlmostEqual(weights["A"], 1.0)
        self.assertAlmostEqual(weights["B"], 0.0)


if __name__ == "__main__":
    unittest.main()
