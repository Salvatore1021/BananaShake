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
import unittest
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Carrier, FareIndexDaily, FareObservation, Route
from app.db.session import engine
from app.index.psd_index import (
    OVERALL_LABEL,
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
        # one per-window row + one blended overall row, for the single date
        self.assertEqual(count, 2)

    def test_recompute_overwrites_changed_value(self):
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

        row = self.session.scalars(
            select(FareIndexDaily).filter_by(index_date=later_day.date(), lead_window=TEST_WINDOW)
        ).one()
        # avg(6000, 5500) = 5750 -> 5750 / 5000 * 100 = 115
        self.assertEqual(row.index_value, Decimal("115.0000"))


if __name__ == "__main__":
    unittest.main()
