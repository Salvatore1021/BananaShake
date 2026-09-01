"""
APIx — Route x Date-Matrix Scheduler (Ingestion Engine)
==========================================================

Centralized generator for the ingestion engine's daily fare-search task
queue: every (city pair) x (advance-purchase window) combination in the
route basket below, turned into a structured `SearchTask` carrying the
concrete departure date to search for.

This module is generation and queueing logic ONLY — it never makes a
network request. That happens later, in the spider layer, once a task is
dequeued and handed to a source-specific spider that knows how to turn a
SearchTask into that source's own search URL / form submission.

Route basket and advance-purchase windows are the two axes of the matrix:

  * ROUTE_PAIRS      -- the (origin, destination) city pairs to search.
  * AP_WINDOWS_DAYS  -- lead time (in days from "today") to price each
                         route at: T+1, T+7, T+15, T+30, T+45.

The full matrix for one run date is len(ROUTE_PAIRS) * len(AP_WINDOWS_DAYS)
tasks (20 x 5 = 100 today).

ROUTE_PAIRS covers both directions of 10 major Akasa-served metro city
pairs (the original 6 plus DEL-HYD, BOM-HYD, DEL-PNQ and BOM-GOI) rather
than one direction each, because a route's outbound and return fares are
priced independently by the carrier and are not mirror images of each
other -- collapsing them to one direction would silently halve the index's
real route coverage. Both this and AP_WINDOWS_DAYS stay swappable via
generate_route_date_matrix()'s own routes/ap_windows parameters (and
RouteDateMatrixScheduler's constructor) for a one-off custom basket -- see
test_custom_routes_and_windows_are_respected / the scheduler-level
equivalent.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from itertools import product
from typing import Iterable, Iterator

# India Standard Time (UTC+5:30). Advance-purchase windows are anchored to
# the travel market's own calendar day, not UTC's — using UTC directly would
# put "today" a day off from what a traveller in India means by T+1 during
# the ~5.5-hour band each day where the UTC and IST calendar dates disagree
# (e.g. 20:00 UTC is already the next day in IST).
IST = timezone(timedelta(hours=5, minutes=30))


@dataclass(frozen=True)
class RoutePair:
    """One directional city pair to search fares for."""

    origin: str
    destination: str

    def __post_init__(self) -> None:
        if not self.origin or not self.destination:
            raise ValueError("RoutePair requires non-empty origin and destination codes")
        if self.origin == self.destination:
            raise ValueError(f"RoutePair origin and destination must differ, got '{self.origin}' twice")

    @property
    def route_id(self) -> str:
        return f"{self.origin}-{self.destination}"


# Ten major metro city pairs, both directions -- 20 routes total. The
# original six plus four more high-traffic Akasa-served pairs (DEL-HYD,
# BOM-HYD, DEL-PNQ, BOM-GOX), each searched outbound and return.
#
# Goa is coded GOX (Manohar International, Mopa) rather than the older
# GOI (Dabolim) -- confirmed live against Akasa's own fare-search API,
# which returns HTTP 400 for every GOI request (real flights back for
# GOX). Akasa's Goa service runs out of the newer airport.
ROUTE_PAIRS: tuple[RoutePair, ...] = (
    RoutePair("DEL", "BOM"),
    RoutePair("BOM", "DEL"),
    RoutePair("DEL", "BLR"),
    RoutePair("BLR", "DEL"),
    RoutePair("BOM", "BLR"),
    RoutePair("BLR", "BOM"),
    RoutePair("DEL", "CCU"),
    RoutePair("CCU", "DEL"),
    RoutePair("BLR", "HYD"),
    RoutePair("HYD", "BLR"),
    RoutePair("MAA", "DEL"),
    RoutePair("DEL", "MAA"),
    RoutePair("DEL", "HYD"),
    RoutePair("HYD", "DEL"),
    RoutePair("BOM", "HYD"),
    RoutePair("HYD", "BOM"),
    RoutePair("DEL", "PNQ"),
    RoutePair("PNQ", "DEL"),
    RoutePair("BOM", "GOX"),
    RoutePair("GOX", "BOM"),
)

# Advance-purchase windows: label -> lead days from the run date.
AP_WINDOWS_DAYS: dict[str, int] = {
    "T+1": 1,
    "T+7": 7,
    "T+15": 15,
    "T+30": 30,
    "T+45": 45,
}


@dataclass(frozen=True)
class SearchTask:
    """One concrete fare-search task: a route, priced at a specific
    departure date derived from an advance-purchase lead time.

    `task_id` is excluded from equality/hash so two tasks built from the
    same (route, run_date, window) — the only inputs that actually define a
    search — compare equal regardless of when the id string was formatted.
    """

    origin: str
    destination: str
    departure_date: date
    lead_days: int
    advance_purchase_window: str
    task_id: str = field(compare=False, default="")

    @property
    def route_id(self) -> str:
        return f"{self.origin}-{self.destination}"

    def to_search_payload(self) -> dict:
        """The structured, source-agnostic payload downstream spiders map
        onto their own query-param / form shape. Pure data shaping — no
        network I/O happens here."""
        return {
            "origin": self.origin,
            "destination": self.destination,
            "departure_date": self.departure_date.isoformat(),
            "lead_days": self.lead_days,
            "advance_purchase_window": self.advance_purchase_window,
            "route_id": self.route_id,
            "trip_type": "ONE_WAY",
            "adults": 1,
            "cabin_class": "ECONOMY",
        }


def default_run_date() -> date:
    """Today's calendar date in IST — the anchor every AP window is
    computed from."""
    return datetime.now(IST).date()


def generate_route_date_matrix(
    run_date: date | None = None,
    routes: Iterable[RoutePair] = ROUTE_PAIRS,
    ap_windows: dict[str, int] | None = None,
) -> list[SearchTask]:
    """Build the full (route x AP-window) matrix of SearchTasks for one run
    date.

    Pure function: identical inputs always produce identical tasks (in the
    same order), which is what makes this trivially unit-testable without
    mocking a clock — pass an explicit `run_date` and the output is fully
    deterministic.
    """
    run_date = run_date if run_date is not None else default_run_date()
    ap_windows = ap_windows if ap_windows is not None else AP_WINDOWS_DAYS
    routes = tuple(routes)

    if not routes:
        raise ValueError("generate_route_date_matrix: routes must not be empty")
    if not ap_windows:
        raise ValueError("generate_route_date_matrix: ap_windows must not be empty")
    for label, lead_days in ap_windows.items():
        if lead_days <= 0:
            raise ValueError(f"AP window '{label}' must have a positive lead_days, got {lead_days}")

    tasks: list[SearchTask] = []
    for route, (window_label, lead_days) in product(routes, ap_windows.items()):
        departure_date = run_date + timedelta(days=lead_days)
        task_id = f"{run_date.isoformat()}::{route.route_id}::{window_label}"
        tasks.append(
            SearchTask(
                origin=route.origin,
                destination=route.destination,
                departure_date=departure_date,
                lead_days=lead_days,
                advance_purchase_window=window_label,
                task_id=task_id,
            )
        )
    return tasks


class RouteDateMatrixScheduler:
    """Dispatcher wrapping the matrix generator with a FIFO task queue.

    Usage:
        scheduler = RouteDateMatrixScheduler()
        scheduler.build()
        while (task := scheduler.dequeue()) is not None:
            payload = task.to_search_payload()
            ...hand off to a spider... (not done here — no HTTP in this module)
    """

    def __init__(
        self,
        routes: Iterable[RoutePair] = ROUTE_PAIRS,
        ap_windows: dict[str, int] | None = None,
        run_date: date | None = None,
    ):
        self.routes: tuple[RoutePair, ...] = tuple(routes)
        self.ap_windows: dict[str, int] = dict(ap_windows) if ap_windows is not None else dict(AP_WINDOWS_DAYS)
        self.run_date: date = run_date if run_date is not None else default_run_date()
        self._queue: deque[SearchTask] = deque()

    def build(self) -> list[SearchTask]:
        """(Re)generate the matrix and load it into the queue, replacing
        whatever was queued before. Returns the generated tasks."""
        tasks = generate_route_date_matrix(self.run_date, self.routes, self.ap_windows)
        self._queue = deque(tasks)
        return tasks

    def dequeue(self) -> SearchTask | None:
        """Pop the next task in FIFO (generation) order, or None once the
        queue is empty."""
        return self._queue.popleft() if self._queue else None

    def drain(self) -> list[SearchTask]:
        """Pop and return every remaining queued task, emptying the queue."""
        tasks = list(self._queue)
        self._queue.clear()
        return tasks

    def __len__(self) -> int:
        return len(self._queue)

    def __iter__(self) -> Iterator[SearchTask]:
        return iter(list(self._queue))
