"""
Unit tests for the route x date-matrix generator in app/ingestion/scheduler.py.

Deliberately network-free: nothing here touches HTTP, Scrapy, or Playwright.
Run with:  python -m unittest tests.test_scheduler -v
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from app.ingestion.scheduler import (
    AP_WINDOWS_DAYS,
    IST,
    ROUTE_PAIRS,
    RouteDateMatrixScheduler,
    RoutePair,
    SearchTask,
    default_run_date,
    generate_route_date_matrix,
)

FIXED_RUN_DATE = date(2026, 8, 29)  # arbitrary fixed anchor for determinism


class RoutePairTests(unittest.TestCase):
    def test_required_city_pairs_are_present(self):
        expected = {
            ("DEL", "BOM"),
            ("DEL", "BLR"),
            ("BOM", "BLR"),
            ("DEL", "CCU"),
            ("BLR", "HYD"),
            ("MAA", "DEL"),
        }
        actual = {(r.origin, r.destination) for r in ROUTE_PAIRS}
        self.assertEqual(actual, expected)

    def test_route_pairs_are_unique(self):
        route_ids = [r.route_id for r in ROUTE_PAIRS]
        self.assertEqual(len(route_ids), len(set(route_ids)))

    def test_route_id_format(self):
        self.assertEqual(RoutePair("DEL", "BOM").route_id, "DEL-BOM")

    def test_rejects_same_origin_and_destination(self):
        with self.assertRaises(ValueError):
            RoutePair("DEL", "DEL")

    def test_rejects_empty_codes(self):
        with self.assertRaises(ValueError):
            RoutePair("", "BOM")
        with self.assertRaises(ValueError):
            RoutePair("DEL", "")


class ApWindowTests(unittest.TestCase):
    def test_required_windows_and_lead_days(self):
        self.assertEqual(
            AP_WINDOWS_DAYS,
            {"T+1": 1, "T+7": 7, "T+15": 15, "T+30": 30, "T+45": 45},
        )


class DefaultRunDateTests(unittest.TestCase):
    def test_returns_a_date_instance(self):
        self.assertIsInstance(default_run_date(), date)

    def test_matches_current_ist_calendar_day(self):
        from datetime import datetime

        self.assertEqual(default_run_date(), datetime.now(IST).date())


class GenerateRouteDateMatrixTests(unittest.TestCase):
    def test_matrix_size_is_routes_times_windows(self):
        tasks = generate_route_date_matrix(FIXED_RUN_DATE)
        self.assertEqual(len(tasks), len(ROUTE_PAIRS) * len(AP_WINDOWS_DAYS))

    def test_every_route_and_window_combination_is_covered_exactly_once(self):
        tasks = generate_route_date_matrix(FIXED_RUN_DATE)
        combos = [(t.route_id, t.advance_purchase_window) for t in tasks]
        expected = {
            (r.route_id, w) for r in ROUTE_PAIRS for w in AP_WINDOWS_DAYS
        }
        self.assertEqual(set(combos), expected)
        self.assertEqual(len(combos), len(set(combos)))  # no duplicates

    def test_departure_date_is_run_date_plus_lead_days(self):
        tasks = generate_route_date_matrix(FIXED_RUN_DATE)
        for task in tasks:
            self.assertEqual(task.departure_date, FIXED_RUN_DATE + timedelta(days=task.lead_days))

    def test_lead_days_match_ap_window_label(self):
        tasks = generate_route_date_matrix(FIXED_RUN_DATE)
        for task in tasks:
            self.assertEqual(task.lead_days, AP_WINDOWS_DAYS[task.advance_purchase_window])

    def test_task_ids_are_unique(self):
        tasks = generate_route_date_matrix(FIXED_RUN_DATE)
        ids = [t.task_id for t in tasks]
        self.assertEqual(len(ids), len(set(ids)))

    def test_deterministic_for_same_inputs(self):
        first = generate_route_date_matrix(FIXED_RUN_DATE)
        second = generate_route_date_matrix(FIXED_RUN_DATE)
        self.assertEqual(first, second)

    def test_defaults_to_today_in_ist_when_run_date_omitted(self):
        tasks = generate_route_date_matrix()
        self.assertTrue(all(t.departure_date > default_run_date() for t in tasks))

    def test_custom_routes_and_windows_are_respected(self):
        custom_routes = [RoutePair("GOI", "PNQ")]
        custom_windows = {"T+2": 2}
        tasks = generate_route_date_matrix(FIXED_RUN_DATE, custom_routes, custom_windows)
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual((task.origin, task.destination), ("GOI", "PNQ"))
        self.assertEqual(task.lead_days, 2)
        self.assertEqual(task.departure_date, FIXED_RUN_DATE + timedelta(days=2))

    def test_rejects_empty_routes(self):
        with self.assertRaises(ValueError):
            generate_route_date_matrix(FIXED_RUN_DATE, routes=[])

    def test_rejects_empty_ap_windows(self):
        with self.assertRaises(ValueError):
            generate_route_date_matrix(FIXED_RUN_DATE, ap_windows={})

    def test_rejects_non_positive_lead_days(self):
        with self.assertRaises(ValueError):
            generate_route_date_matrix(FIXED_RUN_DATE, ap_windows={"T+0": 0})
        with self.assertRaises(ValueError):
            generate_route_date_matrix(FIXED_RUN_DATE, ap_windows={"T-1": -1})


class SearchTaskPayloadTests(unittest.TestCase):
    def test_to_search_payload_shape_and_values(self):
        task = SearchTask(
            origin="DEL",
            destination="BOM",
            departure_date=date(2026, 9, 5),
            lead_days=7,
            advance_purchase_window="T+7",
            task_id="test-id",
        )
        payload = task.to_search_payload()
        self.assertEqual(
            payload,
            {
                "origin": "DEL",
                "destination": "BOM",
                "departure_date": "2026-09-05",
                "lead_days": 7,
                "advance_purchase_window": "T+7",
                "route_id": "DEL-BOM",
                "trip_type": "ONE_WAY",
                "adults": 1,
                "cabin_class": "ECONOMY",
            },
        )

    def test_payload_contains_no_network_artifacts(self):
        task = SearchTask("DEL", "BOM", date(2026, 9, 5), 7, "T+7", "id")
        payload = task.to_search_payload()
        for forbidden_key in ("url", "http_method", "headers", "cookies"):
            self.assertNotIn(forbidden_key, payload)

    def test_task_id_excluded_from_equality(self):
        a = SearchTask("DEL", "BOM", date(2026, 9, 5), 7, "T+7", "id-a")
        b = SearchTask("DEL", "BOM", date(2026, 9, 5), 7, "T+7", "id-b")
        self.assertEqual(a, b)


class RouteDateMatrixSchedulerTests(unittest.TestCase):
    def test_build_populates_queue_with_full_matrix(self):
        scheduler = RouteDateMatrixScheduler(run_date=FIXED_RUN_DATE)
        tasks = scheduler.build()
        self.assertEqual(len(tasks), len(ROUTE_PAIRS) * len(AP_WINDOWS_DAYS))
        self.assertEqual(len(scheduler), len(tasks))

    def test_dequeue_returns_tasks_in_generation_order(self):
        scheduler = RouteDateMatrixScheduler(run_date=FIXED_RUN_DATE)
        built = scheduler.build()
        dequeued = [scheduler.dequeue() for _ in range(len(built))]
        self.assertEqual(dequeued, built)

    def test_dequeue_on_empty_queue_returns_none(self):
        scheduler = RouteDateMatrixScheduler(run_date=FIXED_RUN_DATE)
        self.assertIsNone(scheduler.dequeue())
        scheduler.build()
        scheduler.drain()
        self.assertIsNone(scheduler.dequeue())

    def test_len_decreases_as_tasks_are_dequeued(self):
        scheduler = RouteDateMatrixScheduler(run_date=FIXED_RUN_DATE)
        total = len(scheduler.build())
        scheduler.dequeue()
        self.assertEqual(len(scheduler), total - 1)

    def test_drain_empties_queue_and_returns_remaining_tasks(self):
        scheduler = RouteDateMatrixScheduler(run_date=FIXED_RUN_DATE)
        scheduler.build()
        scheduler.dequeue()
        remaining = scheduler.drain()
        self.assertEqual(len(remaining), len(scheduler.routes) * len(scheduler.ap_windows) - 1)
        self.assertEqual(len(scheduler), 0)
        self.assertIsNone(scheduler.dequeue())

    def test_build_replaces_prior_queue_contents(self):
        scheduler = RouteDateMatrixScheduler(run_date=FIXED_RUN_DATE)
        scheduler.build()
        scheduler.dequeue()
        scheduler.build()  # rebuild before draining the first batch
        self.assertEqual(len(scheduler), len(ROUTE_PAIRS) * len(AP_WINDOWS_DAYS))

    def test_custom_routes_and_windows_override_defaults(self):
        scheduler = RouteDateMatrixScheduler(
            routes=[RoutePair("GOI", "PNQ")],
            ap_windows={"T+3": 3},
            run_date=FIXED_RUN_DATE,
        )
        tasks = scheduler.build()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].route_id, "GOI-PNQ")
        self.assertEqual(tasks[0].departure_date, FIXED_RUN_DATE + timedelta(days=3))

    def test_iteration_does_not_consume_queue(self):
        scheduler = RouteDateMatrixScheduler(run_date=FIXED_RUN_DATE)
        scheduler.build()
        before = len(scheduler)
        list(scheduler)  # iterate without mutating
        self.assertEqual(len(scheduler), before)

    def test_no_http_requests_are_ever_made(self):
        """Sanity guard: nothing in this module can perform network I/O —
        it exposes no requests/session objects and has no such imports."""
        import app.ingestion.scheduler as scheduler_module

        source = scheduler_module.__file__
        with open(source, encoding="utf-8") as fh:
            contents = fh.read()
        for forbidden in ("import requests", "import urllib", "import http.client", "import scrapy"):
            self.assertNotIn(forbidden, contents)


if __name__ == "__main__":
    unittest.main()
