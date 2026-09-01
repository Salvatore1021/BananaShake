"""
Integration tests for app/db/loader.py against a real Postgres connection
(the DATABASE_URL app/db/session.py builds its engine from).

Every test runs inside one outer transaction, rolled back in tearDown, so
nothing here ever WRITES anything durable. But the daily scraper's own
real data lives permanently in this same table (Postgres's default READ
COMMITTED isolation means a query inside this transaction still sees rows
another, already-committed transaction wrote) -- so every test uses a
synthetic carrier ("ZZ"), route ("ZZZ-YYY") and lead_window ("T+TEST")
that production data will never use, and counts are scoped to those
rather than the whole table. Without that, an assertion like "exactly one
row exists" breaks the moment the real scraper has ever loaded anything.

If Postgres isn't reachable at all, every test in this module is skipped
cleanly rather than failing -- this project's other tests are deliberately
network/DB-free, so a machine without Postgres running should still get a
green (if smaller) suite, not a wall of connection errors.
"""

from __future__ import annotations

import unittest

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.loader import load_raw_fare_items
from app.db.models import Carrier, FareObservation, Route
from app.db.session import engine


def _skip_if_db_unreachable() -> None:
    try:
        conn = engine.connect()
        conn.close()
    except Exception as exc:  # noqa: BLE001 — any failure means "skip", not "fail"
        raise unittest.SkipTest(f"Postgres not reachable: {exc}")


TEST_CARRIER = "ZZ"
TEST_ROUTE = "ZZZ-YYY"
TEST_WINDOW = "T+TEST"  # never used by the real basket (app/ingestion/scheduler.py)


def _make_raw_item(**overrides) -> dict:
    item = {
        "flight_number": "ZZ0001", "airline_name": "Test Airline", "carrier_code": TEST_CARRIER,
        "origin": "ZZZ", "destination": "YYY", "route_id": TEST_ROUTE,
        "departure_time": "2026-09-05T10:00:00", "arrival_time": "2026-09-05T12:20:00",
        "base_fare": 5000.0, "taxes_and_fees": 1200.0, "total_fare": 6200.0,
        "seats_left": 5, "fare_class": "EC", "lead_window": TEST_WINDOW,
        "source_name": "akasa_air", "source_type": "airline_direct",
        "scraped_at_timestamp": "2026-08-29T19:39:19.943395+00:00",
    }
    item.update(overrides)
    return item


def _test_observation_count(session) -> int:
    return session.execute(
        select(func.count()).select_from(FareObservation).where(FareObservation.carrier_code == TEST_CARRIER)
    ).scalar()


class LoaderTestCase(unittest.TestCase):
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


class LoadRawFareItemsTests(LoaderTestCase):
    def test_empty_batch_returns_zero_and_writes_nothing(self):
        self.assertEqual(load_raw_fare_items([], session=self.session), 0)

    def test_inserts_new_carrier_and_route_dimensions(self):
        load_raw_fare_items([_make_raw_item()], session=self.session)
        self.assertIsNotNone(self.session.get(Carrier, TEST_CARRIER))
        self.assertIsNotNone(self.session.get(Route, TEST_ROUTE))

    def test_inserts_one_fare_observation_row(self):
        inserted = load_raw_fare_items([_make_raw_item()], session=self.session)
        self.assertEqual(inserted, 1)
        self.assertEqual(_test_observation_count(self.session), 1)

    def test_reloading_the_same_batch_is_a_no_op(self):
        load_raw_fare_items([_make_raw_item()], session=self.session)
        second = load_raw_fare_items([_make_raw_item()], session=self.session)
        self.assertEqual(second, 0)
        self.assertEqual(_test_observation_count(self.session), 1)

    def test_existing_dimension_rows_are_left_untouched(self):
        self.session.add(Carrier(carrier_code=TEST_CARRIER, airline_name="Custom Name"))
        self.session.flush()
        load_raw_fare_items([_make_raw_item()], session=self.session)
        carrier = self.session.get(Carrier, TEST_CARRIER)
        self.assertEqual(carrier.airline_name, "Custom Name")

    def test_two_distinct_scrape_times_both_land_as_separate_rows(self):
        item_a = _make_raw_item(scraped_at_timestamp="2026-08-29T10:00:00+00:00")
        item_b = _make_raw_item(scraped_at_timestamp="2026-08-30T10:00:00+00:00")
        inserted = load_raw_fare_items([item_a, item_b], session=self.session)
        self.assertEqual(inserted, 2)

    def test_item_missing_carrier_code_is_skipped_not_crashing(self):
        item = _make_raw_item(carrier_code=None)
        inserted = load_raw_fare_items([item], session=self.session)
        self.assertEqual(inserted, 0)

    def test_unparseable_decimal_field_becomes_none_not_a_crash(self):
        item = _make_raw_item(base_fare="not-a-number")
        load_raw_fare_items([item], session=self.session)
        obs = self.session.execute(
            select(FareObservation).where(FareObservation.carrier_code == TEST_CARRIER)
        ).scalars().one()
        self.assertIsNone(obs.base_fare)


if __name__ == "__main__":
    unittest.main()
