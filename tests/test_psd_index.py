"""
Integration tests for app/index/psd_index.py against a real Postgres
connection. See tests/test_loader.py's module docstring for the isolation
pattern (one rolled-back transaction per test, skip-if-unreachable) and
for why every synthetic FareObservation here uses a lead_window
("T+TEST"/"T+TEST-OTHER") the real scraper never produces: without that,
these hand-computed expected values would silently mix with whatever real
data has already been loaded by app/ingestion/scheduler.py's actual
T+1/T+7/T+15/T+30/T+45 basket.
"""

from __future__ import annotations

import datetime
import math
import unittest
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Carrier, FareIndexDaily, FareObservation, Route
from app.db.session import engine
from app.index.psd_index import (
    ALL_METHODS,
    GEKS_METHOD,
    LEGACY_METHOD,
    OVERALL_LABEL,
    RECOMMENDED_METHOD,
    TORNQVIST_METHOD,
    compute_geks_window_index_series,
    compute_overall_index_series,
    compute_window_index_series,
    recompute_all,
)

TEST_WINDOW = "T+TEST"
OTHER_TEST_WINDOW = "T+TEST-OTHER"


def _skip_if_db_unreachable() -> None:
    try:
        conn = engine.connect()
        conn.close()
    except Exception as exc:  # noqa: BLE001 — any failure means "skip", not "fail"
        raise unittest.SkipTest(f"Postgres not reachable: {exc}")


def _make_observation(route_id, total_fare, scraped_at, lead_window=TEST_WINDOW) -> FareObservation:
    return FareObservation(
        route_id=route_id, carrier_code="QP", total_fare=Decimal(str(total_fare)),
        lead_window=lead_window, source_name="akasa_air", source_type="airline_direct",
        scraped_at=scraped_at,
    )


class PsdIndexTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _skip_if_db_unreachable()

    def setUp(self):
        self.connection = engine.connect()
        self.trans = self.connection.begin()
        self.session = Session(bind=self.connection)

    def tearDown(self):
        self.session.close()
        self.trans.rollback()
        self.connection.close()

    def _seed_route(self, route_id: str, origin: str, destination: str) -> None:
        self.session.merge(Carrier(carrier_code="QP", airline_name="Akasa Air"))
        self.session.merge(Route(route_id=route_id, origin=origin, destination=destination))
        self.session.flush()


class ComputeWindowIndexSeriesTests(PsdIndexTestCase):
    def test_base_date_gets_index_value_100(self):
        self._seed_route("DEL-BOM", "DEL", "BOM")
        base_day = datetime.datetime(2026, 8, 1, 10, 0)
        self.session.add(_make_observation("DEL-BOM", 5000, base_day))
        self.session.flush()

        rows = compute_window_index_series(self.session, TEST_WINDOW, {"DEL-BOM": 1.0})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["index_value"], Decimal("100.0000"))
        self.assertEqual(rows[0]["route_count"], 1)

    def test_price_increase_moves_index_proportionally(self):
        self._seed_route("DEL-BOM", "DEL", "BOM")
        base_day = datetime.datetime(2026, 8, 1, 10, 0)
        later_day = datetime.datetime(2026, 8, 2, 10, 0)
        self.session.add(_make_observation("DEL-BOM", 5000, base_day))
        self.session.add(_make_observation("DEL-BOM", 6000, later_day))
        self.session.flush()

        rows = compute_window_index_series(self.session, TEST_WINDOW, {"DEL-BOM": 1.0})
        by_date = {r["index_date"]: r for r in rows}
        self.assertEqual(by_date[base_day.date()]["index_value"], Decimal("100.0000"))
        # 6000 / 5000 * 100 = 120
        self.assertEqual(by_date[later_day.date()]["index_value"], Decimal("120.0000"))

    def test_weighted_blend_of_two_routes(self):
        self._seed_route("DEL-BOM", "DEL", "BOM")
        self._seed_route("DEL-BLR", "DEL", "BLR")
        base_day = datetime.datetime(2026, 8, 1, 10, 0)
        later_day = datetime.datetime(2026, 8, 2, 10, 0)
        # DEL-BOM: 5000 -> 6000 (+20%); DEL-BLR: 4000 -> 4000 (+0%); equal weight
        self.session.add(_make_observation("DEL-BOM", 5000, base_day))
        self.session.add(_make_observation("DEL-BOM", 6000, later_day))
        self.session.add(_make_observation("DEL-BLR", 4000, base_day))
        self.session.add(_make_observation("DEL-BLR", 4000, later_day))
        self.session.flush()

        rows = compute_window_index_series(self.session, TEST_WINDOW, {"DEL-BOM": 1.0, "DEL-BLR": 1.0})
        by_date = {r["index_date"]: r for r in rows}
        # equal-weight blend of +20% and +0% => +10% => 110
        self.assertEqual(by_date[later_day.date()]["index_value"], Decimal("110.0000"))
        self.assertEqual(by_date[later_day.date()]["route_count"], 2)

    def test_route_missing_on_a_day_is_excluded_not_zeroed(self):
        self._seed_route("DEL-BOM", "DEL", "BOM")
        self._seed_route("DEL-BLR", "DEL", "BLR")
        base_day = datetime.datetime(2026, 8, 1, 10, 0)
        later_day = datetime.datetime(2026, 8, 2, 10, 0)
        self.session.add(_make_observation("DEL-BOM", 5000, base_day))
        self.session.add(_make_observation("DEL-BOM", 6000, later_day))
        self.session.add(_make_observation("DEL-BLR", 4000, base_day))
        # DEL-BLR has no observation at all on later_day
        self.session.flush()

        rows = compute_window_index_series(self.session, TEST_WINDOW, {"DEL-BOM": 1.0, "DEL-BLR": 1.0})
        by_date = {r["index_date"]: r for r in rows}
        # only DEL-BOM contributes on later_day, renormalized to full weight
        self.assertEqual(by_date[later_day.date()]["index_value"], Decimal("120.0000"))
        self.assertEqual(by_date[later_day.date()]["route_count"], 1)

    def test_no_data_for_window_returns_empty(self):
        self.assertEqual(compute_window_index_series(self.session, "T+TEST-UNUSED"), [])

    def test_lead_window_isolation(self):
        """A route's fares under one AP window must never bleed into
        another window's index."""
        self._seed_route("DEL-BOM", "DEL", "BOM")
        day = datetime.datetime(2026, 8, 1, 10, 0)
        self.session.add(_make_observation("DEL-BOM", 5000, day, lead_window=OTHER_TEST_WINDOW))
        self.session.flush()

        self.assertEqual(compute_window_index_series(self.session, TEST_WINDOW, {"DEL-BOM": 1.0}), [])


class ComputeOverallIndexSeriesTests(unittest.TestCase):
    def test_blends_windows_equally_per_date(self):
        d = datetime.date(2026, 8, 2)
        base = datetime.date(2026, 8, 1)
        series = {
            "T+1": [{"index_date": d, "lead_window": "T+1", "index_value": Decimal("110.0000"), "base_date": base, "route_count": 2}],
            "T+7": [{"index_date": d, "lead_window": "T+7", "index_value": Decimal("130.0000"), "base_date": base, "route_count": 2}],
        }
        overall = compute_overall_index_series(series)
        self.assertEqual(len(overall), 1)
        self.assertEqual(overall[0]["lead_window"], OVERALL_LABEL)
        self.assertEqual(overall[0]["index_value"], Decimal("120.0000"))
        self.assertEqual(overall[0]["route_count"], 4)

    def test_no_series_returns_empty(self):
        self.assertEqual(compute_overall_index_series({}), [])


class PersistAndRecomputeTests(PsdIndexTestCase):
    def test_persist_is_idempotent(self):
        self._seed_route("DEL-BOM", "DEL", "BOM")
        base_day = datetime.datetime(2026, 8, 1, 10, 0)
        self.session.add(_make_observation("DEL-BOM", 5000, base_day))
        self.session.flush()

        first = recompute_all(self.session, {"DEL-BOM": 1.0}, ap_windows=[TEST_WINDOW], commit=False)
        second = recompute_all(self.session, {"DEL-BOM": 1.0}, ap_windows=[TEST_WINDOW], commit=False)
        self.assertGreater(first, 0)
        self.assertEqual(first, second)

        count = self.session.execute(
            select(func.count()).select_from(FareIndexDaily).where(FareIndexDaily.index_date == base_day.date())
        ).scalar()
        # recompute_all's default `methods` is ALL_METHODS (see its own
        # docstring on why: every run keeps every method's series fresh so
        # none of them can go stale) -- one per-window row + one blended
        # overall row, per method, for the single date.
        self.assertEqual(count, 2 * len(ALL_METHODS))

    def test_recompute_overwrites_changed_value_per_method(self):
        self._seed_route("DEL-BOM", "DEL", "BOM")
        base_day = datetime.datetime(2026, 8, 1, 10, 0)
        later_day = datetime.datetime(2026, 8, 2, 10, 0)
        self.session.add(_make_observation("DEL-BOM", 5000, base_day))
        self.session.add(_make_observation("DEL-BOM", 6000, later_day))
        self.session.flush()
        recompute_all(self.session, {"DEL-BOM": 1.0}, ap_windows=[TEST_WINDOW], commit=False)

        # a second, cheaper quote lands for later_day; recompute again
        self.session.add(_make_observation("DEL-BOM", 5500, later_day + datetime.timedelta(hours=1)))
        self.session.flush()
        recompute_all(self.session, {"DEL-BOM": 1.0}, ap_windows=[TEST_WINDOW], commit=False)

        legacy_row = self.session.scalars(
            select(FareIndexDaily).filter_by(index_date=later_day.date(), lead_window=TEST_WINDOW, method=LEGACY_METHOD)
        ).one()
        # arithmetic mean(6000, 5500) = 5750 -> 5750 / 5000 * 100 = 115
        self.assertEqual(legacy_row.index_value, Decimal("115.0000"))

        recommended_row = self.session.scalars(
            select(FareIndexDaily).filter_by(index_date=later_day.date(), lead_window=TEST_WINDOW, method=RECOMMENDED_METHOD)
        ).one()
        # geometric mean(6000, 5500) = sqrt(6000*5500) ~= 5744.5628 -> ~114.8913;
        # below the arithmetic-mean result, which is exactly the Carli
        # upward-bias direction the module docstring documents.
        self.assertLess(recommended_row.index_value, legacy_row.index_value)
        self.assertAlmostEqual(float(recommended_row.index_value), 114.8913, places=3)


class TornqvistIndexTests(PsdIndexTestCase):
    """tornqvist_bilateral_v1 combines routes with a weighted GEOMETRIC
    mean of price relatives instead of jevons_geometric_v2's weighted
    ARITHMETIC mean — same per-route-fixed-base shape as
    ComputeWindowIndexSeriesTests above, so the only thing under test here
    is that the combination step itself is correct."""

    def test_base_date_gets_index_value_100(self):
        self._seed_route("DEL-BOM", "DEL", "BOM")
        base_day = datetime.datetime(2026, 8, 1, 10, 0)
        self.session.add(_make_observation("DEL-BOM", 5000, base_day))
        self.session.flush()

        rows = compute_window_index_series(self.session, TEST_WINDOW, {"DEL-BOM": 1.0}, method=TORNQVIST_METHOD)
        self.assertEqual(rows[0]["index_value"], Decimal("100.0000"))

    def test_weighted_geometric_blend_diverges_from_arithmetic(self):
        self._seed_route("DEL-BOM", "DEL", "BOM")
        self._seed_route("DEL-BLR", "DEL", "BLR")
        base_day = datetime.datetime(2026, 8, 1, 10, 0)
        later_day = datetime.datetime(2026, 8, 2, 10, 0)
        # DEL-BOM: 5000 -> 6000 (+20%); DEL-BLR: 4000 -> 4000 (+0%); equal weight
        self.session.add(_make_observation("DEL-BOM", 5000, base_day))
        self.session.add(_make_observation("DEL-BOM", 6000, later_day))
        self.session.add(_make_observation("DEL-BLR", 4000, base_day))
        self.session.add(_make_observation("DEL-BLR", 4000, later_day))
        self.session.flush()

        rows = compute_window_index_series(self.session, TEST_WINDOW, {"DEL-BOM": 1.0, "DEL-BLR": 1.0}, method=TORNQVIST_METHOD)
        by_date = {r["index_date"]: r for r in rows}
        # weighted geometric mean of relatives 1.20 and 1.00, equal weights
        # = sqrt(1.20 * 1.00) = sqrt(1.20) ~= 1.0954451, vs. the equal-weight
        # ARITHMETIC blend's 110.0000 (see the matching Jevons/Carli test) --
        # AM > GM for any non-identical pair, so this must land strictly below.
        expected = 100 * math.sqrt(1.20)
        self.assertAlmostEqual(float(by_date[later_day.date()]["index_value"]), expected, places=3)
        self.assertLess(by_date[later_day.date()]["index_value"], Decimal("110.0000"))


class GeksIndexTests(PsdIndexTestCase):
    """geks_multilateral_v1 -- see the module docstring's MULTI-ROUTE
    COMBINATION METHODS section. Two properties are cheap to verify
    exactly: (1) the single earliest date across the series is always
    100 by construction, regardless of route coverage elsewhere, and
    (2) with exactly one route in the basket, every bilateral bridge
    trivially agrees (the route's own fare cancels out of every
    comparison), so GEKS collapses to the same plain price relative the
    fixed-base methods already compute -- a degenerate case that still
    exercises the full multilateral averaging code path."""

    def test_base_date_is_always_100_even_with_uneven_route_coverage(self):
        self._seed_route("DEL-BOM", "DEL", "BOM")
        self._seed_route("DEL-BLR", "DEL", "BLR")
        day1 = datetime.datetime(2026, 8, 1, 10, 0)
        day2 = datetime.datetime(2026, 8, 2, 10, 0)
        day3 = datetime.datetime(2026, 8, 3, 10, 0)
        self.session.add(_make_observation("DEL-BOM", 5000, day1))
        self.session.add(_make_observation("DEL-BLR", 4000, day1))
        self.session.add(_make_observation("DEL-BOM", 5500, day2))  # DEL-BLR missing this day
        self.session.add(_make_observation("DEL-BOM", 6000, day3))
        self.session.add(_make_observation("DEL-BLR", 4000, day3))
        self.session.flush()

        rows = compute_geks_window_index_series(self.session, TEST_WINDOW, {"DEL-BOM": 1.0, "DEL-BLR": 1.0})
        by_date = {r["index_date"]: r for r in rows}
        self.assertEqual(by_date[day1.date()]["index_value"], Decimal("100.0000"))
        self.assertEqual(by_date[day1.date()]["route_count"], 2)
        self.assertEqual(by_date[day2.date()]["route_count"], 1)
        # every date got a value despite the uneven coverage -- the whole
        # point of bridging through every date rather than only comparing
        # each date directly to day1.
        self.assertEqual(set(by_date), {day1.date(), day2.date(), day3.date()})

    def test_single_route_collapses_to_the_plain_price_relative(self):
        self._seed_route("DEL-BOM", "DEL", "BOM")
        day1 = datetime.datetime(2026, 8, 1, 10, 0)
        day2 = datetime.datetime(2026, 8, 2, 10, 0)
        day3 = datetime.datetime(2026, 8, 3, 10, 0)
        self.session.add(_make_observation("DEL-BOM", 5000, day1))
        self.session.add(_make_observation("DEL-BOM", 6000, day2))
        self.session.add(_make_observation("DEL-BOM", 7500, day3))
        self.session.flush()

        rows = compute_geks_window_index_series(self.session, TEST_WINDOW, {"DEL-BOM": 1.0})
        by_date = {r["index_date"]: r for r in rows}
        self.assertEqual(by_date[day1.date()]["index_value"], Decimal("100.0000"))
        self.assertEqual(by_date[day2.date()]["index_value"], Decimal("120.0000"))
        self.assertEqual(by_date[day3.date()]["index_value"], Decimal("150.0000"))

    def test_balanced_full_coverage_panel_matches_tornqvist_exactly(self):
        """With every route reporting on every date (a balanced panel),
        the same fixed weights apply to every bilateral comparison, so
        the bilateral matrix is already transitive and GEKS's multilateral
        averaging must reduce to exactly the same series
        tornqvist_bilateral_v1 already computes directly against the
        fixed base date -- this is the case GEKS is NOT needed for, and
        it should show that by agreeing with it exactly rather than by
        coincidence."""
        self._seed_route("DEL-BOM", "DEL", "BOM")
        self._seed_route("DEL-BLR", "DEL", "BLR")
        day1 = datetime.datetime(2026, 8, 1, 10, 0)
        day2 = datetime.datetime(2026, 8, 2, 10, 0)
        day3 = datetime.datetime(2026, 8, 3, 10, 0)
        for day, (bom_fare, blr_fare) in {
            day1: (5000, 4000), day2: (6000, 4200), day3: (4800, 4400),
        }.items():
            self.session.add(_make_observation("DEL-BOM", bom_fare, day))
            self.session.add(_make_observation("DEL-BLR", blr_fare, day))
        self.session.flush()

        weights = {"DEL-BOM": 1.0, "DEL-BLR": 1.0}
        geks_rows = {r["index_date"]: r["index_value"] for r in compute_geks_window_index_series(self.session, TEST_WINDOW, weights)}
        tornqvist_rows = {
            r["index_date"]: r["index_value"]
            for r in compute_window_index_series(self.session, TEST_WINDOW, weights, method=TORNQVIST_METHOD)
        }
        self.assertEqual(geks_rows, tornqvist_rows)


class MethodRevertibilityTests(PsdIndexTestCase):
    """Confirms the actual guarantee this whole method-column design exists
    for: recomputing one method never touches another method's already-
    persisted rows, so flipping ACTIVE_INDEX_METHOD back is always safe."""

    def test_recomputing_one_method_does_not_touch_the_other(self):
        self._seed_route("DEL-BOM", "DEL", "BOM")
        base_day = datetime.datetime(2026, 8, 1, 10, 0)
        self.session.add(_make_observation("DEL-BOM", 5000, base_day))
        self.session.flush()

        recompute_all(self.session, {"DEL-BOM": 1.0}, ap_windows=[TEST_WINDOW], commit=False, methods=ALL_METHODS)
        before = {
            m: self.session.scalars(
                select(FareIndexDaily).filter_by(index_date=base_day.date(), lead_window=TEST_WINDOW, method=m)
            ).one().index_value
            for m in ALL_METHODS
        }

        # A later recompute that only asks for the legacy method must leave
        # the recommended method's row exactly as it was.
        recompute_all(self.session, {"DEL-BOM": 1.0}, ap_windows=[TEST_WINDOW], commit=False, methods=(LEGACY_METHOD,))
        after_recommended = self.session.scalars(
            select(FareIndexDaily).filter_by(index_date=base_day.date(), lead_window=TEST_WINDOW, method=RECOMMENDED_METHOD)
        ).one().index_value
        self.assertEqual(after_recommended, before[RECOMMENDED_METHOD])


if __name__ == "__main__":
    unittest.main()
